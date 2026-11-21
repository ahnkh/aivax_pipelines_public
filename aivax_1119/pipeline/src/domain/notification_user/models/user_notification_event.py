"""Pipeline의 사용자 활동을 나타내는 도메인 이벤트."""

from __future__ import annotations

from dataclasses import dataclass

from src.core.ddd.domain_entity import DomainEntity
from src.domain.notification_user.errors import UserNotificationDomainError


@dataclass(frozen=True, slots=True)
class UserNotificationEvent(DomainEntity):
    """Management 사용자 동기화 Stream과 독립적인 도메인 이벤트."""

    message_id: str
    email: str
    # 생성 시 검증 오류로 바꾸기 위해 누락 가능 입력도 받는다. 정상 Entity에는
    # validate()를 통과한 int 또는 str만 존재한다.
    # 분석 로그에 표시하는 `GPT` 같은 이름이 아니라 요청에서 받은 원본 ID다.
    ai_service: int | str | None
    client_ip: str | None = None
    prompt: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """사용자 동기화에 필요한 식별자와 선택 필드 상태를 검증한다."""
        required_fields = {
            "message_id": self.message_id,
            "ai_service": self.ai_service,
        }
        invalid_fields = [
            name
            for name, value in required_fields.items()
            if value is None or not str(value).strip()
        ]
        if "ai_service" not in invalid_fields:
            ai_service_id = str(self.ai_service).strip()
            if (
                isinstance(self.ai_service, bool)
                or not ai_service_id.isascii()
                or not ai_service_id.isdecimal()
                or int(ai_service_id) <= 0
            ):
                invalid_fields.append("ai_service")
        if invalid_fields:
            raise UserNotificationDomainError.required_fields_missing(invalid_fields)

        optional_fields = {
            "name": self.name,
            "client_ip": self.client_ip,
            "prompt": self.prompt,
        }
        invalid_optional_fields = [
            name
            for name, value in optional_fields.items()
            if value is not None and (not isinstance(value, str) or not value.strip())
        ]
        if invalid_optional_fields:
            raise UserNotificationDomainError.required_fields_missing(
                invalid_optional_fields,
            )
