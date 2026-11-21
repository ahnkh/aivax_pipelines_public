"""분석 로그 병합 도메인의 불변식 위반 예외."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from src.core.ddd.domain_exception import DomainException


AnalysisLogMergeDomainErrorCode = Literal[
    "empty-message-key",
    "invalid-log-document",
]


class AnalysisLogMergeDomainError(DomainException):
    """분석 로그 병합 모델이 지켜야 할 불변식 위반을 나타낸다."""

    def __init__(
        self,
        code: AnalysisLogMergeDomainErrorCode,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("analysis-log-merge", code, message, details)

    @classmethod
    def empty_message_key(cls, value: object) -> "AnalysisLogMergeDomainError":
        return cls(
            "empty-message-key",
            "분석 로그 병합 키는 비어 있지 않은 문자열이어야 합니다.",
            {"message_key": value},
        )

    @classmethod
    def invalid_log_document(cls, value: object) -> "AnalysisLogMergeDomainError":
        return cls(
            "invalid-log-document",
            "분석 로그 문서는 key/value 구조여야 합니다.",
            {"document_type": type(value).__name__},
        )
