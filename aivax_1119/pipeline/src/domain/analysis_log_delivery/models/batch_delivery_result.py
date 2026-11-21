"""분석 로그 배치 전달 결과."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AnalysisLogDeliveryRow:
    index: int
    succeeded: bool


@dataclass(frozen=True, slots=True)
class AnalysisLogBatchDeliveryResult:
    rows: tuple[AnalysisLogDeliveryRow, ...]
