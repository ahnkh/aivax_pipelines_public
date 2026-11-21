"""여러 입력 필터 결과를 하나의 외부 판정으로 결합한다."""

from dataclasses import replace

from src.domain.inspection_input.models import (
    InputAnalysisOptions,
    InputFinalDecision,
    InputInspectionAction,
    InputInspectionResult,
)
from src.domain.inspection_input.models.masking import DEFAULT_MASK_TOKEN


class InputFinalDecisionResolver:
    """
    도메인 서비스

    기존 multiple_filter의 block > masking > accept 우선순위를 보존한다.
    """

    _CONSENT_NOTICE = (
        "\n\n차단된 요청의 전송에 동의하시면, "
        "‘동의합니다’를 입력한 후 다시 전송해 주세요."
    )

    @staticmethod
    def resolve(
        executions: tuple[InputInspectionResult, ...],
        options: InputAnalysisOptions,
        prompt: str = "",
        *,
        consented: bool = False,
        mask_token: str = DEFAULT_MASK_TOKEN,
    ) -> InputFinalDecision:
        if consented:
            # 한 필터에서 차단과 마스킹을 함께 탐지한 경우도 마스킹은 유지한다.
            executions = tuple(
                replace(item, action=InputInspectionAction.MASKING, block_message="")
                if item.action is InputInspectionAction.BLOCK
                and options.is_user_consent_enabled(item.inspector_type)
                and (
                    item.masked_prompt
                    or any(
                        evidence.get("action") == "masking"
                        for evidence in item.evidence
                    )
                )
                else item
                for item in executions
            )
        first_masking: InputInspectionResult | None = None
        for execution in executions:
            if execution.action is InputInspectionAction.BLOCK:
                if consented and options.is_user_consent_enabled(
                    execution.inspector_type
                ):
                    continue
                if options.bypass_block:
                    break
                return InputFinalDecision(
                    action=InputInspectionAction.BLOCK,
                    masked_prompt=execution.masked_prompt,
                    block_message=execution.block_message.removesuffix(
                        InputFinalDecisionResolver._CONSENT_NOTICE
                    )
                    + (
                        InputFinalDecisionResolver._CONSENT_NOTICE
                        if options.is_user_consent_enabled(execution.inspector_type)
                        else ""
                    ),
                    policy=execution.policy,
                )
            if execution.action is InputInspectionAction.MASKING:
                if options.bypass_masking:
                    break
                if first_masking is None:
                    first_masking = execution

        if first_masking is not None:
            return InputFinalDecision(
                action=InputInspectionAction.MASKING,
                masked_prompt=InputFinalDecisionResolver._merged_masked_prompt(
                    prompt,
                    executions,
                    first_masking.masked_prompt,
                    mask_token=mask_token,
                ),
                block_message=first_masking.block_message,
                policy=first_masking.policy,
            )
        failure = next(
            (
                execution
                for execution in executions
                if execution.action is InputInspectionAction.FAILED
            ),
            None,
        )
        if failure is not None:
            return InputFinalDecision(
                action=InputInspectionAction.FAILED, policy=failure.policy
            )
        if any(
            execution.action is InputInspectionAction.ACCEPT for execution in executions
        ) or (
            consented
            and any(
                execution.action is InputInspectionAction.BLOCK
                and options.is_user_consent_enabled(execution.inspector_type)
                for execution in executions
            )
        ):
            return InputFinalDecision(action=InputInspectionAction.ACCEPT)
        return InputFinalDecision(action=InputInspectionAction.BLANK)

    @staticmethod
    def _merged_masked_prompt(
        prompt: str,
        executions: tuple[InputInspectionResult, ...],
        fallback: str,
        *,
        mask_token: str = DEFAULT_MASK_TOKEN,
    ) -> str:
        if not prompt:
            return fallback
        spans: list[tuple[int, int]] = []
        for execution in executions:
            if execution.action is not InputInspectionAction.MASKING:
                continue
            for item in execution.evidence:
                if item.get("action") not in (None, "masking"):
                    continue
                start = item.get("start")
                end = item.get("end")
                if isinstance(start, int) and isinstance(end, int):
                    spans.append((start, end))
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
            return fallback
        parts: list[str] = []
        previous_end = 0
        for start, end in merged:
            parts.extend((prompt[previous_end:start], mask_token))
            previous_end = end
        parts.append(prompt[previous_end:])
        return "".join(parts)
