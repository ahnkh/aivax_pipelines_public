"""multiple_filter controller가 HTTP 밖의 입력 분석 서비스만 호출하는지 검증한다."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.application.use_cases.input_analysis import InputAnalysisOutcome
from src.controllers.input_analysis.v1.input_analysis_controller import (
    InputAnalysisController,
)
from src.domain.analysis_log_merge.errors import (
    OutputLogProcessorUnavailableError,
)
from src.domain.inspection_input.errors import UnsupportedInputInspectorError
from src.domain.inspection_input.models import (
    InputAnalysisResult,
    InputFinalDecision,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
)
from src.domain.inspection_input.models.cso_grade_result import (
    CsoGradeResult,
    CsoGradeStatus,
)


def _controller(
    *,
    result: InputAnalysisResult,
    log_evidence=None,
    side_effect=None,
    debug_details_enabled=True,
):
    analysis_service = AsyncMock()
    analysis_service.execute.return_value = InputAnalysisOutcome(
        analysis_result=result,
        log_evidence=log_evidence,
        debug_details_enabled=debug_details_enabled,
    )
    analysis_service.execute.side_effect = side_effect
    app = FastAPI()
    app.include_router(InputAnalysisController(analysis_service).router)
    return TestClient(app), analysis_service


def test_controller_passes_raw_filter_names_to_application_service():
    result = InputAnalysisResult(
        executions=(),
        final_decision=InputFinalDecision(),
    )
    client, service = _controller(result=result)

    response = client.post(
        "/v1/filter/multiple_filter",
        json={
            "filter_list": ["input_filter", "secret_filter"],
            "prompt": "hello",
            "message_id": "input-controller",
            "client_host": "127.0.0.1",
            "attachments": [{"id": "/tmp/file", "size": "42", "name": "file"}],
        },
    )

    assert response.status_code == 200
    assert set(response.json()) == {
        "api_code",
        "api_message_code",
        "api_command",
        "cso",
        "final_decision",
        "completed_date",
    }
    assert response.json()["api_code"] == 200
    assert response.json()["api_message_code"] == "success"
    assert response.json()["api_command"] == "pipeline multiple filter"
    assert response.json()["cso"] is None
    assert response.json()["final_decision"] == {
        "action": "",
        "action_code": 0,
        "masked_contents": "",
        "block_message": "",
        "message_id": "input-controller",
    }
    request = service.execute.await_args.args[0]
    assert request.requested_filter_names == ("input_filter", "secret_filter")
    assert request.message_id == "input-controller"
    assert request.user.client_host == "127.0.0.1"
    assert request.attachments[0].size == 42
    service.execute.assert_awaited_once_with(request)


def test_blank_attachment_slot_is_dropped_before_analysis():
    result = InputAnalysisResult(
        executions=(),
        final_decision=InputFinalDecision(),
    )
    client, service = _controller(result=result)

    response = client.post(
        "/v1/filter/multiple_filter",
        json={
            "filter_list": ["input_filter"],
            "prompt": "gdb 잡아보자",
            "message_id": "blank-attachment",
            "attachments": [
                {"id": "", "size": 0, "name": "", "mime_type": ""},
                {"id": "/tmp/real.pdf", "size": 4, "name": "real.pdf"},
            ],
        },
    )

    assert response.status_code == 200
    request = service.execute.await_args.args[0]
    assert [item.id for item in request.attachments] == ["/tmp/real.pdf"]


def test_controller_records_requested_input_log_and_exposes_debug_evidence():
    execution = InputInspectionResult(
        inspector_type=InputInspectorType.REGEX,
        action=InputInspectionAction.MASKING,
        masked_prompt="[AIVAX MASKING]",
    )
    result = InputAnalysisResult(
        executions=(execution,),
        final_decision=InputFinalDecision(
            action=InputInspectionAction.MASKING,
            masked_prompt="[AIVAX MASKING]",
        ),
        write_input_log=True,
    )
    client, service = _controller(
        result=result,
        log_evidence={"filter_detect": []},
    )

    response = client.post(
        "/v1/filter/multiple_filter",
        json={
            "filter_list": ["input_filter", "secret_filter"],
            "prompt": "secret",
            "message_id": "debug-log",
            "debug": True,
        },
    )

    assert response.status_code == 200
    response_result = response.json()
    assert set(response_result) == {
        "api_code",
        "api_message_code",
        "api_command",
        "cso",
        "final_decision",
        "filter_result",
        "log_evidence",
        "completed_date",
    }
    assert response_result["filter_result"]["secret_filter"]["action_code"] == 2
    assert response_result["log_evidence"] == {"filter_detect": []}
    assert response_result["cso"] is None
    service.execute.assert_awaited_once()


def test_controller_hides_debug_details_for_mirror_outcome():
    result = InputAnalysisResult(
        executions=(),
        final_decision=InputFinalDecision(),
    )
    client, service = _controller(
        result=result,
        log_evidence={"filter_detect": []},
        debug_details_enabled=False,
    )

    response = client.post(
        "/v1/filter/multiple_filter",
        json={
            "filter_list": ["input_filter"],
            "message_id": "mirror-debug",
            "debug": True,
        },
    )

    assert response.status_code == 200
    response_result = response.json()
    assert "out_result" not in response_result
    assert "final_decision" in response_result
    assert "filter_result" not in response_result
    assert "log_evidence" not in response_result
    assert response_result["cso"] is None
    service.execute.assert_awaited_once()


@pytest.mark.parametrize("debug", (False, True))
@pytest.mark.parametrize(
    ("grades", "action", "status", "notices"),
    (
        (((3, "tagged"),), "accept", "open", ("결재가 상신됩니다.",)),
        (((3, "tagged"),), "block", "open", ("결재가 상신됩니다.", "‘동의합니다’")),
        (((None, "untagged"),), "block", "untagged", ("CSO 등급 태깅 절차",)),
        (((1, "tagged"),), "block", "blocked", ("CSO 등급 변경 절차",)),
        (((2, "tagged"),), "block", "blocked", ("CSO 등급 변경 절차",)),
        (((None, "error"),), "block", "error", ("CSO 등급 조회에 실패",)),
        (((None, "skipped"),), "accept", "skipped", ()),
        (
            ((3, "tagged"), (None, "skipped")),
            "accept",
            "open",
            ("결재가 상신됩니다.",),
        ),
        (
            ((1, "tagged"), (None, "untagged")),
            "block",
            "untagged",
            ("CSO 등급 태깅 절차",),
        ),
        (
            ((3, "tagged"), (None, "untagged"), (None, "skipped")),
            "block",
            "untagged",
            ("CSO 등급 태깅 절차",),
        ),
        (
            ((None, "untagged"), (3, "tagged")),
            "block",
            "untagged",
            ("CSO 등급 태깅 절차",),
        ),
        (
            ((None, "untagged"), (1, "tagged")),
            "block",
            "untagged",
            ("CSO 등급 태깅 절차",),
        ),
        (
            ((None, "error"), (None, "untagged"), (1, "tagged")),
            "block",
            "untagged",
            ("CSO 등급 태깅 절차",),
        ),
        (
            ((1, "tagged"), (None, "untagged"), (None, "error")),
            "block",
            "untagged",
            ("CSO 등급 태깅 절차",),
        ),
        (
            ((1, "tagged"), (None, "error")),
            "block",
            "blocked",
            ("CSO 등급 변경 절차",),
        ),
        (
            ((None, "error"), (2, "tagged")),
            "block",
            "blocked",
            ("CSO 등급 변경 절차",),
        ),
        (
            ((None, "error"), (3, "tagged"), (None, "skipped")),
            "block",
            "error",
            ("조회에 실패",),
        ),
        (
            ((None, "untagged"), (None, "error")),
            "block",
            "untagged",
            ("CSO 등급 태깅 절차",),
        ),
        (((3, "tagged"),), "masking", "open", ("결재가 상신됩니다.",)),
        (((3, "tagged"),), "", "open", ("결재가 상신됩니다.",)),
        (((3, "tagged"),), "failed", "open", ("결재가 상신됩니다.",)),
    ),
)
def test_cso_response_reports_guidance_without_changing_final_decision(
    debug, grades, action, status, notices
):
    result = InputAnalysisResult(
        executions=(),
        final_decision=InputFinalDecision(
            action=InputInspectionAction(action),
            block_message="기존 차단 안내" if action == "block" else "",
            masked_prompt="[AIVAX MASKING]" if action == "masking" else "",
        ),
        cso_grades=tuple(
            CsoGradeResult(grade, CsoGradeStatus(state)) for grade, state in grades
        ),
    )
    client, service = _controller(result=result)

    response = client.post(
        "/v1/filter/multiple_filter",
        json={"message_id": "cso-guidance", "prompt": "문서 요약", "debug": debug},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body["cso"]) == {"status", "message"}
    assert body["cso"]["status"] == status
    assert all(notice in body["cso"]["message"] for notice in notices)
    for other_status, notice in (
        ("untagged", "CSO 등급 태깅 절차"),
        ("blocked", "CSO 등급 변경 절차"),
        ("error", "CSO 등급 조회에 실패"),
    ):
        if other_status != status:
            assert notice not in body["cso"]["message"]
    if status == "skipped":
        assert body["cso"]["message"] == ""
    if action != "block" or status != "open":
        assert "‘동의합니다’" not in body["cso"]["message"]
    if status != "open":
        assert "결재가 상신됩니다." not in body["cso"]["message"]
    assert body["final_decision"] == {
        "action": action,
        "action_code": {"block": 1, "masking": 2}.get(action, 0),
        "masked_contents": result.final_decision.masked_prompt,
        "block_message": result.final_decision.block_message,
        "message_id": "cso-guidance",
    }
    assert ("filter_result" in body) is debug
    assert ("log_evidence" in body) is debug
    service.execute.assert_awaited_once()


def test_controller_returns_block_result_from_single_service_call():
    result = InputAnalysisResult(
        executions=(),
        final_decision=InputFinalDecision(
            action=InputInspectionAction.BLOCK,
            block_message="blocked",
        ),
    )
    client, service = _controller(result=result)

    response = client.post(
        "/v1/filter/multiple_filter",
        json={
            "filter_list": [],
            "message_id": "blocked-input",
            "email": "user@example.com",
        },
    )

    assert response.status_code == 200
    assert response.json()["final_decision"]["action"] == "block"
    service.execute.assert_awaited_once()


def test_controller_maps_unknown_inspector_to_v1_not_found_response():
    result = InputAnalysisResult(
        executions=(),
        final_decision=InputFinalDecision(),
    )
    client, service = _controller(
        result=result,
        side_effect=UnsupportedInputInspectorError("unknown_filter"),
    )

    response = client.post(
        "/v1/filter/multiple_filter",
        json={
            "filter_list": ["unknown_filter"],
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"]["api_command"] == "pipeline multiple filter"
    service.execute.assert_awaited_once()


def test_controller_maps_unavailable_output_processing_from_use_case():
    result = InputAnalysisResult(
        executions=(),
        final_decision=InputFinalDecision(),
    )
    client, service = _controller(
        result=result,
        side_effect=OutputLogProcessorUnavailableError(),
    )

    response = client.post("/v1/filter/multiple_filter", json={})

    assert response.status_code == 404
    service.execute.assert_awaited_once()
