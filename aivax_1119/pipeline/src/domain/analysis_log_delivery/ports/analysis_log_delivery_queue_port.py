"""분석 로그 외부 전달 대기열 계약."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractContextManager

from src.domain.analysis_log_delivery.models import (
    MergedAnalysisLogDocument,
    PendingAnalysisLog,
    PendingAnalysisLogBatch,
)


class AnalysisLogDeliveryQueuePort(ABC):
    @abstractmethod
    def enqueue(
        self,
        event_key: str,
        analysis_log: MergedAnalysisLogDocument,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def claim_batch(
        self,
        limit: int,
    ) -> AbstractContextManager[PendingAnalysisLogBatch]:
        """현재 실행이 독점 처리할 전달 대기 batch를 획득한다."""
        raise NotImplementedError

    @abstractmethod
    def claim_pending(
        self,
    ) -> AbstractContextManager[PendingAnalysisLogBatch]:
        """실행 시작 시점의 전체 전달 대기 항목을 독점 획득한다."""
        raise NotImplementedError

    @abstractmethod
    def acknowledge(self, entries: Sequence[PendingAnalysisLog]) -> int:
        """전달이 완료된 항목을 제거하고 제거 건수를 반환한다."""
        raise NotImplementedError

    @abstractmethod
    def reschedule(self, entries: Sequence[PendingAnalysisLog]) -> int:
        """항목의 재시도 횟수를 증가시키고 갱신 건수를 반환한다."""
        raise NotImplementedError

    @abstractmethod
    def quarantine(self, entries: Sequence[PendingAnalysisLog]) -> int:
        """재전송하지 않을 항목을 격리하고 이동 건수를 반환한다."""
        raise NotImplementedError
