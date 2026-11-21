"""완성 분석 로그의 배치 외부 전달 계약."""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from src.domain.analysis_log_delivery.models import (
    AnalysisLogBatchDeliveryResult,
    MergedAnalysisLogDocument,
)


class AnalysisLogBatchDeliveryPort(ABC):
    @abstractmethod
    def deliver(
        self,
        logs: Sequence[MergedAnalysisLogDocument],
    ) -> AnalysisLogBatchDeliveryResult:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError
