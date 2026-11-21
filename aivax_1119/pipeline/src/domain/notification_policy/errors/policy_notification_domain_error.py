"""정책 알림 도메인 예외."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from src.core.ddd.domain_exception import DomainException


PolicyNotificationDomainErrorCode = Literal[
    "invalid-occurred-at",
    "invalid-mode",
    "required-fields-missing",
]


class PolicyNotificationDomainError(DomainException):
    """정책 알림 도메인의 불변식 위반을 나타낸다."""

    def __init__(
        self,
        code: PolicyNotificationDomainErrorCode,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("policy-notification", code, message, details)

    @classmethod
    def invalid_mode(cls, mode: object) -> "PolicyNotificationDomainError":
        return cls(
            "invalid-mode",
            "mode는 block, masking, accept 중 하나여야 합니다.",
            {"mode": mode},
        )

    @classmethod
    def invalid_occurred_at(
        cls,
        occurred_at: object,
    ) -> "PolicyNotificationDomainError":
        return cls(
            "invalid-occurred-at",
            "occurred_at은 시간대가 포함된 ISO 8601 문자열이어야 합니다.",
            {"occurred_at": occurred_at},
        )

    @classmethod
    def required_fields_missing(
        cls,
        fields: list[str],
    ) -> "PolicyNotificationDomainError":
        return cls(
            "required-fields-missing",
            f"정책 알림 필수 값이 비어 있습니다: {', '.join(fields)}",
            {"fields": fields},
        )
