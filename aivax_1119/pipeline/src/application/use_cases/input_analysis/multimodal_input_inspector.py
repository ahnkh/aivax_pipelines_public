"""공용 멀티모달 분석 결과를 input-analysis 결과로 변환한다."""

from functools import partial

from anyio.to_thread import run_sync

from src.application.services.inspection_multimodal import (
    MultimodalInspectionService,
)
from src.application.services.multimodal_inspection_result_formatter import (
    format_multimodal_matches,
)
from src.application.services.notification_attachment import (
    AttachmentNotificationService,
)
from src.core.file.file_metadata import logged_extension
from src.domain.delivery_file.models import FileDeliveryRequest
from src.domain.delivery_file.ports import FileDeliveryPort
from src.domain.inspection_input.models import (
    InputAnalysisRequest,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
    InputPolicyReference,
)
from src.domain.inspection_input.models.block_phrases import get_block_phrases
from src.domain.inspection_input.ports import InputInspectorPort
from src.domain.inspection_multimodal.models import (
    DocumentSource,
    MultimodalFileInspectionResult,
    MultimodalFileLimitReason,
    MultimodalInspectionTarget,
    MultimodalMatchedPolicy,
)


class MultimodalInputInspector(InputInspectorPort):
    """multiple_filter의 첨부파일 계약과 공용 멀티모달 분석을 연결한다."""

    def __init__(
        self,
        inspection_service: MultimodalInspectionService,
        file_delivery: FileDeliveryPort,
        attachment_notification_service: AttachmentNotificationService | None = None,
        *,
        backup_skipped_oversized_files: bool = True,
    ) -> None:
        self._inspection_service = inspection_service
        self._file_delivery = file_delivery
        self._attachment_notification_service = attachment_notification_service
        self._backup_skipped_oversized_files = backup_skipped_oversized_files

    async def inspect(self, request: InputAnalysisRequest) -> InputInspectionResult:
        targets = tuple(
            MultimodalInspectionTarget(
                source=DocumentSource.from_path(attachment.id),
                file_name=attachment.name,
            )
            for attachment in request.attachments
            if attachment.id and attachment.name
        )
        result = await self._inspection_service.inspect(
            targets,
            email=request.user.email,
            ai_service_id=request.user.ai_service,
        )
        summaries: list[dict[str, object]] = []
        for item in result.files:
            destination = ""
            if not item.inspection_status or (
                self._backup_skipped_oversized_files
                and item.limit_reason is MultimodalFileLimitReason.OVERSIZED
            ):
                delivery = await self._file_delivery.deliver(
                    FileDeliveryRequest(
                        source_path=item.source.path,
                        file_name=item.file_name,
                    )
                )
                destination = delivery.destination
                if delivery.delivered and self._attachment_notification_service:
                    await run_sync(
                        partial(
                            self._attachment_notification_service.enqueue_archived_file,
                            message_id=request.message_id,
                            source_ip=request.user.client_host,
                            occurred_at=request.event_time,
                            file_name=item.file_name,
                            file_backup_path=destination,
                        ),
                    )
            summaries.append(self._to_summary(item, destination))
        return InputInspectionResult(
            inspector_type=InputInspectorType.MULTIMODAL,
            action=self._input_action(result.action),
            block_message=(
                self._block_message(
                    result.selected_policy.name if result.selected_policy else ""
                )
                if result.action == "block"
                else ""
            ),
            policy=self._policy_reference(result.selected_policy),
            evidence=tuple(
                match
                for item in result.files
                for match in format_multimodal_matches(item)
            ),
            details={"file_summary": summaries},
        )

    @staticmethod
    def _block_message(policy_name: str) -> str:
        return get_block_phrases().file_block(policy_name)

    def _to_summary(
        self,
        result: MultimodalFileInspectionResult,
        destination: str,
    ) -> dict[str, object]:
        policy = result.policy
        summary: dict[str, object] = {
            "action": result.action,
            "policy_id": policy.id if policy else "",
            "policy_name": policy.name if policy else "",
            "file_name": result.file_name,
            "file_backup_path": destination,
            "name": policy.name if policy else "",
            "targets": policy.target if policy else "",
            "category": policy.category if policy else "",
            "scope": policy.scope if policy else "",
            "regex_pattern": policy.matched_text if policy else "",
            "matches": list(format_multimodal_matches(result)),
            "file_info": {
                "mime_type": result.metadata.mime_type,
                "file_ext": logged_extension(
                    result.file_name, result.metadata.extension
                ),
                "size": result.metadata.size_bytes,
            },
        }
        if policy is not None:
            summary["id"] = policy.id
        file_info = summary["file_info"]
        assert isinstance(file_info, dict)
        if result.inspection_status:
            file_info.update(
                {
                    "inspection_status": result.inspection_status,
                    "skip_reason": result.skip_reason,
                }
            )
        if result.content_inspected:
            file_info["hash"] = result.file_hash
        if result.content_chunk is not None:
            file_info["chunk"] = result.content_chunk
        if policy is not None and policy.expression:
            summary["detail_reason"] = {
                "policy_detail": {
                    "name": policy.name,
                    "rule": policy.expression,
                    "regex_flag": policy.flags,
                },
                "evidence": [
                    {
                        "no": item.sequence,
                        "page_no": item.page_number,
                        "line_no": item.line_number,
                        "context": item.context,
                        "regex_match": item.matched_text,
                    }
                    for item in result.evidence
                ],
            }
        return summary

    @staticmethod
    def _input_action(value: str) -> InputInspectionAction:
        if value == "block":
            return InputInspectionAction.BLOCK
        if value == InputInspectionAction.ACCEPT.value:
            return InputInspectionAction.ACCEPT
        return InputInspectionAction.BLANK

    @staticmethod
    def _policy_reference(
        policy: MultimodalMatchedPolicy | None,
    ) -> InputPolicyReference | None:
        if policy is None:
            return None
        return InputPolicyReference(
            id=policy.id,
            name=policy.name,
            target=policy.target,
            category=policy.category,
            scope=policy.scope,
        )
