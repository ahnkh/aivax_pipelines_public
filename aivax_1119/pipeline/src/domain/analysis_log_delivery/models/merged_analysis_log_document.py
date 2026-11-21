"""외부 저장소로 전달할 병합 완료 분석 로그 문서."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from src.core.ddd.domain_entity import DomainEntity
from src.domain.analysis_log_delivery.errors import AnalysisLogDeliveryDomainError


@dataclass(frozen=True, slots=True)
class MergedAnalysisLogDocument(DomainEntity):
    """전송 대상과 무관한 병합 분석 로그의 도메인 표현."""

    document: Mapping[str, Any]

    def __post_init__(self) -> None:
        self.validate()
        # caller가 전달한 dict를 이후에 수정해도 전송 대상은 변하지 않는다.
        object.__setattr__(self, "document", deepcopy(dict(self.document)))

    def validate(self) -> None:
        if not isinstance(self.document, Mapping):
            raise AnalysisLogDeliveryDomainError.invalid_merged_analysis_log_document(
                self.document,
            )
        if not self.document:
            raise AnalysisLogDeliveryDomainError.empty_merged_analysis_log_document()

    def to_dict(self) -> dict[str, Any]:
        """adapter가 직렬화·전송할 독립된 문서 복사본을 반환한다."""
        return deepcopy(dict(self.document))
