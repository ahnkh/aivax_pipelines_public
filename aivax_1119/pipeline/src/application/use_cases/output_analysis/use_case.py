"""출력 처리와 분석 로그 병합을 오케스트레이션한다."""

from typing import Any

from anyio.to_thread import run_sync

from src.application.services.analysis_log_merge import AnalysisLogMergeService
from src.application.services.notification_attachment.service import (
    AttachmentNotificationService,
)
from src.core.constants import ERR_OK
from src.core.time import TimeNormalizer
from src.domain.analysis_log_delivery.errors import RetryableAnalysisLogDeliveryError
from src.domain.analysis_log_merge.errors import (
    OutputLogProcessorUnavailableError,
)
from src.domain.analysis_log_merge.models.output_log import OutputLogInput
from src.domain.analysis_log_merge.models.output_log_options import OutputLogOptions
from src.domain.config_ai_service.ports import AiServiceCatalogPort

_DEFAULT_OUTPUT_OPTIONS = OutputLogOptions()


class OutputAnalysisUseCase:
    def __init__(
        self,
        analysis_log_merge_service: AnalysisLogMergeService,
        options: OutputLogOptions | None = _DEFAULT_OUTPUT_OPTIONS,
        attachment_notification_service: AttachmentNotificationService | None = None,
        *,
        ai_service_catalog: AiServiceCatalogPort,
    ) -> None:
        self._ai_service_catalog = ai_service_catalog
        self._analysis_log_merge_service = analysis_log_merge_service
        self._options = options
        self._attachment_notification_service = attachment_notification_service

    def configure(self, options: OutputLogOptions | None) -> None:
        self._options = options

    async def execute(self, data: OutputLogInput) -> dict[str, Any]:
        analysis_log = self._build_analysis_log(data)
        if data.output_revision:
            await run_sync(
                self._analysis_log_merge_service.revise_output,
                data.message_id,
                analysis_log,
            )
            return {}
        # 기존 threadpool 경계를 유지해 동기 파일 저장이 이벤트 루프를 막지 않게 한다.
        # 일반 출력의 병합 실패 반환값은 기존처럼 HTTP 실패로 바꾸지 않는다.
        stored = await run_sync(self._store_output, data, analysis_log)
        if data.user_consent and stored != ERR_OK:
            raise RetryableAnalysisLogDeliveryError(
                "동의 출력 로그 병합 저장에 실패했습니다."
            )
        return {}

    def _store_output(self, data: OutputLogInput, analysis_log: dict[str, Any]) -> int:
        self._enqueue_generated_files(data, analysis_log)
        return self._analysis_log_merge_service.record_output(
            data.message_id,
            analysis_log,
        )

    def _enqueue_generated_files(
        self,
        data: OutputLogInput,
        analysis_log: dict[str, Any],
    ) -> None:
        service = self._attachment_notification_service
        if service is None:
            return
        occurred_at = str(analysis_log.get("@timestamp") or "")
        for item in data.generated_files:
            service.enqueue_archived_file(
                message_id=data.message_id,
                file_name=str(item.get("file_name") or ""),
                file_backup_path=str(item.get("file_backup_path") or ""),
                source_ip=data.client_host,
                occurred_at=occurred_at,
            )

    def _build_analysis_log(self, data: OutputLogInput) -> dict[str, Any]:
        options = self._options
        if options is None:
            raise OutputLogProcessorUnavailableError()

        ai_service_name = self._ai_service_catalog.name_for(data.ai_service)

        text = None
        size = 0
        if options.store_response_text:
            text = data.llm_output
            encoded = text.encode("utf-8", errors="ignore")
            size = len(text)
            if (
                options.response_max_bytes > 0
                and len(encoded) > options.response_max_bytes
            ):
                text = encoded[: options.response_max_bytes].decode(
                    "utf-8",
                    errors="ignore",
                )
                size = len(encoded)

        now = TimeNormalizer.utc_now()
        timestamp = TimeNormalizer.to_utc_iso(now)
        if data.output_revision and (data.logged_at or "").strip():
            timestamp = TimeNormalizer.to_utc_iso(
                data.logged_at.strip(), timespec="microseconds"
            )
        document: dict[str, Any] = {
            "@timestamp": timestamp,
            "message_id": data.message_id,
            "output_text": text,
            "text_truncated_bytes": size,
            "model": (data.model or "").strip() or None,
            "latency_ms": None,
            "tokens": (
                {"prompt": None, "completion": None, "total": None}
                if options.include_usage
                else None
            ),
            "user_id": data.user_id,
            "user_email": data.email,
            "user_uuid": "",
            "client_host": data.client_host,
            # 병합 뒤 사용자 동기화 이벤트가 사용할 요청 원본 서비스 ID다.
            "ai_service_id": data.ai_service,
            "ai_service": ai_service_name,
            "respose_date": now.astimezone().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if data.user_consent:
            # Engine marks the post-consent model reply so Pipeline can patch
            # the original BLOCK row even when no consent INPUT is pending.
            document["user_consent"] = "on"
        if data.generated_files:
            document["generated_files"] = [dict(item) for item in data.generated_files]
        if data.generated_only:
            document["generated_only"] = True
        return document
