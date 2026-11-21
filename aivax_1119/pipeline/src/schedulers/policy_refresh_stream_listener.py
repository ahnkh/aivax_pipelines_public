"""모든 Pipeline worker에 정책 cache 갱신을 fan-out 하는 Stream listener."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from valkey import Valkey


class PolicyRefreshStreamListener:
    """독립 cursor로 Stream 전체를 읽어 process-local 정책 cache를 갱신한다.

    consumer group을 공유하면 한 worker만 이벤트를 받으므로 사용하지 않는다.
    시작 시 cursor를 먼저 고정하고 cache snapshot을 초기화하면 그 뒤의 이벤트를
    빠짐없이 처리할 수 있다.
    """

    def __init__(
        self,
        client: Valkey,
        stream_name: str,
        refresh: Callable[[], None],
        *,
        block_ms: int = 500,
        logger: logging.Logger | None = None,
    ) -> None:
        if not stream_name.strip():
            raise ValueError("policy refresh stream name은 비어 있을 수 없습니다.")
        if block_ms <= 0:
            raise ValueError("policy refresh stream block time은 양수여야 합니다.")
        self._client = client
        self._stream_name = stream_name
        self._refresh = refresh
        self._block_ms = block_ms
        self._logger = logger or logging.getLogger("khan-python")
        self._last_id = "0-0"
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def prepare(self) -> None:
        """초기 snapshot load 전에 현재 Stream 끝을 cursor로 기록한다."""
        try:
            entries = self._client.xrevrange(
                self._stream_name,
                max="+",
                min="-",
                count=1,
            )
            if entries:
                self._last_id = self._text(entries[0][0])
        except Exception:
            # 초기 cache refresh/polling이 복구 경로이므로 listener 준비 실패로
            # worker 기동을 중단하지 않는다. 0-0부터 재생해 누락도 피한다.
            self._logger.warning(
                "policy refresh stream cursor preparation failed; "
                "replaying retained events from the beginning",
                exc_info=True,
            )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="aivax-policy-refresh-stream",
            daemon=True,
        )
        self._thread.start()

    def shutdown(self) -> bool:
        self._stop.set()
        if self._thread is None:
            return True
        self._thread.join(timeout=(self._block_ms / 1_000) + 2)
        if self._thread.is_alive():
            self._logger.error("policy refresh stream listener shutdown timed out")
            return False
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                entries = self._client.xread(
                    {self._stream_name: self._last_id},
                    count=100,
                    block=self._block_ms,
                )
            except Exception:
                if not self._stop.is_set():
                    self._logger.exception("policy refresh stream read failed")
                    self._stop.wait(1)
                continue

            for _, records in entries or []:
                for event_id, _ in records:
                    if self._stop.is_set():
                        return
                    try:
                        self._refresh()
                    except Exception:
                        self._logger.exception(
                            "policy refresh stream event failed (event_id=%s)",
                            self._text(event_id),
                        )
                        self._stop.wait(1)
                        break
                    self._last_id = self._text(event_id)
                    self._logger.info(
                        "policy cache refreshed from stream (event_id=%s)",
                        self._last_id,
                    )

    @staticmethod
    def _text(value: str | bytes) -> str:
        return value.decode() if isinstance(value, bytes) else value
