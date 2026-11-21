"""분석 로그 전달 도메인의 불변식 위반 예외."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from src.core.ddd.domain_exception import DomainException


AnalysisLogDeliveryDomainErrorCode = Literal[
    "invalid-merged-analysis-log-document",
    "empty-merged-analysis-log-document",
]


class AnalysisLogDeliveryDomainError(DomainException):
    """외부로 전달할 병합 분석 로그 문서가 유효하지 않을 때 발생한다."""

    def __init__(
        self,
        code: AnalysisLogDeliveryDomainErrorCode,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("analysis-log-delivery", code, message, details)

    @classmethod
    def invalid_merged_analysis_log_document(
        cls,
        value: object,
    ) -> "AnalysisLogDeliveryDomainError":
        return cls(
            "invalid-merged-analysis-log-document",
            "병합 완료 분석 로그는 key/value 문서여야 합니다.",
            {"document_type": type(value).__name__},
        )

    @classmethod
    def empty_merged_analysis_log_document(
        cls,
    ) -> "AnalysisLogDeliveryDomainError":
        return cls(
            "empty-merged-analysis-log-document",
            "빈 병합 완료 분석 로그는 외부로 전달할 수 없습니다.",
        )
