"""실제 자식 프로세스로 상주 JVM 통신·동시성·장애 회수를 검증한다."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.extensions.cso.fasoo_grade_adapter import FasooCsoGradeAdapter
from src.extensions.cso.jvm_process import CsoJvmProcess

# JVM과 같은 프레임을 사용하는 테스트 프로세스. sleep은 부모의 kill로 종료한다.
SERVER = r"""
import json, pathlib, struct, sys, time
source, target = sys.stdin.buffer, sys.stdout.buffer
target.write(b"AIVAX_CSO_V1\n"); target.flush()
while True:
    header = source.read(12)
    if not header:
        break
    request_id, count = struct.unpack(">QI", header)
    paths = []
    for _ in range(count):
        length, = struct.unpack(">I", source.read(4))
        paths.append(source.read(length).decode("utf-8"))
    if paths[0].startswith("wait:"):
        pathlib.Path(paths[0][5:]).touch()
        time.sleep(60)
    if paths[0] == "exit":
        sys.exit(9)
    if paths[0] == "stderr":
        sys.stderr.write("x" * 131072); sys.stderr.flush()
    result = json.dumps(paths, ensure_ascii=False).encode("utf-8")
    if paths[0] == "wrong-id":
        request_id += 1
    length = 9 * 1024 * 1024 if paths[0] in {"oversize", "flood"} else len(result)
    target.write(struct.pack(">QI", request_id, length))
    if paths[0] == "flood":
        target.write(b"x" * 262144); target.flush()
    if paths[0] == "partial":
        target.flush(); sys.exit(8)
    target.write(result); target.flush()
"""


@pytest.fixture
def processes(monkeypatch):
    spawn = asyncio.create_subprocess_exec
    children = []

    async def start(*args, **kwargs):
        process = await spawn(sys.executable, "-u", "-c", SERVER, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    return children


async def wait_for_marker(marker):
    async with asyncio.timeout(2):
        while not marker.exists():
            await asyncio.sleep(0.01)


def test_reuses_process_for_concurrent_batches_and_unicode_paths(processes):
    async def scenario():
        runtime = CsoJvmProcess(Path("."))
        try:
            await runtime.start()
            paths = [(f"등급 {i}\n문서.docx", str(i)) for i in range(12)]
            replies = await asyncio.gather(*(runtime.request(p) for p in paths))
            assert [json.loads(reply) for reply in replies] == [list(p) for p in paths]
            assert len(processes) == 1
            assert processes[0].returncode is None
        finally:
            await runtime.aclose()
        assert processes[0].returncode is not None
        with pytest.raises(RuntimeError, match="closed"):
            await runtime.request(("3",))

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ("exit", "wrong-id", "oversize", "partial", "flood"))
def test_protocol_failure_reaps_process_and_next_request_recovers(processes, fault):
    async def scenario():
        runtime = CsoJvmProcess(Path("."))
        try:
            with pytest.raises((ValueError, asyncio.IncompleteReadError)):
                await runtime.request((fault,))
            assert processes[0].returncode is not None
            assert json.loads(await runtime.request(("recovered",))) == ["recovered"]
            assert len(processes) == 2
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_timeout_reaps_process_without_reusing_late_response(processes, tmp_path):
    async def scenario():
        runtime = CsoJvmProcess(Path("."), timeout_seconds=0.3)
        marker = tmp_path / "received"
        try:
            await runtime.start()
            with pytest.raises(TimeoutError):
                await runtime.request((f"wait:{marker}",))
            assert marker.exists()
            assert processes[0].returncode is not None
            assert json.loads(await runtime.request(("next",))) == ["next"]
            assert len(processes) == 2
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_queued_cancellation_preserves_owner_and_active_cancellation_reaps(
    processes, tmp_path
):
    async def scenario():
        runtime = CsoJvmProcess(Path("."))
        marker = tmp_path / "received"
        owner = asyncio.create_task(runtime.request((f"wait:{marker}",)))
        try:
            await wait_for_marker(marker)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(runtime.request(("queued",)), timeout=0.05)
            assert processes[0].returncode is None
            assert not owner.done()
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
            assert processes[0].returncode is not None
            assert json.loads(await runtime.request(("next",))) == ["next"]
        finally:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
            await runtime.aclose()

    asyncio.run(scenario())


def test_drains_stderr_while_request_is_running(processes):
    async def scenario():
        logger = Mock()
        runtime = CsoJvmProcess(Path("."), logger=logger)
        try:
            assert json.loads(await runtime.request(("stderr",))) == ["stderr"]
        finally:
            await runtime.aclose()
        assert logger.warning.called

    asyncio.run(scenario())


def test_startup_timeout_reaps_process(monkeypatch):
    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def start(*args, **kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
        runtime = CsoJvmProcess(Path("."), startup_timeout_seconds=0.05)
        try:
            with pytest.raises(TimeoutError):
                await runtime.start()
            assert process.returncode is not None
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_cancellation_during_spawn_still_reaps_child(monkeypatch):
    async def scenario():
        spawn = asyncio.create_subprocess_exec
        created, release = asyncio.Event(), asyncio.Event()
        children = []

        async def delayed_spawn(*args, **kwargs):
            process = await spawn(sys.executable, "-u", "-c", SERVER, **kwargs)
            children.append(process)
            created.set()
            await release.wait()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
        runtime = CsoJvmProcess(Path("."))
        task = asyncio.create_task(runtime.start())
        try:
            await created.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(children) == 1
            assert children[0].returncode is not None
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.aclose()

    asyncio.run(scenario())


def test_empty_grade_lookup_does_not_launch_jvm(processes):
    async def scenario():
        runtime = CsoJvmProcess(Path("."))
        try:
            assert await FasooCsoGradeAdapter(runtime).read_grades(()) == ()
            assert processes == []
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
