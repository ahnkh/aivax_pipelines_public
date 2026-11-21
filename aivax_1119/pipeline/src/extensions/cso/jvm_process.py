"""worker별 상주 JVM과 직렬 stdio 요청의 수명 관리."""

import asyncio
import logging
import math
from pathlib import Path

from src.extensions.cso.protocol import (
    HEADER,
    MAX_FRAME_BYTES,
    READY,
    encode_request,
)


class CsoJvmProcess:
    def __init__(
        self,
        directory: Path,
        *,
        timeout_seconds: float = 5,
        startup_timeout_seconds: float = 15,
        logger: logging.Logger | None = None,
    ) -> None:
        for timeout in (timeout_seconds, startup_timeout_seconds):
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("CSO timeout must be positive and finite")
        self._directory = directory.resolve()
        self._timeout_seconds = timeout_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._logger = logger or logging.getLogger("khan-python")
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._request_id = 0
        self._closed = False

    async def start(self) -> None:
        async with self._lock:
            self._ensure_open()
            try:
                async with asyncio.timeout(self._startup_timeout_seconds):
                    await self._start_locked()
            except BaseException:
                await self._stop_locked()
                raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("CSO JVM runtime is closed")

    async def _start_locked(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        await self._stop_locked()
        worker_directory = Path(__file__).resolve().parent / "java"
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                "java",
                "-Dfile.encoding=UTF-8",
                "-cp",
                f"{worker_directory}:{self._directory}:{self._directory / 'lib'}/*",
                "CsoJvmWorker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )
        cancelled = False
        while not spawn.done():
            try:
                await asyncio.shield(spawn)
            except asyncio.CancelledError:
                cancelled = True
        # spawn 도중 취소돼도 만들어진 자식의 소유권을 확보한 뒤 회수한다.
        self._process = spawn.result()
        if cancelled:
            raise asyncio.CancelledError
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process))
        assert self._process.stdout is not None
        if await self._process.stdout.readexactly(len(READY)) != READY:
            raise ValueError("잘못된 CSO JVM 준비 응답입니다.")

    async def request(self, paths: tuple[str, ...]) -> bytes:
        self._ensure_open()
        # 대기 중 취소/시간 초과는 현재 JVM을 사용 중인 다른 요청에 영향을 주지 않는다.
        async with asyncio.timeout(self._timeout_seconds):
            async with self._lock:
                self._ensure_open()
                try:
                    await self._start_locked()
                    self._request_id += 1
                    frame = encode_request(self._request_id, paths)
                    process = self._process
                    assert process is not None
                    assert process.stdin is not None and process.stdout is not None
                    process.stdin.write(frame)
                    await process.stdin.drain()
                    header = await process.stdout.readexactly(HEADER.size)
                    response_id, length = HEADER.unpack(header)
                    if response_id != self._request_id:
                        raise ValueError("CSO 응답 ID가 요청과 다릅니다.")
                    if length > MAX_FRAME_BYTES:
                        raise ValueError("CSO 응답 크기가 제한을 초과했습니다.")
                    return await process.stdout.readexactly(length)
                except BaseException:
                    # 전송 이후 취소·타임아웃·통신 오류는 늦은 응답을 남기지 않는다.
                    await self._stop_locked()
                    raise

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        process, self._process = self._process, None
        stderr_task, self._stderr_task = self._stderr_task, None
        if process is None and stderr_task is None:
            return
        cleanup = asyncio.create_task(self._reap(process, stderr_task))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    async def _reap(
        process: asyncio.subprocess.Process | None,
        stderr_task: asyncio.Task[None] | None,
    ) -> None:
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            drains = []
            if process.stdout is not None:
                drains.append(process.stdout.read())
            if process.stderr is not None and stderr_task is None:
                drains.append(process.stderr.read())
            await asyncio.gather(*drains)
            await process.wait()
            if process.stdin is not None:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
        if stderr_task is not None:
            await asyncio.gather(stderr_task, return_exceptions=True)

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while chunk := await process.stderr.read(4096):
            self._logger.warning(
                "CSO Java 실행 로그: %s",
                chunk.decode("utf-8", errors="replace").strip(),
            )
