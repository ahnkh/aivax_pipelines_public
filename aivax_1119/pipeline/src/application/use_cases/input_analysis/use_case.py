"""multiple_filter 입력 분석 유스케이스."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any

from anyio.to_thread import run_sync

from src.application.services.analysis_log_merge import AnalysisLogMergeService
from src.application.use_cases.input_analysis.input_analysis_log_builder import (
    InputAnalysisLogBuilder,
)
from src.core.constants import ERR_OK
from src.core.time import TimeNormalizer
from src.domain.analysis_log_delivery.errors import RetryableAnalysisLogDeliveryError
from src.domain.analysis_log_delivery.errors.consent_log_not_found_error import (
    ConsentLogNotFoundError,
)
from src.domain.analysis_log_delivery.models.user_consent import (
    CONSENT_LOG_TIMESTAMP_FIELD,
)
from src.domain.analysis_log_delivery.ports.analysis_log_consent_writer_port import (
    AnalysisLogConsentWriterPort,
)
from src.domain.analysis_log_merge.errors import OutputLogProcessorUnavailableError
from src.domain.config_aivax.ports import AivaxConfigPort
from src.domain.inspection_input.errors import UnsupportedInputInspectorError
from src.domain.inspection_input.models.visible_block_log import (
    logged_block_output_for_result,
)
from src.domain.inspection_input.models import (
    INPUT_LOG_REQUEST_NAME,
    InputAnalysisOptions,
    InputAnalysisRequest,
    InputAnalysisResult,
    InputFinalDecision,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
)
from src.domain.inspection_input.models.input_analysis_result import (
    InputInspectionError,
)
from src.domain.inspection_input.models.masking import DEFAULT_MASK_TOKEN
from src.domain.inspection_input.ports import InputInspectorPort
from src.domain.inspection_input.ports.input_guard_port import (
    InputGuardPort,
    InputGuardResult,
)
from src.domain.inspection_input.services import InputFinalDecisionResolver


@dataclass(frozen=True, slots=True)
class InputAnalysisOutcome:
    """입력 분석 유스케이스의 실행 결과."""

    analysis_result: InputAnalysisResult
    log_evidence: Mapping[str, Any] | None = None
    debug_details_enabled: bool = True


class InputAnalysisUseCase:
    """multiple_filter 한 건의 전체 흐름을 순서대로 오케스트레이션한다."""

    _SUPPORTED_FILTER_NAMES = frozenset(
        {
            INPUT_LOG_REQUEST_NAME,
            InputInspectorType.REGEX.value,
            InputInspectorType.MULTIMODAL.value,
            InputInspectorType.SLM.value,
            InputInspectorType.SECUXPER.value,
        }
    )

    def __init__(
        self,
        *,
        regex_inspector: InputInspectorPort,
        multimodal_inspector: InputInspectorPort,
        slm_inspector: InputInspectorPort,
        options: InputAnalysisOptions,
        input_log_builder: InputAnalysisLogBuilder,
        analysis_log_merge_service: AnalysisLogMergeService,
        aivax_config: AivaxConfigPort,
        consent_log_writer: AnalysisLogConsentWriterPort | None = None,
        secuxper_inspector: InputInspectorPort | None = None,
        input_guard: InputGuardPort | None = None,
        decision_resolver: InputFinalDecisionResolver | None = None,
        mask_token: str = DEFAULT_MASK_TOKEN,
        logger: logging.Logger | None = None,
    ) -> None:
        self._regex_inspector = regex_inspector
        self._multimodal_inspector = multimodal_inspector
        self._slm_inspector = slm_inspector
        self._secuxper_inspector = secuxper_inspector
        self._input_guard = input_guard
        self._options = options
        self._input_log_builder = input_log_builder
        self._analysis_log_merge_service = analysis_log_merge_service
        self._aivax_config = aivax_config
        self._consent_log_writer = consent_log_writer
        self._decision_resolver = decision_resolver or InputFinalDecisionResolver()
        self._mask_token = mask_token
        self._logger = logger or logging.getLogger("khan-python")
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def execute(
        self,
        request: InputAnalysisRequest,
    ) -> InputAnalysisOutcome:
        self._validate_requested_filters(request)
        if self._is_mirror_mode():
            # Mirror는 수집만 수행하며 선행 가드와 동의 허용 경로를 적용하지 않는다.
            # proxy는 mirror가 아니므로 아래 동기 판정(차단·마스킹)을 탄다.
            return self._accept_mirror(request)
        if request.user_consent:
            return await self._execute_consent(request)

        guard = await self._check_guard(request)
        if guard.blocked_result is not None:
            return InputAnalysisOutcome(guard.blocked_result, guard.log_evidence)
        return await self._execute_inline(guard.request, guard)

    async def _check_guard(
        self,
        request: InputAnalysisRequest,
        original_document: dict[str, Any] | None = None,
    ) -> InputGuardResult:
        if self._input_guard is None:
            return InputGuardResult(request)
        return await self._input_guard.check(
            request, original_document=original_document
        )

    async def _execute_consent(
        self, request: InputAnalysisRequest
    ) -> InputAnalysisOutcome:
        """선행 가드를 통과한 동의 입력을 로그 병합 저장소에 적재한다."""
        writer = self._consent_log_writer
        original_document = None
        analysis_request = request
        if writer is not None:
            try:
                document = await run_sync(writer.read_block_log, request.message_id)
            except ConsentLogNotFoundError:
                pass
            else:
                original_document = document
        guard = await self._check_guard(request, original_document)
        if guard.blocked_result is not None:
            return InputAnalysisOutcome(guard.blocked_result, guard.log_evidence)
        analysis_request = guard.request
        result = guard.apply(
            InputAnalysisResult(
                executions=(),
                final_decision=InputFinalDecision(action=InputInspectionAction.ACCEPT),
                write_input_log=True,
            )
        )
        document = self._input_log_builder.build(analysis_request, result)
        document["user_consent"] = "on"
        if original_document is not None:
            timestamp = original_document["@timestamp"]
            document["@timestamp"] = timestamp
            document[CONSENT_LOG_TIMESTAMP_FIELD] = timestamp
        await self._record_input_log(request, document)
        return InputAnalysisOutcome(analysis_result=result, log_evidence=document)

    def _validate_requested_filters(self, request: InputAnalysisRequest) -> None:
        unsupported_filter_names = (
            set(request.requested_filter_names) - self._SUPPORTED_FILTER_NAMES
        )
        if unsupported_filter_names:
            raise UnsupportedInputInspectorError(
                sorted(unsupported_filter_names)[0],
            )

    def _is_mirror_mode(self) -> bool:
        try:
            return self._aivax_config.get_config().is_mirror
        except Exception:
            # 설정 조회 실패로 요청을 무조건 허용하지 않도록 inline으로 처리한다.
            self._logger.exception(
                "AIVAX mode 조회 실패; inline 모드로 처리합니다",
            )
            return False

    async def _execute_inline(
        self,
        request: InputAnalysisRequest,
        guard: InputGuardResult,
    ) -> InputAnalysisOutcome:
        result = await self._analyze(request, self._options)
        result = guard.apply(result)
        log_evidence = await self._process_inline_analysis_log(request, result)
        return InputAnalysisOutcome(
            analysis_result=result,
            log_evidence=log_evidence,
        )

    def _accept_mirror(
        self,
        request: InputAnalysisRequest,
    ) -> InputAnalysisOutcome:
        task = asyncio.create_task(
            self._execute_mirror_background(request),
            name=f"mirror-input-analysis:{request.message_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        # mirror의 accept는 분석 판정이 아니라 요청 접수를 의미한다.
        accepted_result = InputAnalysisResult(
            executions=(),
            final_decision=self._decision_resolver.resolve((), self._options),
            write_input_log=False,
            stopped_after_block=False,
        )
        return InputAnalysisOutcome(
            analysis_result=accepted_result,
            debug_details_enabled=False,
        )

    async def _execute_mirror_background(
        self,
        request: InputAnalysisRequest,
    ) -> None:
        try:
            # Mirror는 응답을 즉시 허용할 뿐, 분석 규칙은 inline과 같다.
            # 어느 inspector든 block을 반환하면 이후 inspector를 호출하지 않는다.
            result = await self._analyze(request, self._options)
            await self._process_mirror_analysis_log(request, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            # HTTP 응답은 이미 반환됐으므로 분석 실패는 로그로만 남긴다.
            self._logger.exception(
                "mirror 입력 백그라운드 분석 실패 message_id=%s",
                request.message_id,
            )

    async def shutdown(self) -> None:
        """프로세스 종료 전에 실행 중인 mirror 분석을 마무리한다."""
        tasks = tuple(self._background_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _analyze(
        self,
        request: InputAnalysisRequest,
        options: InputAnalysisOptions,
    ) -> InputAnalysisResult:
        executions: list[InputInspectionResult] = []
        stopped_after_block = False

        # 실행 우선순위는 요청 배열의 순서와 무관하게 고정한다.
        # 차단 뒤의 필터는 호출하지 않지만 Management 로그에는 빈 기본값을 남긴다.
        inspectors = dict(self._inspectors())
        for inspector_type in self._requested_plan(request):
            inspector = inspectors[inspector_type]
            if inspector is None:
                raise OutputLogProcessorUnavailableError()
            if stopped_after_block:
                executions.append(self._empty_execution(inspector_type))
                continue
            execution = await self._inspect(inspector, request, inspector_type)
            executions.append(execution)
            stopped_after_block = self._is_blocked(execution)

        execution_results = tuple(executions)
        return InputAnalysisResult(
            executions=execution_results,
            final_decision=self._decision_resolver.resolve(
                execution_results, options, request.prompt, mask_token=self._mask_token
            ),
            write_input_log=(INPUT_LOG_REQUEST_NAME in request.requested_filter_names),
            stopped_after_block=stopped_after_block,
        )

    def _inspectors(
        self,
    ) -> tuple[tuple[InputInspectorType, InputInspectorPort | None], ...]:
        return (
            (InputInspectorType.REGEX, self._regex_inspector),
            (InputInspectorType.MULTIMODAL, self._multimodal_inspector),
            (InputInspectorType.SLM, self._slm_inspector),
            (InputInspectorType.SECUXPER, self._secuxper_inspector),
        )

    def _execution_plan(self) -> tuple[InputInspectorType, ...]:
        filters = self._aivax_config.get_config().filters
        filter_settings = {
            InputInspectorType.REGEX: filters.regex,
            InputInspectorType.MULTIMODAL: filters.multimodal,
            InputInspectorType.SLM: filters.slm,
            InputInspectorType.SECUXPER: filters.secuxper,
        }
        return tuple(
            kind
            for kind, inspector in self._inspectors()
            if inspector is not None and filter_settings[kind].enabled
        )

    def _requested_plan(
        self, request: InputAnalysisRequest
    ) -> tuple[InputInspectorType, ...]:
        return tuple(
            kind
            for kind in self._execution_plan()
            if kind.value in request.requested_filter_names
        )

    @staticmethod
    def _is_blocked(
        execution: InputInspectionResult,
    ) -> bool:
        return execution.action is InputInspectionAction.BLOCK

    @staticmethod
    def _empty_execution(
        inspector_type: InputInspectorType,
    ) -> InputInspectionResult:
        """호출하지 않은 필터의 Management 로그 기본값을 만든다."""
        details: dict[str, object] = {}
        if inspector_type is InputInspectorType.MULTIMODAL:
            details["file_summary"] = []
        elif inspector_type is InputInspectorType.SLM:
            details["slm_content"] = ""
        return InputInspectionResult(
            inspector_type=inspector_type,
            details=details,
        )

    async def _inspect(
        self,
        inspector: InputInspectorPort,
        request: InputAnalysisRequest,
        inspector_type: InputInspectorType,
    ) -> InputInspectionResult:
        started_at = TimeNormalizer.utc_now()
        started_at_counter = perf_counter()
        try:
            execution = await inspector.inspect(request)
        except Exception:
            self._logger.exception(
                "input filter failed filter=%s", inspector_type.value
            )
            execution = InputInspectionResult(
                inspector_type=inspector_type,
                action=InputInspectionAction.FAILED,
                error=InputInspectionError(
                    "INTERNAL_ERROR", "필터 검사 처리 중 오류가 발생했습니다."
                ),
            )
        return replace(
            execution,
            started_at=started_at,
            completed_at=TimeNormalizer.utc_now(),
            duration_ms=round((perf_counter() - started_at_counter) * 1000, 3),
        )

    async def _process_inline_analysis_log(
        self,
        request: InputAnalysisRequest,
        result: InputAnalysisResult,
    ) -> dict[str, Any] | None:
        is_blocked = result.final_decision.action is InputInspectionAction.BLOCK
        if not result.write_input_log and not is_blocked:
            return None

        if is_blocked and self._options.user_consent_global_bypass_enabled:
            input_document = self._input_log_builder.build(
                request, result, include_block_messages=True
            )
        else:
            input_document = self._input_log_builder.build(request, result)
        if is_blocked:
            await self._deliver_blocked_final_log(
                result,
                input_document,
                require_delivery=self._options.user_consent_global_bypass_enabled,
            )
        else:
            await self._record_input_log(request, input_document)
        return input_document

    async def _process_mirror_analysis_log(
        self,
        request: InputAnalysisRequest,
        result: InputAnalysisResult,
    ) -> dict[str, Any] | None:
        is_blocked = result.final_decision.action is InputInspectionAction.BLOCK
        if not result.write_input_log and not is_blocked:
            return None

        input_document = self._input_log_builder.build(request, result)
        # mirror는 실제 요청을 차단하지 않았으므로 block 판정이어도 가상
        # output을 만들지 않는다. 실제 Output API 로그와 병합한다.
        await self._record_input_log(request, input_document)
        return input_document

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

    async def _record_input_log(
        self,
        request: InputAnalysisRequest,
        input_document: dict[str, Any],
    ) -> None:
        # INPUT/OUTPUT 도착 순서와 최종 delivery 후 이벤트 발행은 모두
        # AnalysisLogMergeService가 소유한다.
        stored = await run_sync(
            self._analysis_log_merge_service.record_input,
            request.message_id,
            input_document,
        )
        if request.user_consent and stored != ERR_OK:
            raise RetryableAnalysisLogDeliveryError(
                "동의 입력 로그 병합 저장에 실패했습니다."
            )
