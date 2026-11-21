"""대상별 secuXper 정책을 선택하고 외부 판정을 결합한다."""

import logging
from difflib import SequenceMatcher

from src.application.services.prompt_inspection_result_formatter import (
    PromptInspectionResultFormatter,
)
from src.domain.inspection_input.models import (
    InputAnalysisOptions,
    InputAnalysisRequest,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
    InputPolicyReference,
)
from src.domain.inspection_input.models.input_analysis_result import (
    InputInspectionError,
)
from src.domain.inspection_input.models.masking import DEFAULT_MASK_TOKEN
from src.domain.inspection_input.services.final_decision_resolver import (
    InputFinalDecisionResolver,
)
from src.domain.inspection_secuxper.models import SecuxperInspectionRequest
from src.domain.inspection_secuxper.ports import SecuxperInspectionPort
from src.domain.policy_secuxper.ports import SecuxperPolicyPort


class SecuxperInspectionService:
    def __init__(
        self,
        inspection: SecuxperInspectionPort,
        policy: SecuxperPolicyPort,
        *,
        mask_token: str = DEFAULT_MASK_TOKEN,
        logger=None,
    ):
        self._inspection = inspection
        self._policy = policy
        self._mask_token = mask_token
        self._logger = logger or logging.getLogger("khan-python")

    async def inspect(self, request: InputAnalysisRequest) -> InputInspectionResult:
        snapshot = self._policy.get_policy()
        selected = snapshot.match(request.user.email, request.user.ai_service)
        executions = []
        for rule in selected:
            try:
                result = await self._inspection.inspect(
                    SecuxperInspectionRequest(request.prompt, rule.policy_code)
                )
            except Exception:
                self._logger.exception(
                    "secuXper inspection failed (policy=%s)", rule.policy_code
                )
                executions.append(
                    InputInspectionResult(
                        inspector_type=InputInspectorType.SECUXPER,
                        action=InputInspectionAction.FAILED,
                        policy=InputPolicyReference(id=rule.id, name=rule.name, category=rule.category, scope=rule.scope, target=rule.targets),
                        details={"policy_code": rule.policy_code, "unavailable": True, "mode": "failed"},
                        error=InputInspectionError("INTERNAL_ERROR", "SecuXper 검사 처리 중 오류가 발생했습니다."),
                    )
                )
                continue
            if result.unavailable:
                executions.append(
                    InputInspectionResult(
                        inspector_type=InputInspectorType.SECUXPER,
                        action=InputInspectionAction.FAILED,
                        policy=InputPolicyReference(id=rule.id, name=rule.name, category=rule.category, scope=rule.scope, target=rule.targets),
                        details={"policy_code": rule.policy_code, "unavailable": True, "mode": "failed"},
                        error=result.error or InputInspectionError("UPSTREAM_UNAVAILABLE", "SecuXper 검사를 사용할 수 없습니다."),
                    )
                )
                continue
            action = InputInspectionAction(result.provider_action or "")
            evidence = tuple(
                {
                    **item,
                    "policy_code": rule.policy_code,
                    "policy_id": rule.id,
                    "policy_name": rule.name,
                    "category": rule.category,
                }
                for item in result.evidence
            )
            if action is InputInspectionAction.MASKING:
                evidence = PromptInspectionResultFormatter._with_mask_spans(
                    request.prompt, evidence
                )
                spans = [
                    item
                    for item in evidence
                    if item.get("action") == "masking"
                    and type(item.get("start")) is int
                    and type(item.get("end")) is int
                ]
                if result.provider_filtered_content or not spans:
                    inferred = [
                        (a, b)
                        for tag, a, b, _, _ in SequenceMatcher(
                            None,
                            request.prompt,
                            result.provider_filtered_content,
                            autojunk=False,
                        ).get_opcodes()
                        if tag != "equal" and a < b
                    ]
                    if not inferred and not spans and request.prompt:
                        inferred = [(0, len(request.prompt))]
                    evidence += tuple(
                        {
                            "start": a,
                            "end": b,
                            "action": "masking",
                            "policy_code": rule.policy_code,
                            "policy_id": rule.id,
                            "policy_name": rule.name,
                            "category": rule.category,
                        }
                        for a, b in inferred
                    )
            executions.append(
                InputInspectionResult(
                    inspector_type=InputInspectorType.SECUXPER,
                    action=action,
                    evidence=evidence,
                    policy=InputPolicyReference(
                        id=rule.id,
                        name=rule.name,
                        target=rule.targets,
                        scope=rule.scope,
                        category=rule.category,
                    ),
                    masked_prompt=PromptInspectionResultFormatter._mask_prompt(
                        request.prompt,
                        evidence,
                        fallback=result.provider_filtered_content,
                        mask_token=self._mask_token,
                    )
                    if action is InputInspectionAction.MASKING
                    else "",
                    block_message=PromptInspectionResultFormatter._provider_block_message(
                        evidence, provider_name="secuXper"
                    )
                    if action is InputInspectionAction.BLOCK
                    else "",
                    details={
                        **result.provider_metadata,
                        "policy_code": rule.policy_code,
                    },
                )
            )
            if action is InputInspectionAction.BLOCK:
                break
        decision = InputFinalDecisionResolver.resolve(
            tuple(executions),
            InputAnalysisOptions(),
            request.prompt,
            mask_token=self._mask_token,
        )
        return InputInspectionResult(
            inspector_type=InputInspectorType.SECUXPER,
            action=decision.action,
            policy=(
                decision.policy
                or next(
                    (execution.policy for execution in executions if execution.policy),
                    None,
                )
            ),
            masked_prompt=decision.masked_prompt,
            block_message=decision.block_message,
            error=next((execution.error for execution in executions if execution.action is InputInspectionAction.FAILED), None) if decision.action is InputInspectionAction.FAILED else None,
            evidence=tuple(
                item for execution in executions for item in execution.evidence
            ),
            details={
                "provider": "secuxperai",
                "policy_version": snapshot.version,
                "skipped": not bool(selected),
                "policy_codes": [rule.policy_code for rule in selected],
                "policy_results": [dict(execution.details) for execution in executions],
                "failed_policies": [
                    {"policy_id": execution.policy.id, "policy_name": execution.policy.name,
                     "category": execution.policy.category, "scope": execution.policy.scope,
                     "target": execution.policy.target, "mode": "failed",
                     **({"error": execution.error.to_document()} if execution.error else {})}
                    for execution in executions
                    if execution.action is InputInspectionAction.FAILED and execution.policy
                ],
            },
        )
