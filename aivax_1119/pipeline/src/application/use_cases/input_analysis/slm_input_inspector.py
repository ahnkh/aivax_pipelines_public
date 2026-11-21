"""공용 SLM 분석 결과를 multiple_filter 입력 결과로 변환한다."""

from src.application.services.inspection_slm import (
    SlmAnalysisPolicy,
    SlmInspectionService,
)
from src.application.services.prompt_inspection_result_formatter import (
    PromptInspectionResultFormatter,
)
from src.domain.inspection_input.models import (
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
from src.domain.inspection_input.ports import InputInspectorPort


class SlmInputInspector(PromptInspectionResultFormatter, InputInspectorPort):
    def __init__(
        self,
        inspection_service: SlmInspectionService,
        *,
        mask_token: str = DEFAULT_MASK_TOKEN,
    ) -> None:
        self._inspection_service = inspection_service
        self._mask_token = mask_token

    async def inspect(self, request: InputAnalysisRequest) -> InputInspectionResult:
        policy_context = {}
        if request.user.email or request.user.ai_service:
            policy_context = {
                "email": request.user.email,
                "ai_service_id": request.user.ai_service,
            }

        result = await self._inspection_service.inspect(
            request.prompt,
            **policy_context,
        )
        if result.unavailable:
            return InputInspectionResult(
                inspector_type=InputInspectorType.SLM,
                action=InputInspectionAction.FAILED,
                error=InputInspectionError(result.error.code, result.error.message)
                if result.error is not None
                else InputInspectionError(
                    "UPSTREAM_UNAVAILABLE", "SLM 검사를 사용할 수 없습니다."
                ),
            )
        action = self._input_action(result.action)
        evidence = tuple(dict(item) for item in result.evidence)

        if action is InputInspectionAction.MASKING:
            evidence = self._with_mask_spans(request.prompt, evidence)

        details = {"slm_content": result.raw_content}
        if result.risk is not None:
            details["risk"] = result.risk.value

        return InputInspectionResult(
            inspector_type=InputInspectorType.SLM,
            action=action,
            masked_prompt=(
                self._mask_prompt(
                    request.prompt,
                    evidence,
                    mask_token=self._mask_token,
                )
                if action is InputInspectionAction.MASKING
                else ""
            ),
            block_message=(
                self._block_message(evidence)
                if action is InputInspectionAction.BLOCK
                else ""
            ),
            policy=self._policy_reference(result.policy),
            evidence=evidence,
            details=details,
        )

    @staticmethod
    def _input_action(value: str) -> InputInspectionAction:
        if value == "block":
            return InputInspectionAction.BLOCK
        if value == "masking":
            return InputInspectionAction.MASKING
        if value == InputInspectionAction.ACCEPT.value:
            return InputInspectionAction.ACCEPT
        return InputInspectionAction.BLANK

    @staticmethod
    def _policy_reference(
        policy: SlmAnalysisPolicy | None,
    ) -> InputPolicyReference | None:
        if policy is None:
            return None
        return InputPolicyReference(
            id=policy.id,
            name=policy.name,
            target="prompt",
            category=policy.category,
            scope=policy.scope,
        )
