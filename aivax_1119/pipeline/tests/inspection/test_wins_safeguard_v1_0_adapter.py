"""SafeGuard v1.0 응답과 기존 탐지·정책 계약 사이의 경계를 검증한다."""

from tests.ai_service_catalog import make_ai_service_catalog
import asyncio
import json

import pytest

from src.adapters.analysis_log_delivery import FluentBitAnalysisLogDeliveryAdapter
from src.adapters.inspection_slm.wins_safeguard_v1_0_adapter import (
    WinsSafeguardV1_0SlmAdapter,
)
from src.application.services.inspection_slm import (
    SlmInspectionOptions,
    SlmInspectionService,
)
from src.application.use_cases.input_analysis import (
    InputAnalysisLogBuilder,
    SlmInputInspector,
)
from src.domain.analysis_log_delivery.models import MergedAnalysisLogDocument
from src.domain.analysis_log_merge.services.analysis_log_merger import AnalysisLogMerger
from src.domain.inspection_input.models import (
    InputAnalysisOptions,
    InputAnalysisRequest,
    InputAnalysisResult,
)
from src.domain.inspection_input.services.final_decision_resolver import (
    InputFinalDecisionResolver,
)
from src.domain.inspection_slm.models import SlmInspectionRequest
from src.domain.policy_slm.models import SlmPolicyRule, SlmPolicySnapshot


class StubSlmClient:
    def __init__(self, content: str | None) -> None:
        self.content = content
        self.payloads: list[dict] = []

    async def post(self, payload: dict) -> dict | None:
        self.payloads.append(payload)
        if self.content is None:
            return None
        return {"choices": [{"message": {"content": self.content}}]}


def normal_response(**overrides: object) -> dict:
    return {
        "risk": "P",
        "pii": False,
        "secret": False,
        "abuse": False,
        "gr": "NONE",
        "items": [],
        **overrides,
    }


def inspect(data: dict):
    client = StubSlmClient(json.dumps(data, ensure_ascii=False))
    return asyncio.run(
        WinsSafeguardV1_0SlmAdapter(client).inspect(SlmInspectionRequest("prompt"))
    )


def test_v1_0_maps_combined_detections_without_overriding_policy() -> None:
    content = json.dumps(
        normal_response(
            risk="H",
            pii=True,
            secret=True,
            abuse=True,
            gr="PROMPT_INJECTION",
            items=[
                {"type": "EMAIL", "value": "user@example.com"},
                {"type": "PASSWORD", "value": "example-password"},
            ],
        )
    )
    client = StubSlmClient(content)
    result = asyncio.run(
        WinsSafeguardV1_0SlmAdapter(client).inspect(SlmInspectionRequest("prompt"))
    )

    assert result.unsafe
    assert not result.unavailable
    assert result.raw_content == content
    assert result.use_default_policy_metadata
    assert result.error is None
    assert not result.force_block_without_policy
    assert result.evidence == [
        {"kind": "pii", "type": "EMAIL", "value": "user@example.com"},
        {"kind": "pii", "type": "PASSWORD", "value": "example-password"},
        {"kind": "guardrail", "type": "PROMPT_INJECTION", "value": ""},
        {"kind": "abuse", "type": None, "value": ""},
    ]
    assert client.payloads == [
        {"messages": [{"role": "user", "content": "prompt"}], "max_tokens": 3000}
    ]


def test_v1_0_preserves_non_identifying_items_and_exact_values(caplog) -> None:
    result = inspect(
        normal_response(
            risk="L",
            items=[
                {"type": "BANK", "value": "은행명"},
                {"type": "YEAR", "value": "2026"},
                {"type": "NAME", "value": " 홍길동 "},
            ],
        )
    )

    assert result.unsafe
    assert result.evidence == [
        {"kind": "pii", "type": "BANK", "value": "은행명"},
        {"kind": "pii", "type": "YEAR", "value": "2026"},
        {"kind": "pii", "type": "NAME", "value": " 홍길동 "},
    ]
    assert not caplog.records


def test_v1_0_secret_only_is_detected_when_pii_is_false() -> None:
    result = inspect(
        normal_response(
            risk="H",
            secret=True,
            items=[{"type": "PASSWORD", "value": "example-password"}],
        )
    )

    assert result.unsafe
    assert result.evidence == [
        {"kind": "pii", "type": "PASSWORD", "value": "example-password"}
    ]


@pytest.mark.parametrize("field", ["pii", "secret", "abuse"])
def test_v1_0_positive_flag_without_items_is_not_treated_as_normal(field) -> None:
    result = inspect(normal_response(risk="H", **{field: True}))

    assert result.unsafe
    assert not result.unavailable


def test_v1_0_normal_response() -> None:
    result = inspect(normal_response())

    assert not result.unsafe
    assert not result.unavailable
    assert result.evidence == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"pii": "false"},
        {"secret": 1},
        {"abuse": None},
        {"risk": "INVALID"},
        {"risk": []},
        {"risk": 1},
        {"risk": True},
        {"gr": None},
        {"gr": ""},
        {"items": {}},
        {"items": [None]},
        {"items": [{"type": "", "value": "value"}]},
        {"items": [{"type": "PASSWORD", "value": 1234}]},
        {"items": [{"type": "PASSWORD", "value": ""}]},
    ],
)
def test_v1_0_invalid_fields_are_unavailable(overrides) -> None:
    result = inspect(normal_response(**overrides))

    assert result.unavailable
    assert result.raw_content is not None
    assert result.risk is None


@pytest.mark.parametrize("field", ["risk", "pii", "secret", "abuse", "gr", "items"])
def test_v1_0_missing_required_fields_are_unavailable(field) -> None:
    data = normal_response()
    del data[field]

    result = inspect(data)
    assert result.unavailable
    assert result.risk is None


@pytest.mark.parametrize("content", [None, "invalid json", "[]", ""])
def test_v1_0_transport_or_parsing_failure_is_unavailable(content) -> None:
    result = asyncio.run(
        WinsSafeguardV1_0SlmAdapter(StubSlmClient(content)).inspect(
            SlmInspectionRequest("prompt")
        )
    )

    assert result.unavailable
    assert result.risk is None


def test_v1_0_credential_action_comes_from_aivax_policy() -> None:
    class PolicyProvider:
        def get_policy(self) -> SlmPolicySnapshot:
            return SlmPolicySnapshot(
                rules=(
                    SlmPolicyRule(
                        id="credential-policy",
                        name="자격증명 마스킹",
                        action="masking",
                        detection_kind="pii",
                        detection_type="PASSWORD",
                    ),
                )
            )

    client = StubSlmClient(
        json.dumps(
            normal_response(
                risk="H",
                secret=True,
                items=[{"type": "PASSWORD", "value": "example-password"}],
            )
        )
    )
    service = SlmInspectionService(
        WinsSafeguardV1_0SlmAdapter(client), PolicyProvider(), SlmInspectionOptions()
    )

    result = asyncio.run(service.inspect("password: example-password"))

    assert result.action == "masking"
    assert result.policy is not None
    assert result.policy.id == "credential-policy"
    assert result.evidence[0]["action"] == "masking"


@pytest.mark.parametrize(
    ("risk", "detected", "policy_action"),
    [
        ("P", False, ""),
        ("L", False, ""),
        ("M", False, ""),
        ("H", False, ""),
        ("H", True, ""),
        ("H", True, "accept"),
        ("H", True, "masking"),
        ("H", True, "block"),
    ],
)
def test_risk_reaches_bulk_log_without_changing_policy_action(
    tmp_path, risk, detected, policy_action
) -> None:
    class PolicyProvider:
        def get_policy(self) -> SlmPolicySnapshot:
            return SlmPolicySnapshot(
                rules=(
                    SlmPolicyRule(
                        id="credential-policy",
                        name="Credentials",
                        action=policy_action,
                        detection_kind="pii",
                        detection_type="PASSWORD",
                    ),
                )
                if policy_action
                else (),
            )

    content = json.dumps(
        normal_response(
            risk=risk,
            secret=detected,
            items=[{"type": "PASSWORD", "value": "example-password"}]
            if detected
            else [],
        )
    )
    service = SlmInspectionService(
        WinsSafeguardV1_0SlmAdapter(StubSlmClient(content)),
        PolicyProvider(),
        SlmInspectionOptions(),
    )
    request = InputAnalysisRequest(
        requested_filter_names=("slm_filter",),
        prompt="password: example-password",
        message_id="risk-log",
    )
    execution = asyncio.run(SlmInputInspector(service).inspect(request))
    decision = InputFinalDecisionResolver.resolve(
        (execution,), InputAnalysisOptions(), request.prompt
    )
    builder = InputAnalysisLogBuilder(make_ai_service_catalog())
    document = builder.build(
        request, InputAnalysisResult(executions=(execution,), final_decision=decision)
    )
    restored = builder.restore_executions(document)
    merged = AnalysisLogMerger().merge(document, {"output_text": "response"})

    assert FluentBitAnalysisLogDeliveryAdapter(tmp_path).deliver(
        MergedAnalysisLogDocument(merged)
    )
    stored = json.loads(next(tmp_path.glob("aivax_log#*.json")).read_text())
    detection = stored["filter_detect"][0]
    assert detection["risk"] == risk
    assert restored[0].details["risk"] == risk
    assert detection["slm_content"] == content
    assert detection["mode"] == policy_action
    assert stored["mode"] == policy_action
    assert stored["output_text"] == "response"
    if policy_action == "masking":
        assert detection["masked_contents"] == "password: [AIVAX MASKING]"
