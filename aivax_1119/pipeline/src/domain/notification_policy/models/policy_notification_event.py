"""정책 탐지 결과를 나타내는 도메인 이벤트."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from src.core.ddd.domain_entity import DomainEntity
from src.core.hash import HashGenerator
from src.domain.notification_policy.errors import PolicyNotificationDomainError
from src.domain.notification_policy.models.occurred_at import OccurredAt


PolicyNotificationMode = Literal["block", "masking", "accept"]


@dataclass(frozen=True, slots=True)
class PolicyNotificationEvent(DomainEntity):
    occurred_at: OccurredAt = field(default_factory=OccurredAt.now)
    message_id: str = ""
    user_id: str = ""
    user_role: str | None = None
    user_email: str = ""
    user_uuid: str | None = None
    ai_service: str = ""
    prompt: str = ""
    mode: PolicyNotificationMode | None = None
    output_text: str = ""
    policy_name: str | None = None
    event_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.validate()
        # 재시도 시에도 바뀌지 않는 결정적 ID를 생성한다.
        object.__setattr__(self, "event_id", self._create_event_id())

    def validate(self) -> None:
        """정책 알림 이벤트가 Stream에 발행 가능한 상태인지 확인한다."""
        if self.mode not in {"block", "masking", "accept"}:
            raise PolicyNotificationDomainError.invalid_mode(self.mode)
        if not isinstance(self.occurred_at, OccurredAt):
            raise PolicyNotificationDomainError.invalid_occurred_at(self.occurred_at)
        required_fields = {
            "message_id": self.message_id,
            "user_id": self.user_id,
            "ai_service": self.ai_service,
            "prompt": self.prompt,
            "output_text": self.output_text,
        }
        invalid_fields = [
            name
            for name, value in required_fields.items()
            if not isinstance(value, str) or not value.strip()
        ]
        if invalid_fields:
            raise PolicyNotificationDomainError.required_fields_missing(invalid_fields)

    def _create_event_id(self) -> str:
        """Management의 정책 알림 중복 기준을 나타내는 128비트 해시를 반환한다."""
        # Management는 동일 messageId와 mode를 중복 알림으로 취급한다.
        # 이 값은 Stream에 노출하지 않고 adapter의 재전송 dedupe marker에만 사용한다.
        canonical_value = {
            "messageId": self.message_id,
            "mode": self.mode,
        }
        return HashGenerator.blake2s(canonical_value)
