"""병합 분석 로그의 외부 전달 계약."""

from abc import ABC, abstractmethod

from src.domain.analysis_log_delivery.models.merged_analysis_log_document import (
    MergedAnalysisLogDocument,
)

BULK_LOG_ARCHIVE_AGE_SECONDS = 87_400


class AnalysisLogDeliveryPort(ABC):
    """병합 완료 분석 로그를 외부 목적지에 즉시 전달한다.

    구현체는 Fluent Bit 감시 파일에 기록하거나 OpenSearch에 직접 적재할 수 있다.
    도메인은 목적지와 전송 프로토콜을 알지 않는다.
    """

    @abstractmethod
    def deliver(self, analysis_log: MergedAnalysisLogDocument) -> bool:
        """병합 완료 분석 로그 하나를 외부 목적지에 즉시 전달한다."""
        raise NotImplementedError

    def archive_bulk_logs(self, backup_directory: str) -> int:
        """전달용 로컬 로그를 백업하고 건수를 반환한다. 파일 미사용 구현은 0."""
        return 0
