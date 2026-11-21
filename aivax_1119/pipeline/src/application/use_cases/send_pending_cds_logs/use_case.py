"""대기 중인 CDS 로그를 외부 API로 전송하는 유스케이스."""

from __future__ import annotations

from collections.abc import Sequence
import logging

from src.domain.analysis_log_delivery.errors import (
    RetryableAnalysisLogDeliveryError,
)
from src.domain.analysis_log_delivery.models import (
    AnalysisLogDeliveryRow,
    PendingAnalysisLog,
)
from src.domain.analysis_log_delivery.ports import (
    AnalysisLogBatchDeliveryPort,
    AnalysisLogDeliveryQueuePort,
)
from src.domain.notification_audit.models import AuditEvent
from src.domain.notification_audit.ports import AuditNotificationPort


class SendPendingCdsLogsUseCase:
    """CDS spool의 대기 로그 전체를 제한 크기의 배치로 전송한다."""

    _MAX_BATCH_SIZE = 1_000
    _MAX_RETRY_COUNT = 3

    def __init__(
        self,
        spool: AnalysisLogDeliveryQueuePort,
        delivery: AnalysisLogBatchDeliveryPort,
        *,
        audit: AuditNotificationPort | None = None,
        batch_size: int = _MAX_BATCH_SIZE,
        logger: logging.Logger | None = None,
    ) -> None:
        if not 1 <= batch_size <= self._MAX_BATCH_SIZE:
            raise ValueError(
                "CDS 배치 크기는 1~1000 범위여야 합니다.",
            )

        self._spool = spool
        self._delivery = delivery
        self._audit = audit
        self._batch_size = batch_size
        self._logger = logger or logging.getLogger("khan-python")

    def execute(self) -> int:
        """실행 시작 시점의 대기 로그를 모두 처리하고 삭제 수를 반환한다."""
        try:
            with self._spool.claim_pending() as batch:
                if batch.unreadable_count:
                    self._record_failure(
                        "CDS 로그 파일 읽기 실패",
                        failed_count=batch.unreadable_count,
                    )
                deleted_count = 0
                for offset in range(0, len(batch.entries), self._batch_size):
                    entries = list(
                        batch.entries[offset : offset + self._batch_size],
                    )
                    deleted_count += self._send_batch(entries)
                return deleted_count
        except Exception:
            self._record_failure("CDS 전송 작업 실패")
            return 0

    def _send_batch(self, entries: list[PendingAnalysisLog]) -> int:
        try:
            result = self._delivery.deliver([entry.analysis_log for entry in entries])
            self._validate_delivery_rows(entries, result.rows)
        except RetryableAnalysisLogDeliveryError:
            self._handle_retryable_failure(entries)
            return 0
        except Exception:
            quarantined_count = self._quarantine(entries)
            self._record_failure(
                "CDS 재시도 불가 오류로 로그 격리",
                failed_count=quarantined_count,
            )
            return 0

        succeeded_entries: list[PendingAnalysisLog] = []
        failed_entries: list[PendingAnalysisLog] = []

        for row in result.rows:
            entry = entries[row.index]

            if not row.succeeded:
                failed_entries.append(entry)
                continue

            succeeded_entries.append(entry)
            self._logger.info(
                "CDS 로그 전송 성공 receipt=%s event_key=%s",
                entry.receipt,
                entry.event_key,
            )

        try:
            deleted_count = self._spool.acknowledge(succeeded_entries)
        except Exception:
            deleted_count = 0

        delete_failed_count = len(succeeded_entries) - deleted_count
        if delete_failed_count:
            self._record_failure(
                "CDS 성공 파일 삭제 실패",
                failed_count=delete_failed_count,
            )

        if failed_entries:
            quarantined_count = self._quarantine(failed_entries)
            self._record_failure(
                "CDS 개별 로그 처리 실패로 격리",
                failed_count=quarantined_count,
            )

        return deleted_count

    def _handle_retryable_failure(
        self,
        entries: Sequence[PendingAnalysisLog],
    ) -> None:
        retry_entries = tuple(
            entry for entry in entries if entry.retry_count < self._MAX_RETRY_COUNT
        )
        exhausted_entries = tuple(
            entry for entry in entries if entry.retry_count >= self._MAX_RETRY_COUNT
        )

        if retry_entries:
            try:
                rescheduled_count = self._spool.reschedule(retry_entries)
            except Exception:
                rescheduled_count = 0
            self._record_failure(
                "CDS 서버 오류로 재시도 예약",
                failed_count=rescheduled_count,
            )

        if exhausted_entries:
            quarantined_count = self._quarantine(exhausted_entries)
            self._record_failure(
                "CDS 최대 재시도 횟수 초과로 격리",
                failed_count=quarantined_count,
            )

    def _quarantine(
        self,
        entries: Sequence[PendingAnalysisLog],
    ) -> int:
        try:
            return self._spool.quarantine(entries)
        except Exception:
            self._logger.exception("CDS 로그 격리 처리 실패")
            return 0

    @staticmethod
    def _validate_delivery_rows(
        entries: list[PendingAnalysisLog],
        rows: Sequence[AnalysisLogDeliveryRow],
    ) -> None:
        indexes = [row.index for row in rows]
        if len(rows) != len(entries) or set(indexes) != set(range(len(entries))):
            raise ValueError("분석 로그 batch 전달 결과가 요청과 일치하지 않습니다.")

    def _record_failure(
        self,
        message: str,
        *,
        failed_count: int | None = None,
    ) -> None:
        if failed_count is None:
            self._logger.error(message)
        else:
            self._logger.error(
                "%s failed_count=%s",
                message,
                failed_count,
            )

        if self._audit is None:
            return

        audit_message = f"nNetCDS: {message}"
        if failed_count is not None:
            audit_message += f", 실패 {failed_count}건"

        try:
            self._audit.notify(
                AuditEvent(
                    succeeded=False,
                    category=4,
                    source="nNetCDS",
                    message=audit_message,
                )
            )
        except Exception:
            # 감사로그 실패로 CDS 재시도 대상이 제거되면 안 된다.
            self._logger.exception("CDS 실패 감사로그 호출 오류")
