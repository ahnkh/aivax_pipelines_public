"""정책 cache 갱신 신호를 모든 Pipeline worker에 발행한다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PolicyRefreshSignalPublisher(Protocol):
    def publish(self, *, source: str) -> str: ...


@dataclass(frozen=True, slots=True)
class PolicyRefreshSignalReceipt:
    event_id: str


class PublishPolicyRefreshSignalUseCase:
    """레거시 HTTP 호출도 Stream fan-out 경로로 전환한다."""

    def __init__(self, publisher: PolicyRefreshSignalPublisher) -> None:
        self._publisher = publisher

    def execute(self) -> PolicyRefreshSignalReceipt:
        return PolicyRefreshSignalReceipt(
            event_id=self._publisher.publish(source="pipeline-api"),
        )
