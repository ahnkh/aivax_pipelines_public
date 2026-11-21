"""regex 검사를 multiple_filter 입력 분석 결과로 조립하는 서비스."""

from __future__ import annotations

from collections.abc import Iterable

from anyio.to_thread import run_sync

from src.domain.inspection_input.models import (
    InputAnalysisRequest,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
    InputPolicyReference,
)
from src.domain.inspection_input.models.block_phrases import get_block_phrases
from src.domain.inspection_input.models.masking import DEFAULT_MASK_TOKEN
from src.domain.inspection_input.ports import InputInspectorPort
from src.domain.inspection_regex.models import (
    RegexInspectionRequest,
    RegexMatchedPolicy,
)
from src.domain.inspection_regex.ports import RegexInspectionPort


class RegexInputInspector(InputInspectorPort):
    """RegexInspectionPort를 호출해 입력 분석 공통 결과로 조립한다."""

    _PROMPT_LIMIT_POLICY_NAME = "prompt size exceeds limit"

    def __init__(
        self,
        inspection_port: RegexInspectionPort,
        *,
        prompt_size_limit: int = 1_048_576,
        mask_token: str = DEFAULT_MASK_TOKEN,
    ) -> None:
        self._inspection_port = inspection_port
        self._prompt_size_limit = prompt_size_limit
        self._mask_token = mask_token or DEFAULT_MASK_TOKEN

    async def inspect(self, request: InputAnalysisRequest) -> InputInspectionResult:
        if len(request.prompt) > self._prompt_size_limit:
            return self._prompt_limit_result()

        inspection_result = await run_sync(
            self._inspection_port.inspect,
            RegexInspectionRequest(
                contents=request.prompt,
                email=request.user.email,
                ai_service_type=request.user.ai_service,
            ),
        )
        action = self._to_input_action(inspection_result.action)
        policy = self._to_policy_reference(inspection_result.selected_policy)
        evidence = tuple(
            {
                "policy_id": item.policy.id,
                "policy_name": item.policy.name,
                "action": item.policy.action,
                "target": item.policy.targets,
                "category": item.policy.category,
                "scope": item.policy.scope,
                "match": item.matched_text,
                "start": item.start,
                "end": item.end,
            }
            for item in inspection_result.evidence
        )
        masked_prompt = (
            self._mask_spans(request.prompt, inspection_result.spans)
            if action is InputInspectionAction.MASKING
            else ""
        )
        block_message = (
            self._block_message(self._policy_names(evidence, policy))
            if action in (InputInspectionAction.BLOCK, InputInspectionAction.MASKING)
            else ""
        )
        return InputInspectionResult(
            inspector_type=InputInspectorType.REGEX,
            action=action,
            masked_prompt=masked_prompt,
            block_message=block_message,
            policy=policy,
            evidence=evidence,
        )

    def _prompt_limit_result(self) -> InputInspectionResult:
        policy = InputPolicyReference(name=self._PROMPT_LIMIT_POLICY_NAME)
        return InputInspectionResult(
            inspector_type=InputInspectorType.REGEX,
            action=InputInspectionAction.BLOCK,
            block_message=self._block_message(policy.name),
            policy=policy,
        )

    @staticmethod
    def _to_input_action(action: str) -> InputInspectionAction:
        if action == InputInspectionAction.BLOCK.value:
            return InputInspectionAction.BLOCK
        if action == InputInspectionAction.MASKING.value:
            return InputInspectionAction.MASKING
        if action == InputInspectionAction.ACCEPT.value:
            return InputInspectionAction.ACCEPT
        return InputInspectionAction.BLANK

    @staticmethod
    def _to_policy_reference(
        policy: RegexMatchedPolicy | None,
    ) -> InputPolicyReference | None:
        if policy is None:
            return None
        return InputPolicyReference(
            id=policy.id,
            name=policy.name,
            category=policy.category,
            scope=policy.scope,
            target=policy.targets,
        )

    @staticmethod
    def _policy_names(
        evidence: Iterable[dict[str, object]],
        selected_policy: InputPolicyReference | None,
    ) -> str:
        names: list[str] = []
        for item in evidence:
            name = str(item.get("policy_name") or "")
            if name and name not in names:
                names.append(name)
            if len(names) == 2:
                return ",".join(names)
        return ",".join(names) or (selected_policy.name if selected_policy else "")

    def _mask_spans(self, prompt: str, spans: Iterable[tuple[int, int]]) -> str:
        merged: list[tuple[int, int]] = []
        for start, end in sorted(spans):
            start = max(0, start)
            end = min(len(prompt), end)
            if start >= end:
                continue
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))

        if not merged:
            return prompt
        parts: list[str] = []
        previous_end = 0
        for start, end in merged:
            parts.extend((prompt[previous_end:start], self._mask_token))
            previous_end = end
        parts.append(prompt[previous_end:])
        return "".join(parts)

    @staticmethod
    def _block_message(policy_names: str) -> str:
        return get_block_phrases().prompt_block(policy_names)
