"""AIVAX 설정 도메인의 불변식 위반 예외."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from src.core.ddd.domain_exception import DomainException

AivaxConfigDomainErrorCode = Literal[
    "invalid-schema-version",
    "invalid-mode",
    "invalid-updated-at",
    "invalid-source",
    "invalid-filters",
]


class AivaxConfigDomainError(DomainException):
    def __init__(
        self,
        code: AivaxConfigDomainErrorCode,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("aivax-config", code, message, details)

    @classmethod
    def invalid_schema_version(cls, value: object) -> "AivaxConfigDomainError":
        return cls(
            "invalid-schema-version",
            "schema_version은 1 또는 2여야 합니다.",
            {"schema_version": value},
        )

    @classmethod
    def invalid_mode(cls, value: object) -> "AivaxConfigDomainError":
        return cls(
            "invalid-mode",
            "mode는 inline, mirror, proxy 중 하나여야 합니다.",
            {"mode": value},
        )

    @classmethod
    def invalid_updated_at(cls, value: object) -> "AivaxConfigDomainError":
        return cls(
            "invalid-updated-at",
            "updated_at은 시간대가 포함된 datetime이어야 합니다.",
            {"updated_at": value},
        )

    @classmethod
    def invalid_source(cls, value: object) -> "AivaxConfigDomainError":
        return cls(
            "invalid-source",
            "source는 비어 있지 않은 문자열이어야 합니다.",
            {"source": value},
        )
