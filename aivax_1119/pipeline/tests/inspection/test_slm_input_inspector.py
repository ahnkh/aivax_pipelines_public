"""SLM 분석 결과를 multiple_filter 공통 결과로 변환하는 계약."""

from tests.ai_service_catalog import make_ai_service_catalog
import asyncio

import pytest

from src.application.services.inspection_slm import (
    SlmAnalysisPolicy,
    SlmAnalysisResult,
    SlmInspectionOptions,
    SlmInspectionService,
)
from src.application.use_cases.input_analysis import (
    InputAnalysisLogBuilder,
    SlmInputInspector,
)
from src.domain.inspection_input.models import (
    InputAnalysisOptions,
    InputAnalysisRequest,
    InputAnalysisResult,
    InputInspectionAction,
)
from src.domain.inspection_input.services.final_decision_resolver import (
    InputFinalDecisionResolver,
)
from src.domain.inspection_slm.models import SlmInspectionResult
from src.domain.policy_slm.models import SlmPolicyRule, SlmPolicySnapshot


class StubSlmInspectionService:
    async def inspect(self, prompt: str) -> SlmAnalysisResult:
        return SlmAnalysisResult(
            action="masking",
            policy=SlmAnalysisPolicy(
                id="slm-policy",
                name="Email",
                action="masking",
                category="safety",
                scope="default",
            ),
            evidence=(
                {
                    "kind": "pii",
                    "type": "EMAIL",
                    "value": "user@example.com",
                    "action": "masking",
                },
            ),
            raw_content="Unsafe",
        )


def test_unavailable_slm_logs_default_mode_and_content() -> None:
    class UnavailableService:
        async def inspect(self, prompt: str) -> SlmAnalysisResult:
            return SlmAnalysisResult(unavailable=True)

    request = InputAnalysisRequest(
        requested_filter_names=("slm_filter",),
        prompt="safe",
        message_id="failed-slm",
    )
    execution = asyncio.run(SlmInputInspector(UnavailableService()).inspect(request))
    result = InputAnalysisResult(
        executions=(execution,),
        final_decision=InputFinalDecisionResolver.resolve(
            (execution,),
            InputAnalysisOptions(),
            request.prompt,
        ),
    )

    document = InputAnalysisLogBuilder(make_ai_service_catalog()).build(request, result)

    assert document["mode"] == ""
    detection = document["filter_detect"][0]
    assert detection["mode"] == ""
    assert detection["slm_content"] == ""
    assert detection["masked_contents"] == ""
    assert detection["evidence"] == []
    assert detection["policy_id"] == ""
    assert detection["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert "risk" not in detection


def test_slm_input_inspector_masks_detected_value() -> None:
    result = asyncio.run(
        SlmInputInspector(
            StubSlmInspectionService(),
        ).inspect(
            InputAnalysisRequest(
                requested_filter_names=("slm_filter",),
                prompt="email=user@example.com",
            ),
        )
    )

    assert result.action is InputInspectionAction.MASKING
    assert result.masked_prompt == "email=[AIVAX MASKING]"
    assert result.policy is not None
    assert result.policy.id == "slm-policy"
    assert result.policy.target == "prompt"
    assert result.details["slm_content"] == "Unsafe"
    assert "risk" not in result.details
    assert result.evidence[0]["start"] == 6
    assert result.evidence[0]["end"] == 22


def test_overlapping_slm_policies_mask_repeated_values_and_keep_policy_ids_in_log():
    class Model:
        async def inspect(self, request):
            return SlmInspectionResult(
                unsafe=True,
                use_default_policy_metadata=True,
                evidence=[
                    {"kind": "pii", "type": "EMAIL", "value": "user@example.com"},
                    {"kind": "pii", "type": "PHONE", "value": "010-1234-5678"},
                ],
            )

    class Policies:
        def get_policy(self):
            return SlmPolicySnapshot(
                rules=(
                    SlmPolicyRule(
                        id="detect-email",
                        name="Email detection",
                        action="accept",
                        detection_kind="pii",
                        detection_type="EMAIL",
                    ),
                    SlmPolicyRule(
                        id="mask-email",
                        name="Email masking",
                        action="masking",
                        detection_kind="pii",
                        detection_type="EMAIL",
                    ),
                    SlmPolicyRule(
                        id="mask-phone",
                        name="Phone masking",
                        action="masking",
                        detection_kind="pii",
                        detection_type="PHONE",
                    ),
                )
            )

    prompt = "user@example.com 010-1234-5678 user@example.com"
    request = InputAnalysisRequest(
        requested_filter_names=("slm_filter",), prompt=prompt
    )
    service = SlmInspectionService(Model(), Policies(), SlmInspectionOptions())
    execution = asyncio.run(
        SlmInputInspector(service, mask_token="[MASK]").inspect(request)
    )

    assert execution.action is InputInspectionAction.MASKING
    assert execution.masked_prompt == "[MASK] [MASK] [MASK]"
    builder = InputAnalysisLogBuilder(make_ai_service_catalog())
    document = builder.build(
        request,
        InputAnalysisResult(
            executions=(execution,),
            final_decision=InputFinalDecisionResolver.resolve(
                (execution,),
                InputAnalysisOptions(),
                prompt,
                mask_token="[MASK]",
            ),
        ),
    )
    assert document["masked_contents"] == "[MASK] [MASK] [MASK]"
    evidence = document["filter_detect"][0]["evidence"]
    assert [item["id"] for item in evidence] == [
        "detect-email",
        "mask-email",
        "mask-email",
        "mask-phone",
    ]
    assert builder.restore_executions(document)[0].evidence == tuple(evidence)


def test_slm_input_inspector_returns_block_message_without_masking() -> None:
    class BlockingService:
        async def inspect(self, prompt: str) -> SlmAnalysisResult:
            return SlmAnalysisResult(
                action="block",
                evidence=({"kind": "abuse", "type": "ABUSE", "value": ""},),
            )

    result = asyncio.run(
        SlmInputInspector(BlockingService()).inspect(
            InputAnalysisRequest(
                requested_filter_names=("slm_filter",),
                prompt="욕설 입력",
            )
        )
    )

    assert result.action is InputInspectionAction.BLOCK
    assert result.masked_prompt == ""
    assert result.block_message.startswith("[AIVAX] 프롬프트 차단\n")
    assert "악성·욕설·유해 콘텐츠" in result.block_message


def test_slm_input_inspector_masks_repeated_values() -> None:
    evidence = (
        {
            "kind": "pii",
            "type": "EMAIL",
            "value": "user@example.com",
            "action": "masking",
        },
    )

    expanded = SlmInputInspector._with_mask_spans(
        "user@example.com and user@example.com",
        evidence,
    )
    masked = SlmInputInspector._mask_prompt(
        "user@example.com and user@example.com",
        expanded,
    )

    assert tuple((item["start"], item["end"]) for item in expanded) == (
        (0, 16),
        (21, 37),
    )
    assert masked == ("[AIVAX MASKING] and [AIVAX MASKING]")


def test_slm_input_inspector_merges_overlapping_spans() -> None:
    evidence = (
        {"start": 0, "end": 10},
        {"start": 5, "end": 15},
    )

    masked = SlmInputInspector._mask_prompt(
        "0123456789abcde-end",
        evidence,
    )

    assert masked == "[AIVAX MASKING]-end"


def test_slm_input_inspector_logs_when_value_is_not_found(
    caplog,
) -> None:
    evidence = (
        {
            "kind": "pii",
            "type": "EMAIL",
            "value": "missing@example.com",
            "action": "masking",
        },
    )

    result = SlmInputInspector._with_mask_spans(
        "safe prompt",
        evidence,
    )

    assert result == evidence
    assert "slm masking value was not found in prompt" in caplog.text


@pytest.mark.parametrize("skipped", [False, True])
def test_slm_without_risk_omits_log_field(skipped) -> None:
    class ServiceWithoutRisk:
        async def inspect(self, prompt: str) -> SlmAnalysisResult:
            return SlmAnalysisResult(skipped=skipped)

    request = InputAnalysisRequest(
        requested_filter_names=("slm_filter",),
        prompt="safe",
    )
    execution = asyncio.run(SlmInputInspector(ServiceWithoutRisk()).inspect(request))
    result = InputAnalysisResult(
        executions=(execution,),
        final_decision=InputFinalDecisionResolver.resolve(
            (execution,), InputAnalysisOptions(), request.prompt
        ),
    )

    detection = InputAnalysisLogBuilder(make_ai_service_catalog()).build(request, result)["filter_detect"][0]

    assert "risk" not in detection
