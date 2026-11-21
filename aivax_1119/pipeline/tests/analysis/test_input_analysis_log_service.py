"""input_filter 요청의 입력 분석 로그 기록 계약."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.application.use_cases.input_analysis import InputAnalysisLogBuilder
from src.domain.inspection_input.models import (
    InputAnalysisRequest,
    InputAnalysisResult,
    InputAnalysisUser,
    InputFinalDecision,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
    InputPolicyReference,
)
from src.domain.inspection_input.models.input_analysis_result import (
    InputInspectionError,
)
from tests.ai_service_catalog import make_ai_service_catalog


def _result(*, write_input_log: bool) -> InputAnalysisResult:
    policy = InputPolicyReference(
        id="rule-1",
        name="Secret",
        target="prompt",
        category="credential",
        scope="default",
    )
    return InputAnalysisResult(
        executions=(
            InputInspectionResult(
                inspector_type=InputInspectorType.REGEX,
                action=InputInspectionAction.MASKING,
                masked_prompt="[AIVAX MASKING]",
                policy=policy,
                evidence=({"match": "secret", "start": 0, "end": 6},),
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                completed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
                duration_ms=1000,
            ),
        ),
        final_decision=InputFinalDecision(
            action=InputInspectionAction.MASKING,
            masked_prompt="[AIVAX MASKING]",
            policy=policy,
        ),
        write_input_log=write_input_log,
    )


@pytest.mark.parametrize(
    ("ai_service", "service_name"),
    [(9, "GitHub Copilot"), (10, "openapi.copilot"), (13, "Genspark")],
)
def test_service_code_is_named_in_the_input_log(
    ai_service: int, service_name: str
) -> None:
    request = InputAnalysisRequest(
        requested_filter_names=("input_filter",),
        prompt="짧은 질문",
        user=InputAnalysisUser(
            id="cogen_example",
            uuid="",
            email="user@example.com",
            ai_service=ai_service,
            client_host="10.0.50.60",
        ),
        message_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        event_time="2026-11-02 19:00:00",
    )

    document = InputAnalysisLogBuilder(make_ai_service_catalog()).build(
        request, _result(write_input_log=True)
    )

    assert document["ai_service_id"] == ai_service
    assert document["ai_service"] == service_name


def test_records_legacy_compatible_input_document_when_requested() -> None:
    service = InputAnalysisLogBuilder(make_ai_service_catalog())
    request = InputAnalysisRequest(
        requested_filter_names=("input_filter", "secret_filter"),
        prompt="secret",
        user=InputAnalysisUser(
            id="user-1",
            uuid="uuid-1",
            email="user@example.com",
            ai_service=1,
            client_host="127.0.0.1",
        ),
        message_id="message-1",
        event_time="2026-01-01 12:00:00",
    )

    document = service.build(request, _result(write_input_log=True))

    assert document["message_id"] == "message-1"
    assert document["prompt"] == "secret"
    assert document["ai_service_id"] == 1
    assert document["ai_service"] == "GPT"
    assert document["event_time"] == "2026-01-01 12:00:00"
    assert document["request_date"] == "2026-01-01T12:00:00"
    assert document["client_host_cidr"] == 2_130_706_433
    assert document["mode"] == "masking"
    assert document["masked_contents"] == "[AIVAX MASKING]"
    assert document["policy"]["policy_id"] == "rule-1"
    assert document["filter_detect"] == [
        {
            "filter": "regex",
            "mode": "masking",
            "policy_id": "rule-1",
            "policy_name": "Secret",
            "target": "prompt",
            "category": "credential",
            "scope": "default",
            "masked_contents": "[AIVAX MASKING]",
            "evidence": [
                {
                    "match": "secret",
                    "start": 0,
                    "end": 6,
                    "id": "rule-1",
                    "name": "Secret",
                }
            ],
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:01Z",
            "duration_ms": 1000,
        }
    ]


def test_evidence_keeps_its_own_policy_identity() -> None:
    selected_policy = InputPolicyReference(id="block-rule", name="IP - v4")
    result = InputAnalysisResult(
        executions=(
            InputInspectionResult(
                inspector_type=InputInspectorType.REGEX,
                action=InputInspectionAction.BLOCK,
                policy=selected_policy,
                evidence=(
                    {
                        "policy_id": "matching-rule",
                        "policy_name": "VPN address",
                        "action": "block",
                        "match": "127.0.0.1",
                    },
                ),
            ),
        ),
        final_decision=InputFinalDecision(
            action=InputInspectionAction.BLOCK,
            policy=selected_policy,
        ),
    )
    request = InputAnalysisRequest(
        requested_filter_names=("secret_filter",),
        prompt="127.0.0.1",
        user=InputAnalysisUser(),
        message_id="evidence-policy-id",
    )

    evidence = InputAnalysisLogBuilder(make_ai_service_catalog()).build(
        request, result
    )["filter_detect"][0]["evidence"]

    assert evidence == [
        {
            "policy_id": "matching-rule",
            "policy_name": "VPN address",
            "action": "block",
            "match": "127.0.0.1",
            "id": "matching-rule",
            "name": "VPN address",
        }
    ]


def test_preserves_accept_when_policy_was_detected() -> None:
    policy = InputPolicyReference(id="rule-1", name="Allowed")
    result = InputAnalysisResult(
        executions=(
            InputInspectionResult(
                inspector_type=InputInspectorType.REGEX,
                action=InputInspectionAction.ACCEPT,
                policy=policy,
            ),
        ),
        final_decision=InputFinalDecision(action=InputInspectionAction.ACCEPT),
    )
    request = InputAnalysisRequest(
        requested_filter_names=("secret_filter",),
        prompt="safe",
        user=InputAnalysisUser(),
        message_id="message-accept",
    )

    document = InputAnalysisLogBuilder(make_ai_service_catalog()).build(request, result)

    assert document["mode"] == "accept"
    assert document["filter_detect"][0]["mode"] == "accept"


def test_preserves_blank_without_policy_detection() -> None:
    result = InputAnalysisResult(
        executions=(
            InputInspectionResult(
                inspector_type=InputInspectorType.REGEX,
                action=InputInspectionAction.BLANK,
            ),
        ),
        final_decision=InputFinalDecision(action=InputInspectionAction.BLANK),
    )
    request = InputAnalysisRequest(
        requested_filter_names=("secret_filter",),
        prompt="safe",
        user=InputAnalysisUser(),
        message_id="message-collect",
    )

    document = InputAnalysisLogBuilder(make_ai_service_catalog()).build(request, result)

    assert document["mode"] == ""
    assert document["filter_detect"][0]["mode"] == ""


@pytest.mark.parametrize("inspector_type", list(InputInspectorType))
@pytest.mark.parametrize("details", [{}, {"mode": "fail"}, {"mode": "failed"}])
def test_failed_inspection_records_blank_mode_and_keeps_error(
    inspector_type: InputInspectorType,
    details: dict[str, str],
) -> None:
    error = InputInspectionError("UPSTREAM_UNAVAILABLE", "검사를 사용할 수 없습니다.")
    execution = InputInspectionResult(
        inspector_type=inspector_type,
        action=InputInspectionAction.FAILED,
        details=details,
        error=error,
    )
    result = InputAnalysisResult(
        executions=(execution,),
        final_decision=InputFinalDecision(action=InputInspectionAction.FAILED),
    )
    request = InputAnalysisRequest(
        requested_filter_names=("slm_filter",),
        prompt="safe",
        message_id="failed",
    )

    document = InputAnalysisLogBuilder(make_ai_service_catalog()).build(request, result)

    assert document["mode"] == ""
    assert document["filter_detect"][0]["mode"] == ""
    assert document["filter_detect"][0]["error"] == error.to_document()
    if inspector_type is InputInspectorType.SLM:
        assert document["filter_detect"][0]["slm_content"] == ""
    elif inspector_type is InputInspectorType.MULTIMODAL:
        assert document["filter_detect"][0]["file_summary"] == []
    assert execution.action is InputInspectionAction.FAILED
    assert result.final_decision.action is InputInspectionAction.FAILED


def test_failed_filter_keeps_successful_mode_and_normalizes_extra_failures() -> None:
    failed_policy = {"policy_id": "failed-rule", "mode": "failed"}
    result = InputAnalysisResult(
        executions=(
            InputInspectionResult(
                inspector_type=InputInspectorType.SLM,
                action=InputInspectionAction.FAILED,
            ),
            InputInspectionResult(
                inspector_type=InputInspectorType.SECUXPER,
                action=InputInspectionAction.MASKING,
                details={"failed_policies": [failed_policy]},
            ),
        ),
        final_decision=InputFinalDecision(action=InputInspectionAction.MASKING),
    )
    request = InputAnalysisRequest(
        requested_filter_names=("slm_filter",),
        prompt="safe",
        message_id="mixed",
    )

    document = InputAnalysisLogBuilder(make_ai_service_catalog()).build(request, result)

    assert document["mode"] == "masking"
    assert [detect["mode"] for detect in document["filter_detect"]] == [
        "",
        "masking",
        "",
    ]
    assert document["filter_detect"][2]["policy_id"] == "failed-rule"
    assert failed_policy["mode"] == "failed"


def test_preserves_empty_filter_fields_after_an_earlier_block() -> None:
    blocked = InputInspectionResult(
        inspector_type=InputInspectorType.REGEX,
        action=InputInspectionAction.BLOCK,
    )
    result = InputAnalysisResult(
        executions=(
            blocked,
            InputInspectionResult(
                inspector_type=InputInspectorType.MULTIMODAL,
                details={"file_summary": []},
            ),
            InputInspectionResult(
                inspector_type=InputInspectorType.SLM,
                details={"slm_content": ""},
            ),
        ),
        final_decision=InputFinalDecision(action=InputInspectionAction.BLOCK),
        stopped_after_block=True,
    )
    request = InputAnalysisRequest(
        requested_filter_names=(
            "secret_filter",
            "file_block_filter",
            "slm_filter",
        ),
        prompt="blocked",
        message_id="empty-filter-fields",
    )

    detects = InputAnalysisLogBuilder(make_ai_service_catalog()).build(request, result)[
        "filter_detect"
    ]

    assert detects[1] == {
        "filter": "file block",
        "mode": "",
        "policy_id": "",
        "policy_name": "",
        "target": "",
        "category": "",
        "scope": "",
        "masked_contents": "",
        "evidence": [],
        "file_summary": [],
    }
    assert detects[2] == {
        "filter": "slm",
        "mode": "",
        "policy_id": "",
        "policy_name": "",
        "target": "",
        "category": "",
        "scope": "",
        "masked_contents": "",
        "evidence": [],
        "slm_content": "",
    }
