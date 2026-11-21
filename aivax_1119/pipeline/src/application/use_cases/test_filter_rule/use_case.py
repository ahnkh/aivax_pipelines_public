"""운영 정책을 변경하지 않고 regex·SLM rule을 시험하는 유스케이스."""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import perf_counter

from anyio.to_thread import run_sync

from src.application.services.inspection_multimodal import MultimodalInspectionService
from src.application.services.inspection_slm import SlmInspectionService
from src.application.services.multimodal_inspection_result_formatter import (
    format_multimodal_matches,
)
from src.domain.inspection_input.models.block_phrases import get_block_phrases
from src.domain.inspection_input.models.masking import DEFAULT_MASK_TOKEN
from src.domain.inspection_multimodal.models import (
    DocumentSource,
    MultimodalInspectionTarget,
)
from src.domain.inspection_regex.models import RegexInspectionRequest
from src.domain.inspection_regex.ports import RegexInspectionPort
from src.domain.policy_multimodal.models import MultimodalPolicySnapshot
from src.domain.policy_regex.models import RegexPolicySnapshot
from src.domain.policy_slm.models import SlmPolicySnapshot


@dataclass(frozen=True, slots=True)
class FilterRuleTestAttachment:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class FilterRuleTestRequest:
    prompt: str
    attachments: tuple[FilterRuleTestAttachment, ...] = ()
    email: str = ""
    ai_service_id: int | str = 0
    regex_policy: RegexPolicySnapshot | None = None
    multimodal_policy: MultimodalPolicySnapshot | None = None
    slm_policy: SlmPolicySnapshot | None = None


@dataclass(frozen=True, slots=True)
class FilterRuleTestExecution:
    action: str
    content: str | None = None
    block_message: str | None = None
    policy_id: str = ""
    policy_name: str = ""
    elapsed_ms: float = 0
    evidence: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class FilterRuleTestMultimodalExecution:
    action: str
    policy_id: str = ""
    policy_name: str = ""
    files: tuple[dict[str, object], ...] = ()
    elapsed_ms: float = 0


@dataclass(frozen=True, slots=True)
class FilterRuleTestResult:
    regex_result: FilterRuleTestExecution | None = None
    multimodal_result: FilterRuleTestMultimodalExecution | None = None
    slm_result: FilterRuleTestExecution | None = None
    final_decision: FilterRuleTestExecution | None = None


class FilterRuleTestUseCase:
    """요청 snapshot에 포함된 검사를 운영과 같은 우선순위로 실행한다."""

    _POLICY_NAME = "정책 테스트"

    def __init__(
        self,
        regex_inspection: RegexInspectionPort,
        multimodal_inspection_service: MultimodalInspectionService,
        slm_inspection_service: SlmInspectionService,
        *,
        mask_token: str = DEFAULT_MASK_TOKEN,
    ) -> None:
        self._regex_inspection = regex_inspection
        self._multimodal_inspection_service = multimodal_inspection_service
        self._slm_inspection_service = slm_inspection_service
        self._mask_token = mask_token

    async def execute(self, request: FilterRuleTestRequest) -> FilterRuleTestResult:
        regex_result = None
        multimodal_result = None
        slm_result = None

        stopped_after_block = False
        if request.regex_policy is not None:
            started = perf_counter()
            regex_result = await self._test_regex(request)
            regex_result = replace(
                regex_result,
                elapsed_ms=round((perf_counter() - started) * 1000, 3),
            )
            stopped_after_block = regex_result.action == "block"
        if request.multimodal_policy is not None and not stopped_after_block:
            started = perf_counter()
            multimodal_result = await self._test_multimodal(request)
            multimodal_result = replace(
                multimodal_result,
                elapsed_ms=round((perf_counter() - started) * 1000, 3),
            )
            stopped_after_block = multimodal_result.action == "block"
        if request.slm_policy is not None and not stopped_after_block:
            started = perf_counter()
            slm_result = await self._test_slm(request)
            slm_result = replace(
                slm_result,
                elapsed_ms=round((perf_counter() - started) * 1000, 3),
            )

        final_decision = self._final_decision(
            regex_result,
            multimodal_result,
            slm_result,
        )
        return FilterRuleTestResult(
            regex_result=regex_result,
            multimodal_result=multimodal_result,
            slm_result=slm_result,
            final_decision=final_decision,
        )

    async def _test_regex(
        self,
        request: FilterRuleTestRequest,
    ) -> FilterRuleTestExecution:
        assert request.regex_policy is not None
        policy = request.regex_policy
        inspection = await run_sync(
            self._regex_inspection.inspect,
            RegexInspectionRequest(
                contents=request.prompt,
                email=request.email,
                ai_service_type=request.ai_service_id,
                policy=policy,
            ),
        )
        if not inspection.spans:
            return FilterRuleTestExecution(action="undetected")
        action = inspection.action or "undetected"
        selected = inspection.selected_policy
        detail = {
            "policy_id": selected.id if selected else "",
            "policy_name": selected.name if selected else "",
            "evidence": tuple(
                {
                    "matched_text": item.matched_text,
                    "start": item.start,
                    "end": item.end,
                    "policy_id": item.policy.id,
                    "policy_name": item.policy.name,
                }
                for item in inspection.evidence
            ),
        }
        if action == "block":
            return FilterRuleTestExecution(
                action=action,
                content="\n허용되지 않은 프롬프트가 포함되어 요청이 차단되었습니다. \n",
                block_message=self._regex_block_message(),
                **detail,
            )
        if action == "masking":
            masked = self._mask_spans(request.prompt, inspection.spans)
            return FilterRuleTestExecution(
                action=action,
                content=masked,
                block_message=masked,
                **detail,
            )
        return FilterRuleTestExecution(action=action, **detail)

    async def _test_multimodal(
        self,
        request: FilterRuleTestRequest,
    ) -> FilterRuleTestMultimodalExecution:
        assert request.multimodal_policy is not None
        inspection = await self._multimodal_inspection_service.inspect(
            tuple(
                MultimodalInspectionTarget(
                    source=DocumentSource.from_path(item.id),
                    file_name=item.name,
                )
                for item in request.attachments
                if item.id and item.name
            ),
            email=request.email,
            ai_service_id=request.ai_service_id,
            policy_override=request.multimodal_policy,
        )
        selected = inspection.selected_policy
        files = tuple(
            {
                "file_name": item.file_name,
                "action": item.action,
                "policy_id": item.policy.id if item.policy else "",
                "policy_name": item.policy.name if item.policy else "",
                "matches": list(format_multimodal_matches(item)),
                "inspection_status": item.inspection_status,
                "skip_reason": item.skip_reason,
                "evidence": tuple(
                    {
                        "matched_text": evidence.matched_text,
                        "page": evidence.page_number,
                        "line": evidence.line_number,
                        "context": evidence.context,
                    }
                    for evidence in item.evidence
                ),
            }
            for item in inspection.files
        )
        return FilterRuleTestMultimodalExecution(
            action=inspection.action or "undetected",
            policy_id=selected.id if selected else "",
            policy_name=selected.name if selected else "",
            files=files,
        )

    async def _test_slm(
        self,
        request: FilterRuleTestRequest,
    ) -> FilterRuleTestExecution:
        assert request.slm_policy is not None
        policy = request.slm_policy
        inspection = await self._slm_inspection_service.inspect(
            request.prompt,
            email=request.email,
            ai_service_id=request.ai_service_id,
            policy_override=policy,
        )
        if inspection.action not in {"accept", "block", "masking"}:
            return FilterRuleTestExecution(action="undetected")

        evidence = tuple(dict(item) for item in inspection.evidence)
        result_action = inspection.action

        if inspection.action == "accept":
            return FilterRuleTestExecution(
                action=result_action,
                policy_id=inspection.policy.id if inspection.policy else "",
                policy_name=inspection.policy.name if inspection.policy else "",
                evidence=evidence,
            )

        if inspection.action == "block":
            return FilterRuleTestExecution(
                action=result_action,
                content=(
                    "\n허용되지 않은 프롬프트 문맥이 SLM에 의해 "
                    "탐지되어 요청이 차단되었습니다. \n"
                ),
                block_message=self._slm_message(
                    evidence,
                    masking=False,
                ),
                policy_id=inspection.policy.id if inspection.policy else "",
                policy_name=inspection.policy.name if inspection.policy else "",
                evidence=evidence,
            )

        return FilterRuleTestExecution(
            action=result_action,
            content=(
                "\n허용되지 않은 프롬프트 문맥이 SLM에 의해 "
                "탐지되어 요청이 마스킹 되었습니다. \n"
            ),
            block_message=self._slm_message(
                evidence,
                masking=True,
            ),
            policy_id=inspection.policy.id if inspection.policy else "",
            policy_name=inspection.policy.name if inspection.policy else "",
            evidence=evidence,
        )

    @staticmethod
    def _final_decision(
        regex: FilterRuleTestExecution | None,
        multimodal: FilterRuleTestMultimodalExecution | None,
        slm: FilterRuleTestExecution | None,
    ) -> FilterRuleTestExecution:
        executions = tuple(item for item in (regex, multimodal, slm) if item)
        for action in ("block", "masking", "accept", "undetected"):
            selected = next(
                (item for item in executions if item.action == action),
                None,
            )
            if selected is not None:
                return FilterRuleTestExecution(
                    action=selected.action,
                    content=getattr(selected, "content", None),
                    block_message=getattr(selected, "block_message", None),
                )
        return FilterRuleTestExecution(action="undetected")

    def _mask_spans(
        self,
        contents: str,
        spans: tuple[tuple[int, int], ...],
    ) -> str:
        merged: list[tuple[int, int]] = []
        for start, end in sorted(spans):
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))

        parts: list[str] = []
        previous_end = 0
        for start, end in merged:
            parts.extend((contents[previous_end:start], self._mask_token))
            previous_end = end
        parts.append(contents[previous_end:])
        return "".join(parts)

    @classmethod
    def _regex_block_message(cls) -> str:
        return get_block_phrases().prompt_block(cls._POLICY_NAME)

    @staticmethod
    def _slm_message(
        evidence: tuple[dict[str, object], ...],
        *,
        masking: bool,
    ) -> str:
        evidence_types = ",".join(str(item.get("type") or "") for item in evidence)
        evidence_values = ",".join(str(item.get("value") or "") for item in evidence)
        evidence_types = evidence_types or "악성/욕설/유해"
        evidence_values = evidence_values or "(욕설/유해문장포함)"
        phrases = get_block_phrases()
        if masking:
            return phrases.masking_notice(evidence_types, evidence_values)
        return phrases.slm_block(
            "민감정보가 프롬프트 문맥에 포함된 것으로",
            evidence_types,
            evidence_values,
        )
