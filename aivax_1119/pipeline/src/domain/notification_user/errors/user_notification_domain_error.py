"""사용자 알림 도메인 예외."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from src.core.ddd.domain_exception import DomainException


UserNotificationDomainErrorCode = Literal["required-fields-missing",]


class UserNotificationDomainError(DomainException):
    """사용자 알림 도메인의 불변식 위반을 나타낸다."""

    def __init__(
        self,
        code: UserNotificationDomainErrorCode,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("user-notification", code, message, details)

    @classmethod
    def required_fields_missing(
        cls,
        fields: list[str],
    ) -> "UserNotificationDomainError":
        return cls(
            "required-fields-missing",
            f"사용자 알림 필수 값이 비어 있습니다: {', '.join(fields)}",
            {"fields": fields},
        )
