"""동의 상태와 증적 변환 임시파일을 함께 정리한다."""

import logging

from src.domain.analysis_log_delivery.ports.analysis_log_consent_writer_port import (
    AnalysisLogConsentWriterPort,
)
from src.domain.analysis_log_delivery.ports.analysis_log_delivery_port import (
    AnalysisLogDeliveryPort,
)
from src.domain.inspection_multimodal.ports import MultimodalInspectionPort


class CleanupPipelineFilesUseCase:
    def __init__(
        self,
        writer: AnalysisLogConsentWriterPort,
        inspection: MultimodalInspectionPort,
        evidence_directory: str,
        *,
        log_delivery: AnalysisLogDeliveryPort,
        log_backup_directory: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self._writer = writer
        self._inspection = inspection
        self._evidence_directory = evidence_directory
        self._log_delivery = log_delivery
        self._log_backup_directory = log_backup_directory
        self._logger = logger or logging.getLogger("khan-python")

    def execute(self, *, archive_bulk: bool = False) -> dict[str, int]:
        counts = {"consent_states": 0, "evidence_entries": 0}
        try:
            counts["consent_states"] = self._writer.expire_consent_states()
        except Exception:
            self._logger.exception("consent state cleanup failed")
        try:
            counts["evidence_entries"] = self._inspection.expire_temporary_evidence(
                self._evidence_directory
            )
        except Exception:
            self._logger.exception("temporary evidence cleanup failed")
        if archive_bulk:
            counts["bulk_logs"] = 0
            try:
                counts["bulk_logs"] = self._log_delivery.archive_bulk_logs(
                    self._log_backup_directory
                )
            except Exception:
                self._logger.exception("bulk log archive failed")
        if any(counts.values()):
            self._logger.info("pipeline cleanup completed %s", counts)
        return counts
