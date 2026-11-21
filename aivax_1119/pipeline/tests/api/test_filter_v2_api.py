"""V2 계약과 실제 검사·메모리 INPUT/OUTPUT 병합을 HTTP로 검증한다."""

from tests.ai_service_catalog import make_ai_service_catalog
from datetime import UTC, datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from valkey import Valkey

from src.adapters.ai_service_catalog.valkey_ai_service_catalog import (
    ValkeyAiServiceCatalog,
)
from src.adapters.analysis_log_merge import MemoryAnalysisLogMergeStore
from src.adapters.inspection_regex import PythonRegexInspectionAdapter
from src.application.services.analysis_log_merge import AnalysisLogMergeService
from src.application.use_cases.input_analysis import (
    InputAnalysisLogBuilder,
    InputAnalysisOutcome,
    InputAnalysisUseCase,
    RegexInputInspector,
)
from src.application.use_cases.output_analysis import OutputAnalysisUseCase
from src.controllers.documentation import setup_documentation
from src.controllers.input_analysis.v1.input_analysis_controller import (
    InputAnalysisController,
)
from src.controllers.input_analysis.v2.dto import InputAnalysisRequestV2
from src.domain.inspection_input.models.visible_block_log import logged_block_output
from src.controllers.input_analysis.v2.input_analysis_controller import (
    InputAnalysisControllerV2,
)
from src.controllers.output_analysis.v1.output_analysis_controller import (
    OutputAnalysisController,
)
from src.controllers.output_analysis.v2.output_analysis_controller import (
    OutputAnalysisControllerV2,
)
from src.core.time import TimeNormalizer
from src.domain.analysis_log_delivery.errors import RetryableAnalysisLogDeliveryError
from src.domain.analysis_log_delivery.errors.consent_log_not_found_error import (
    ConsentLogNotFoundError,
)
from src.domain.analysis_log_merge.errors import OutputLogProcessorUnavailableError
from src.domain.config_aivax.models import AivaxConfig
from src.domain.inspection_input.errors import UnsupportedInputInspectorError
from src.domain.inspection_input.models import (
    InputAnalysisOptions,
    InputAnalysisResult,
    InputFinalDecision,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
)
from src.domain.inspection_input.models.block_phrases import BlockPhrases
from src.domain.inspection_input.models.cso_grade_result import (
    CsoGradeResult,
    CsoGradeStatus,
)
from src.domain.policy_regex.models import RegexPolicyRule, RegexPolicySnapshot


def runtime(*rules, ai_service_catalog=None):
    catalog = ai_service_catalog or make_ai_service_catalog()
    documents = []
    delivery = Mock()
    delivery.deliver.side_effect = lambda document: (
        documents.append(document.to_dict()) or True
    )
    merge = AnalysisLogMergeService(MemoryAnalysisLogMergeStore(), delivery)
    policy = SimpleNamespace(get_policy=lambda: RegexPolicySnapshot(rules=rules))
    config = SimpleNamespace(get_config=lambda: AivaxConfig())
    input_service = InputAnalysisUseCase(
        regex_inspector=RegexInputInspector(PythonRegexInspectionAdapter(policy)),
        multimodal_inspector=AsyncMock(
            inspect=AsyncMock(
                return_value=InputInspectionResult(InputInspectorType.MULTIMODAL)
            )
        ),
        slm_inspector=AsyncMock(
            inspect=AsyncMock(
                return_value=InputInspectionResult(InputInspectorType.SLM)
            )
        ),
        options=InputAnalysisOptions(),
        input_log_builder=InputAnalysisLogBuilder(catalog),
        analysis_log_merge_service=merge,
        aivax_config=config,
    )
    output_service = OutputAnalysisUseCase(merge, ai_service_catalog=catalog)
    app = FastAPI()
    input_controller = InputAnalysisControllerV2(input_service)
    output_controller = OutputAnalysisControllerV2(output_service)
    app.include_router(input_controller.router)
    app.include_router(output_controller.router)
    app.include_router(InputAnalysisController(input_service).router)
    app.include_router(OutputAnalysisController(output_service).router)
    return SimpleNamespace(
        app=app,
        input_controller=input_controller,
        output_controller=output_controller,
        documents=documents,
        input_service=input_service,
        output_service=output_service,
    )


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_service_names_follow_valkey_catalog_without_pipeline_code_changes(
    isolated_api_environment, version
):
    environment = isolated_api_environment
    valkey = Valkey(
        host=environment["AIVAX_PIPELINE_VALKEY_HOST"],
        port=int(environment["AIVAX_PIPELINE_VALKEY_PORT"]),
    )
    key = f"pipeline:ai-service:catalog:v1:test-{version}"
    catalog = ValkeyAiServiceCatalog(valkey, key)
    app = runtime(ai_service_catalog=catalog)
    try:
        with TestClient(app.app) as client:
            for index, services in enumerate(
                [{"42": "Custom AI"}, {"42": "Renamed AI"}, {}]
            ):
                valkey.set(
                    key, json.dumps({"version": str(index), "services": services})
                )
                catalog.refresh()
                message_id = f"catalog-{version}-{index}"
                response = client.post(
                    f"/{version}/filter/multiple_filter",
                    json={
                        "message_id": message_id,
                        "prompt": "hello",
                        "ai_service": 42,
                        "filter_list": ["input_filter"] if version == "v1" else [],
                    },
                )
                assert response.status_code == 200, response.text
                output = client.post(
                    f"/{version}/filter/output_filter",
                    json={
                        "message_id": message_id,
                        "llm_output": "answer",
                        "ai_service": 42,
                    },
                )
                assert output.status_code == (200 if version == "v1" else 204)
                document = app.documents[-1]
                assert document["ai_service_id"] == 42
                assert document["ai_service"] == services.get("42", "")
    finally:
        valkey.delete(key)
        valkey.close()


def test_input_output_flow_keeps_required_id_and_delivers_merged_document():
    app = runtime()
    with TestClient(app.app) as client:
        response = client.post(
            "/v2/filter/multiple_filter",
            json={
                "prompt": "hello",
                "message_id": " input-output-v2 ",
                "filter_list": [],
                "email": "user@example.com",
                "ai_service": 1,
                "event_time": "2026-10-19T12:00:00+09:00",
            },
        )
        assert response.status_code == 200
        assert set(response.json()) == {"cso", "final_decision"}
        assert response.json()["cso"] is None
        decision = response.json()["final_decision"]
        message_id = decision["message_id"]
        assert message_id == "input-output-v2"
        assert decision == {
            "action": "",
            "masked_contents": "",
            "block_message": "",
            "message_id": message_id,
        }
        assert app.documents == []
        response = client.post(
            "/v2/filter/output_filter",
            json={"message_id": f" {message_id} ", "llm_output": "world"},
        )
    assert response.status_code == 204
    assert response.content == b""
    assert "content-type" not in response.headers
    assert len(app.documents) == 1
    document = app.documents[0]
    assert document["message_id"] == message_id
    assert document["prompt"] == "hello"
    assert document["output_text"] == "world"
    assert document["user"]["email"] == "user@example.com"
    assert document["ai_service_id"] == 1
    assert document["event_time"] == "2026-10-19T12:00:00+09:00"


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize(
    "user_fields,expected_name",
    [
        ({}, ""),
        ({"user_name": None}, ""),
        ({"user_name": ""}, ""),
        ({"user_name": "홍길동"}, "홍길동"),
    ],
    ids=["omitted", "null", "empty", "name"],
)
def test_input_user_name_is_preserved_in_delivered_log(
    version, user_fields, expected_name
):
    app = runtime()
    with TestClient(app.app) as client:
        response = client.post(
            f"/{version}/filter/multiple_filter",
            json={
                "prompt": "hello",
                "message_id": "user-name",
                "filter_list": ["input_filter"] if version == "v1" else [],
                **user_fields,
            },
        )
        assert response.status_code == 200
        assert app.documents == []
        response = client.post(
            f"/{version}/filter/output_filter",
            json={"message_id": "user-name", "llm_output": "world"},
        )
        assert response.status_code == (200 if version == "v1" else 204)
    assert len(app.documents) == 1
    assert app.documents[0]["user"]["name"] == expected_name


@pytest.mark.parametrize("action", ["accept", "block", "masking"])
def test_real_regex_decision_omits_legacy_and_debug_response_fields(action):
    app = runtime(RegexPolicyRule("secret", "Secret", "secret", action))
    with TestClient(app.app) as client:
        response = client.post(
            "/v2/filter/multiple_filter",
            json={"prompt": "a secret", "message_id": "regex", "user_name": "홍길동"},
        )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"cso", "final_decision"}
    assert body["cso"] is None
    assert body["final_decision"]["action"] == action
    assert "action_code" not in body["final_decision"]
    if action == "masking":
        assert body["final_decision"]["masked_contents"] == "a [AIVAX MASKING]"
    if action == "block":
        assert len(app.documents) == 1
        assert app.documents[0]["user"]["name"] == "홍길동"
        assert app.documents[0]["output_text"] == logged_block_output(
            body["final_decision"]["block_message"],
            prompt="a secret",
        )


@pytest.mark.parametrize(
    "path,payload,field",
    [
        (
            "multiple_filter",
            {"prompt": "hello", "message_id": "id", "encoding": False},
            "encoding",
        ),
        (
            "multiple_filter",
            {"prompt": "hello", "message_id": "id", "session_id": "s"},
            "session_id",
        ),
        (
            "multiple_filter",
            {"prompt": "hello", "message_id": "id", "user_id": "user"},
            "user_id",
        ),
        (
            "multiple_filter",
            {"prompt": "hello", "message_id": "id", "debug": False},
            "debug",
        ),
        (
            "output_filter",
            {"message_id": "id", "llm_output": "hello", "session_id": "s"},
            "session_id",
        ),
        (
            "output_filter",
            {"message_id": "id", "llm_output": "hello", "debug": False},
            "debug",
        ),
    ],
)
def test_removed_fields_are_rejected_instead_of_silently_ignored(path, payload, field):
    app = runtime()
    with TestClient(app.app) as client:
        response = client.post(f"/v2/filter/{path}", json=payload)
        assert response.status_code == 422
        assert any(
            error["loc"] == ["body", field] and error["type"] == "extra_forbidden"
            for error in response.json()["detail"]
        )
        assert client.post(f"/v1/filter/{path}", json=payload).status_code == 200


@pytest.mark.parametrize(
    "path,payload",
    [
        ("multiple_filter", {}),
        ("multiple_filter", {"prompt": "hello"}),
        ("multiple_filter", {"prompt": "hello", "message_id": "  "}),
        ("multiple_filter", {"prompt": "", "user_consent": "on"}),
        ("output_filter", {"llm_output": "hello"}),
        ("output_filter", {"message_id": "id"}),
        ("output_filter", {"message_id": "  ", "llm_output": ""}),
    ],
)
def test_missing_required_content_or_invalid_link_id_is_rejected(path, payload):
    app = runtime()
    app.input_controller._use_case = AsyncMock()
    app.output_controller._use_case = AsyncMock()
    with TestClient(app.app) as client:
        assert client.post(f"/v2/filter/{path}", json=payload).status_code == 422
    app.input_controller._use_case.execute.assert_not_awaited()
    app.output_controller._use_case.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "path,error,status,code",
    [
        (
            "multiple_filter",
            UnsupportedInputInspectorError("unknown"),
            404,
            "unsupported_filter",
        ),
        (
            "multiple_filter",
            OutputLogProcessorUnavailableError(),
            404,
            "processor_unavailable",
        ),
        (
            "output_filter",
            OutputLogProcessorUnavailableError(),
            404,
            "processor_unavailable",
        ),
        (
            "multiple_filter",
            ConsentLogNotFoundError("missing"),
            409,
            "consent_log_not_found",
        ),
        (
            "output_filter",
            ConsentLogNotFoundError("missing"),
            409,
            "consent_log_not_found",
        ),
        (
            "multiple_filter",
            RetryableAnalysisLogDeliveryError("retry"),
            503,
            "log_delivery_failed",
        ),
        (
            "output_filter",
            RetryableAnalysisLogDeliveryError("retry"),
            503,
            "log_delivery_failed",
        ),
    ],
)
def test_errors_have_status_and_structured_detail_without_legacy_envelope(
    path, error, status, code
):
    app = runtime()
    service = AsyncMock()
    service.execute.side_effect = error
    if path == "multiple_filter":
        app.input_controller._use_case = service
        payload = {"prompt": "hello", "message_id": "id"}
    else:
        app.output_controller._use_case = service
        payload = {"message_id": "id", "llm_output": "hello"}
    with TestClient(app.app) as client:
        response = client.post(f"/v2/filter/{path}", json=payload)
    assert response.status_code == status
    assert response.json() == {"detail": {"code": code, "message": str(error)}}


@pytest.mark.parametrize("mirror", [False, True])
def test_cso_override_and_response_without_debug_survive_v2_mapping(mirror):
    app = runtime()
    app.input_controller._use_case = AsyncMock()
    app.input_controller._use_case.execute.return_value = InputAnalysisOutcome(
        analysis_result=InputAnalysisResult(
            executions=(),
            final_decision=InputFinalDecision(action=InputInspectionAction.ACCEPT),
            cso_block_message="" if mirror else "CSO blocked",
        ),
        debug_details_enabled=not mirror,
    )
    with TestClient(app.app) as client:
        response = client.post(
            "/v2/filter/multiple_filter",
            json={"prompt": "", "message_id": "guard"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["final_decision"]["action"] == ("accept" if mirror else "block")
    assert body["final_decision"]["block_message"] == ("" if mirror else "CSO blocked")
    assert set(body) == {"cso", "final_decision"}
    assert body["cso"] is None


@pytest.mark.parametrize(
    ("grades", "action", "status", "message"),
    [
        ((), InputInspectionAction.ACCEPT, None, None),
        (
            (CsoGradeResult(1, CsoGradeStatus.TAGGED),),
            InputInspectionAction.ACCEPT,
            "blocked",
            "등급 변경 안내",
        ),
        (
            (CsoGradeResult(2, CsoGradeStatus.TAGGED),),
            InputInspectionAction.ACCEPT,
            "blocked",
            "등급 변경 안내",
        ),
        (
            (CsoGradeResult(3, CsoGradeStatus.TAGGED),),
            InputInspectionAction.ACCEPT,
            "open",
            "결재 안내",
        ),
        (
            (CsoGradeResult(3, CsoGradeStatus.TAGGED),),
            InputInspectionAction.BLOCK,
            "open",
            "결재 안내 동의 안내",
        ),
        (
            (CsoGradeResult(3, CsoGradeStatus.TAGGED),),
            InputInspectionAction.FAILED,
            "open",
            "결재 안내",
        ),
        (
            (CsoGradeResult(None, CsoGradeStatus.UNTAGGED),),
            InputInspectionAction.ACCEPT,
            "untagged",
            "태깅 안내",
        ),
        (
            (CsoGradeResult(None, CsoGradeStatus.ERROR),),
            InputInspectionAction.ACCEPT,
            "error",
            "조회 불가 안내",
        ),
        (
            (CsoGradeResult(None, CsoGradeStatus.SKIPPED),),
            InputInspectionAction.ACCEPT,
            "skipped",
            "",
        ),
        (
            (CsoGradeResult(None, CsoGradeStatus.ERROR, unsupported=True),),
            InputInspectionAction.ACCEPT,
            "error",
            "조회 불가 안내",
        ),
        (
            (
                CsoGradeResult(1, CsoGradeStatus.TAGGED),
                CsoGradeResult(None, CsoGradeStatus.UNTAGGED),
            ),
            InputInspectionAction.ACCEPT,
            "untagged",
            "태깅 안내",
        ),
    ],
)
def test_v1_and_v2_return_the_same_cso_status_and_guidance(
    monkeypatch, grades, action, status, message
):
    phrases = BlockPhrases(
        cso_guidance_blocked="등급 변경 안내",
        cso_guidance_untagged="태깅 안내",
        cso_guidance_error="조회 불가 안내",
        cso_guidance_open="결재 안내",
        cso_guidance_open_consent="동의 안내",
    )
    monkeypatch.setattr(
        "src.controllers.input_analysis.cso_response.get_block_phrases", lambda: phrases
    )
    result = InputAnalysisResult(
        executions=(),
        final_decision=InputFinalDecision(action=action),
        cso_grades=grades,
        cso_block_message="CSO blocked" if any(g.is_blocking for g in grades) else "",
    )
    service = AsyncMock()
    service.execute.return_value = InputAnalysisOutcome(result)
    app = FastAPI()
    app.include_router(InputAnalysisController(service).router)
    app.include_router(InputAnalysisControllerV2(service).router)
    with TestClient(app) as client:
        bodies = []
        for version in ("v1", "v2"):
            response = client.post(
                f"/{version}/filter/multiple_filter",
                json={"prompt": "hello", "message_id": "cso-version-parity"},
            )
            assert response.status_code == 200
            bodies.append(response.json())
    expected = None if status is None else {"status": status, "message": message}
    assert bodies[0]["cso"] == bodies[1]["cso"] == expected
    assert set(bodies[1]) == {"cso", "final_decision"}
    assert (
        bodies[0]["final_decision"]["action"] == bodies[1]["final_decision"]["action"]
    )


def test_live_swagger_preserves_v2_request_response_and_bodyless_output():
    app = runtime()
    setup_documentation(app.app)
    with TestClient(app.app) as client:
        schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/v1/filter/multiple_filter" in paths
    assert "/v2/filter/multiple_filter" in paths
    output = paths["/v2/filter/output_filter"]["post"]["responses"]
    assert "204" in output
    assert "content" not in output["204"]
    assert "200" not in output
    models = schema["components"]["schemas"]
    assert set(models["InputAnalysisRequestV2"]["required"]) == {"prompt", "message_id"}
    assert set(models["OutputAnalysisRequestV2"]["required"]) == {
        "message_id",
        "llm_output",
    }
    assert "encoding" not in models["InputAnalysisRequestV2"]["properties"]
    assert "session_id" not in models["OutputAnalysisRequestV2"]["properties"]
    assert "action_code" not in models["FinalDecisionV2"]["properties"]
    request_fields = models["InputAnalysisRequestV2"]["properties"]
    assert not {"user_id", "debug"} & request_fields.keys()
    assert (
        "input_filter" not in request_fields["filter_list"]["anyOf"][0]["items"]["enum"]
    )
    assert request_fields["user_consent"]["default"] is False
    assert set(models["InputAnalysisResponseV2"]["properties"]) == {
        "cso",
        "final_decision",
    }
    assert set(models["CsoResponseV2"]["properties"]) == {"status", "message"}
    assert {
        entry.get("type")
        for entry in models["InputAnalysisResponseV2"]["properties"]["cso"]["anyOf"]
    } >= {"null"}
    assert set(models["CsoInspectionStatus"]["enum"]) == {
        "open",
        "blocked",
        "untagged",
        "error",
        "skipped",
    }


def test_input_filter_is_not_a_public_v2_filter():
    app = runtime()
    service = AsyncMock()
    app.input_controller._use_case = service
    with TestClient(app.app) as client:
        response = client.post(
            "/v2/filter/multiple_filter",
            json={
                "prompt": "hello",
                "message_id": "id",
                "filter_list": ["input_filter"],
            },
        )
    assert response.status_code == 422
    service.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (1, True),
        ("true", True),
        ("1", True),
        ("on", True),
        ("yes", True),
        ("enable", True),
        ("enabled", True),
        (" ON ", True),
        (False, False),
        (0, False),
        (None, False),
        ("", False),
        ("false", False),
        ("0", False),
        ("off", False),
        ("no", False),
        ("disable", False),
        ("disabled", False),
        (" FALSE ", False),
    ],
)
def test_input_consent_values_are_explicitly_normalized_before_the_use_case(
    value, expected
):
    app = runtime()
    service = AsyncMock()
    service.execute.return_value = InputAnalysisOutcome(
        InputAnalysisResult((), InputFinalDecision())
    )
    app.input_controller._use_case = service
    with TestClient(app.app) as client:
        response = client.post(
            "/v2/filter/multiple_filter",
            json={"prompt": "hello", "message_id": "id", "user_consent": value},
        )
    assert response.status_code == 200
    request = service.execute.await_args.args[0]
    assert request.user_consent is expected
    assert request.requested_filter_names[0] == "input_filter"
    assert request.user.id == ""
    assert request.debug is False


@pytest.mark.parametrize("value", ["maybe", "t", "y", 2, -1, 1.0, [], {}])
def test_input_consent_rejects_values_outside_the_documented_sets(value):
    app = runtime()
    service = AsyncMock()
    app.input_controller._use_case = service
    with TestClient(app.app) as client:
        response = client.post(
            "/v2/filter/multiple_filter",
            json={"prompt": "hello", "message_id": "id", "user_consent": value},
        )
    assert response.status_code == 422
    service.execute.assert_not_awaited()


def test_event_time_is_generated_per_request_in_the_documented_utc_format():
    for second in (1, 2):
        now = datetime(2026, 10, 19, 3, 0, second, tzinfo=UTC)
        with patch.object(TimeNormalizer, "utc_now", return_value=now):
            for event_time in ({}, {"event_time": None}, {"event_time": "  "}):
                request = InputAnalysisRequestV2(
                    prompt="hello", message_id="id", **event_time
                )
                assert request.event_time == f"2026-10-19T03:00:0{second}.000000Z"
                assert request.user_consent is False
                assert "input_filter" not in request.filter_list
    request = InputAnalysisRequestV2(
        prompt="hello", message_id="id", event_time="2026-10-19T12:00:00+09:00"
    )
    assert request.event_time == "2026-10-19T12:00:00+09:00"
