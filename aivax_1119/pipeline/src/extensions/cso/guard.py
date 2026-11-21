"""CSO 배포에서만 설치하는 입력 선행 가드와 차단 후처리."""

from __future__ import annotations

import logging
import os
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any

from anyio.to_thread import run_sync

from src.application.services.analysis_log_merge import AnalysisLogMergeService
from src.application.services.notification_attachment import (
    AttachmentNotificationService,
)
from src.application.use_cases.input_analysis.input_analysis_log_builder import (
    InputAnalysisLogBuilder,
)
from src.core.constants import ERR_OK
from src.core.file import LocalFileMetadataReader
from src.core.file.file_metadata import logged_extension
from src.core.time import TimeNormalizer
from src.domain.analysis_log_delivery.errors import RetryableAnalysisLogDeliveryError
from src.domain.analysis_log_delivery.ports.analysis_log_consent_writer_port import (
    AnalysisLogConsentWriterPort,
)
from src.domain.analysis_log_merge.errors import OutputLogProcessorUnavailableError
from src.domain.delivery_file.models import FileDeliveryRequest, FileDeliveryResult
from src.domain.delivery_file.ports import FileDeliveryPort
from src.domain.inspection_input.models import (
    InputAnalysisRequest,
    InputAnalysisResult,
    InputFinalDecision,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
)
from src.domain.inspection_input.models.block_phrases import get_block_phrases
from src.domain.inspection_input.models.cso_grade_result import (
    CsoGradeResult,
    CsoGradeStatus,
)
from src.domain.inspection_input.models.visible_block_log import (
    logged_block_output_for_result,
)
from src.domain.inspection_input.ports.cso_grade_port import CsoGradePort
from src.domain.inspection_input.ports.input_guard_port import (
    InputGuardPort,
    InputGuardResult,
)

# 공통 MIME 판독기가 반환한 문서 형식에 적용하는 fdoc-handler 지원 목록.
CSO_SUPPORTED_EXTENSIONS = frozenset(
    {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "hwp", "hwpx", "pdf"}
)


@dataclass(frozen=True, slots=True)
class CsoGuardResult(InputGuardResult):
    grades: tuple[CsoGradeResult, ...] = ()

    def apply(self, result: InputAnalysisResult) -> InputAnalysisResult:
        return replace(result, cso_grades=self.grades)


class CsoInputGuard(InputGuardPort):
    def __init__(
        self,
        reader: CsoGradePort,
        *,
        skip_unsupported_extensions_enabled: bool = False,
        skip_untagged_enabled: bool = False,
        skip_error_enabled: bool = False,
        file_delivery: FileDeliveryPort | None,
        input_log_builder: InputAnalysisLogBuilder,
        analysis_log_merge_service: AnalysisLogMergeService,
        consent_log_writer: AnalysisLogConsentWriterPort | None = None,
        attachment_notification_service: AttachmentNotificationService | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._cso_grade_reader = reader
        self._skip_unsupported_extensions_enabled = skip_unsupported_extensions_enabled
        self._skip_untagged_enabled = skip_untagged_enabled
        self._skip_error_enabled = skip_error_enabled
        self._file_delivery = file_delivery
        self._input_log_builder = input_log_builder
        self._analysis_log_merge_service = analysis_log_merge_service
        self._consent_log_writer = consent_log_writer
        self._attachment_notification_service = attachment_notification_service
        self._logger = logger or logging.getLogger("khan-python")

    async def check(
        self,
        request: InputAnalysisRequest,
        *,
        original_document: dict[str, Any] | None = None,
    ) -> InputGuardResult:
        previous = ()
        if original_document is not None and request.attachments:
            previous = self._input_log_builder.restore_executions(original_document)
            backup_paths = defaultdict(deque)
            for execution in previous:
                if execution.inspector_type is InputInspectorType.MULTIMODAL:
                    for summary in execution.details.get("file_summary", []):
                        if summary.get("file_backup_path"):
                            backup_paths[summary["file_name"]].append(
                                summary["file_backup_path"]
                            )
            request = replace(
                request,
                attachments=tuple(
                    replace(item, id=backup_paths[item.name].popleft())
                    if backup_paths[item.name]
                    else item
                    for item in request.attachments
                ),
            )
        # 경로가 없는 칸은 파일이 아니다.
        # 빈 문자열은 작업 디렉터리로 풀려 등급 조회가 실패한다.
        request = replace(
            request,
            attachments=tuple(
                item for item in request.attachments if str(item.id or "").strip()
            ),
        )
        grades = await self._read_cso_grades(request)
        if any(grade.is_blocking for grade in grades):
            return await self._execute_cso_block(
                request,
                grades,
                previous=previous,
                update_log=original_document is not None,
            )
        return CsoGuardResult(request, grades=grades)

    async def _execute_cso_block(
        self,
        request: InputAnalysisRequest,
        cso_grades: tuple[CsoGradeResult, ...],
        *,
        previous: tuple[InputInspectionResult, ...] = (),
        update_log: bool = False,
    ) -> InputGuardResult:
        """분석 없이 첨부파일을 보관하고 CSO 차단 로그를 전달한다."""
        if self._file_delivery is None:
            raise OutputLogProcessorUnavailableError()
        archived_files = defaultdict(deque)
        for execution in previous:
            if execution.inspector_type is InputInspectorType.MULTIMODAL:
                for summary in execution.details.get("file_summary", []):
                    if summary.get("file_backup_path"):
                        archived_files[summary["file_name"]].append(summary)
        summaries = []
        for attachment in request.attachments:
            if archived_files[attachment.name]:
                summaries.append(archived_files[attachment.name].popleft())
                continue
            source_path = Path(attachment.id)
            delivery = FileDeliveryResult()
            if attachment.id and await run_sync(source_path.is_file):
                delivery = await self._file_delivery.deliver(
                    FileDeliveryRequest(source_path, attachment.name)
                )
            file_info = {
                "mime_type": attachment.mime_type,
                "file_ext": Path(attachment.name).suffix.lstrip("."),
                "size": attachment.size,
                "inspection_status": "skipped",
                "skip_reason": "cso_blocked",
            }
            if delivery.delivered:
                try:
                    metadata = await run_sync(
                        LocalFileMetadataReader().read, delivery.destination
                    )
                    file_info.update(
                        mime_type=metadata.mime_type,
                        file_ext=logged_extension(
                            attachment.name, metadata.extension
                        ),
                        size=metadata.size_bytes,
                    )
                except Exception:
                    self._logger.exception(
                        "CSO 차단 파일 메타데이터 조회 실패 path=%s",
                        delivery.destination,
                    )
                if self._attachment_notification_service:
                    await run_sync(
                        partial(
                            self._attachment_notification_service.enqueue_archived_file,
                            message_id=request.message_id,
                            source_ip=request.user.client_host,
                            occurred_at=request.event_time,
                            file_name=attachment.name,
                            file_backup_path=delivery.destination,
                        )
                    )
            summaries.append(
                {
                    "action": "",
                    "file_name": attachment.name,
                    "file_backup_path": delivery.destination
                    if delivery.delivered
                    else "",
                    "file_info": file_info,
                }
            )
        result = self._apply_cso_result(
            InputAnalysisResult(
                executions=previous,
                final_decision=InputFinalDecision(action=InputInspectionAction.BLOCK),
            ),
            cso_grades,
        )
        # CSO로 실행하지 않은 필터도 로그에는 빈 기본값을 남긴다.
        log_executions = {
            kind: InputInspectionResult(
                inspector_type=kind,
                details={"slm_content": ""} if kind is InputInspectorType.SLM else {},
            )
            for kind in (
                InputInspectorType.REGEX,
                InputInspectorType.MULTIMODAL,
                InputInspectorType.SLM,
            )
        }
        log_executions.update((item.inspector_type, item) for item in previous)
        document = self._input_log_builder.build(
            request,
            replace(result, executions=tuple(log_executions.values())),
            include_block_messages=True,
            file_summaries=summaries,
        )
        if update_log:
            await run_sync(
                self._consent_log_writer.write_analysis_update,
                request.message_id,
                {
                    "filter_detect": document["filter_detect"],
                    "cso": document["cso"],
                    "mode": "block",
                    "masked_contents": None,
                    "output_text": logged_block_output_for_result(
                        result,
                        request.prompt,
                        ai_service_id=request.user.ai_service,
                    ),
                },
            )
        else:
            await self._deliver_blocked_final_log(
                result, document, require_delivery=True
            )
        return InputGuardResult(request, result, document)

    async def _read_cso_grades(
        self,
        request: InputAnalysisRequest,
    ) -> tuple[CsoGradeResult, ...]:
        if self._cso_grade_reader is None or not request.attachments:
            return ()
        skipped = CsoGradeResult(
            grade=None,
            status=CsoGradeStatus.SKIPPED,
        )
        grades = [skipped for _ in request.attachments]
        indices = []
        for index, attachment in enumerate(request.attachments):
            if not attachment.id:
                continue
            if await self._is_unsupported_file_type(attachment.id):
                grades[index] = self._apply_skip_options(
                    CsoGradeResult(None, CsoGradeStatus.ERROR, unsupported=True)
                )
            else:
                indices.append(index)
        if not indices:
            return tuple(grades)
        try:
            paths = tuple(request.attachments[index].id for index in indices)
            if self._logger.isEnabledFor(logging.DEBUG):
                for index in indices:
                    attachment = request.attachments[index]
                    self._logger.debug(
                        "Starting CSO file lookup message_id=%r file_index=%s "
                        "file_name=%r file_path=%r absolute_path=%r",
                        request.message_id,
                        index,
                        attachment.name,
                        attachment.id,
                        os.path.abspath(attachment.id),
                    )
            results = await self._cso_grade_reader.read_grades(paths)
            if len(results) != len(paths):
                raise ValueError("CSO 조회 결과 수가 입력 파일 수와 다릅니다.")
        except Exception:
            # 실행·파싱 등 모든 조회 예외는 같은 상태로 처리한다.
            self._logger.exception(
                "CSO 등급 조회 실패 message_id=%s", request.message_id
            )
            results = tuple(
                CsoGradeResult(grade=None, status=CsoGradeStatus.ERROR) for _ in indices
            )
        for index, result in zip(indices, results, strict=True):
            grades[index] = self._apply_skip_options(result)
        return tuple(grades)

    def _apply_skip_options(self, result: CsoGradeResult) -> CsoGradeResult:
        if (
            (self._skip_unsupported_extensions_enabled and result.unsupported)
            or (
                self._skip_untagged_enabled and result.status is CsoGradeStatus.UNTAGGED
            )
            or (self._skip_error_enabled and result.status is CsoGradeStatus.ERROR)
        ):
            return CsoGradeResult(None, CsoGradeStatus.SKIPPED)
        return result

    async def _is_unsupported_file_type(self, path: str) -> bool:
        if not self._cso_grade_reader.supports_mime_skip:
            return False
        try:
            metadata = await run_sync(LocalFileMetadataReader().read, path)
        except Exception:
            self._logger.warning(
                "CSO MIME detection failed; continuing grade lookup path=%r",
                path,
                exc_info=True,
            )
            return False
        return metadata.extension.lower() not in CSO_SUPPORTED_EXTENSIONS

    @staticmethod
    def _apply_cso_result(
        result: InputAnalysisResult,
        cso_grades: tuple[CsoGradeResult, ...],
    ) -> InputAnalysisResult:
        if not cso_grades:
            return result
        if not any(grade.is_blocking for grade in cso_grades):
            return replace(result, cso_grades=cso_grades)

        phrases = get_block_phrases()
        messages = [phrases.cso_title, ""]
        if any(grade.grade in (1, 2) for grade in cso_grades):
            messages.append(phrases.cso_cs)
        if any(grade.status is CsoGradeStatus.UNTAGGED for grade in cso_grades):
            messages.append(phrases.cso_untagged)
        if any(grade.status is CsoGradeStatus.ERROR for grade in cso_grades):
            messages.append(phrases.cso_error)
        # 분석 판정과 정책은 유지하고 CSO 안내를 별도로 보관한다.
        return replace(
            result,
            cso_grades=cso_grades,
            cso_block_message="\n".join(messages),
        )

    async def _deliver_blocked_final_log(
        self,
        result: InputAnalysisResult,
        input_document: dict[str, Any],
        *,
        require_delivery: bool = False,
    ) -> None:
        completed_at = TimeNormalizer.utc_now()
        completed_document = dict(input_document)
        completed_document.update(
            {
                "@timestamp": TimeNormalizer.to_utc_iso(completed_at),
                "output_text": logged_block_output_for_result(
                    result,
                    str(input_document.get("prompt") or ""),
                    ai_service_id=int(input_document.get("ai_service_id") or 0),
                ),
                "respose_date": completed_at.astimezone().strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
        # 차단 응답도 최종 output이 채워진 분석 로그다. 같은 delivery service를
        # 통과해야 사용자·정책 이벤트가 일관되게 final delivery 뒤에 발행된다.
        delivered = await run_sync(
            self._analysis_log_merge_service.deliver, completed_document
        )
        if require_delivery and delivered != ERR_OK:
            raise RetryableAnalysisLogDeliveryError(
                "최초 차단 로그 기록에 실패했습니다."
            )
