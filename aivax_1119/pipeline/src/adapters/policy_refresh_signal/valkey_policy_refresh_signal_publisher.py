"""Valkey Stream으로 정책 cache 갱신 신호를 발행한다."""

from __future__ import annotations

from valkey import Valkey


class ValkeyPolicyRefreshSignalPublisher:
    def __init__(self, client: Valkey, stream_name: str) -> None:
        if not stream_name.strip():
            raise ValueError("policy refresh stream name은 비어 있을 수 없습니다.")
        self._client = client
        self._stream_name = stream_name

    def publish(self, *, source: str) -> str:
        event_id = self._client.xadd(
            self._stream_name,
            {"source": source},
        )
        return event_id.decode() if isinstance(event_id, bytes) else str(event_id)
