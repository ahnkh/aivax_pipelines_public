"""외부 전달을 기다리는 분석 로그."""

from dataclasses import dataclass

from src.domain.analysis_log_delivery.models.merged_analysis_log_document import (
    MergedAnalysisLogDocument,
)


@dataclass(frozen=True, slots=True)
class PendingAnalysisLog:
    event_key: str
    receipt: str
    analysis_log: MergedAnalysisLogDocument
    retry_count: int = 0


@dataclass(frozen=True, slots=True)
class PendingAnalysisLogBatch:
    entries: tuple[PendingAnalysisLog, ...]
    unreadable_count: int = 0
