"""SLM 실패 원인이 어댑터에서 분석 로그까지 보존되는지 검증한다."""

from tests.ai_service_catalog import make_ai_service_catalog
import asyncio
import json
import logging

import httpx
import pytest

from src.adapters.analysis_log_delivery import FluentBitAnalysisLogDeliveryAdapter
from src.adapters.inspection_slm.factory import create_slm_inspection_adapter
from src.application.services.inspection_slm import (
    SlmInspectionOptions,
    SlmInspectionService,
)
from src.application.use_cases.input_analysis import (
    InputAnalysisLogBuilder,
    SlmInputInspector,
)
from src.core.http import AsyncJsonHttpClient, HttpClientOptions
from src.domain.analysis_log_delivery.models import MergedAnalysisLogDocument
from src.domain.inspection_input.models import (
    InputAnalysisOptions,
    InputAnalysisRequest,
    InputAnalysisResult,
    InputInspectionAction,
)
from src.domain.inspection_input.services.final_decision_resolver import (
    InputFinalDecisionResolver,
)
from src.domain.inspection_slm.models import SlmInspectionRequest
from src.domain.policy_slm.models import SlmPolicySnapshot


class NoPolicies:
    def get_policy(self):
        return SlmPolicySnapshot()


def stored_failure(tmp_path, handler, *, version=5):
    async def execute():
        client = AsyncJsonHttpClient(
            HttpClientOptions(max_connections=1, max_keepalive_connections=0),
            transport=httpx.MockTransport(handler),
        )
        adapter = create_slm_inspection_adapter(
            {"model_version": version, "slm_url": "http://slm.test/inspect"}, client
        )
        service = SlmInspectionService(adapter, NoPolicies(), SlmInspectionOptions())
        request = InputAnalysisRequest(
            requested_filter_names=("slm_filter",),
            prompt="prompt",
            message_id="slm-failure",
        )
        try:
            execution = await SlmInputInspector(service).inspect(request)
        finally:
            await client.aclose()
        assert execution.action is InputInspectionAction.FAILED
        decision = InputFinalDecisionResolver.resolve(
            (execution,), InputAnalysisOptions(), request.prompt
        )
        document = InputAnalysisLogBuilder(make_ai_service_catalog()).build(
            request, InputAnalysisResult((execution,), decision)
        )
        assert FluentBitAnalysisLogDeliveryAdapter(tmp_path).deliver(
            MergedAnalysisLogDocument(document)
        )
        stored = json.loads(next(tmp_path.glob("aivax_log#*.json")).read_text())
        detection = stored["filter_detect"][0]
        assert detection["filter"] == "slm"
        assert detection["mode"] == ""
        assert "risk" not in detection
        return detection["error"]

    return asyncio.run(execute())


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
@pytest.mark.parametrize(
    ("failure", "code", "message"),
    [
        ("timeout", "UPSTREAM_TIMEOUT", "SLM API 응답 시간이 초과되었습니다."),
        (
            "connection",
            "UPSTREAM_CONNECTION_ERROR",
            "SLM API에 연결하지 못했습니다.",
        ),
        ("status", "UPSTREAM_HTTP_ERROR", "SLM API가 오류 응답을 반환했습니다."),
        ("request", "UPSTREAM_REQUEST_ERROR", "SLM API 요청에 실패했습니다."),
        ("json", "INVALID_RESPONSE", "SLM 응답 형식이 올바르지 않습니다."),
        ("shape", "INVALID_RESPONSE", "SLM 응답 형식이 올바르지 않습니다."),
    ],
)
def test_http_failure_reason_reaches_log(
    tmp_path, caplog, version, failure, code, message
):
    caplog.set_level(logging.ERROR, logger="khan-python")
    sent_payloads = []

    def handler(request):
        sent_payloads.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if failure == "connection":
            raise httpx.ConnectError("connection", request=request)
        if failure == "request":
            raise httpx.RemoteProtocolError("protocol", request=request)
        if failure == "status":
            return httpx.Response(503, json={"error": "unavailable"})
        if failure == "json":
            return httpx.Response(200, text="invalid json")
        return httpx.Response(200, json=[])

    error = stored_failure(tmp_path, handler, version=version)

    assert error == {"code": code, "message": message}
    assert (
        f"request_body={json.dumps(sent_payloads[0], ensure_ascii=False)}"
        in caplog.text
    )
    if failure in {"timeout", "connection", "request"}:
        assert "response_body=null" in caplog.text
    elif failure == "status":
        assert "status=503" in caplog.text
        assert json.dumps('{"error":"unavailable"}') in caplog.text
    if failure == "json":
        assert "invalid JSON" in caplog.text
        assert 'response_body="invalid json"' in caplog.text
    elif failure == "shape":
        assert "body_type=list" in caplog.text
        assert "response_body=[]" in caplog.text


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"risk": "invalid"}, "risk는 P, L, M, H 중 하나여야 합니다."),
        ({"pii": "false"}, "pii는 boolean이어야 합니다."),
        ({"gr": ""}, "gr은 비어 있지 않은 문자열이어야 합니다."),
        ({"items": {}}, "items는 배열이어야 합니다."),
        (
            {"items": [{"type": "EMAIL", "value": ""}]},
            "items[0].value는 비어 있지 않은 문자열이어야 합니다.",
        ),
    ],
)
def test_safeguard_validation_reason_stays_in_runtime_log(
    tmp_path, caplog, overrides, reason
):
    caplog.set_level(logging.ERROR, logger="khan-python")
    content = json.dumps(
        {
            "risk": "P",
            "pii": False,
            "secret": False,
            "abuse": False,
            "gr": "NONE",
            "items": [],
            **overrides,
        }
    )
    error = stored_failure(
        tmp_path,
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        ),
    )

    assert error == {
        "code": "INVALID_RESPONSE",
        "message": "SLM 응답 형식이 올바르지 않습니다.",
    }
    assert reason in caplog.text
    body = {"choices": [{"message": {"content": content}}]}
    assert f"response_body={json.dumps(body, ensure_ascii=False)}" in caplog.text
    assert (
        'request_body={"messages": [{"role": "user", "content": "prompt"}]'
        in caplog.text
    )


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({}, "choices[0].message.content는 문자열이어야 합니다."),
        (
            {"choices": [{"message": {"content": "not json"}}]},
            "assistant content는 JSON 객체여야 합니다.",
        ),
    ],
)
def test_safeguard_content_error_stays_in_runtime_log(tmp_path, caplog, body, reason):
    caplog.set_level(logging.ERROR, logger="khan-python")
    error = stored_failure(tmp_path, lambda request: httpx.Response(200, json=body))

    assert error == {
        "code": "INVALID_RESPONSE",
        "message": "SLM 응답 형식이 올바르지 않습니다.",
    }
    assert reason in caplog.text
    assert f"response_body={json.dumps(body, ensure_ascii=False)}" in caplog.text
    assert (
        'request_body={"messages": [{"role": "user", "content": "prompt"}]'
        in caplog.text
    )


def test_unexpected_failure_is_internal_error(tmp_path, caplog):
    caplog.set_level(logging.ERROR, logger="khan-python")

    def handler(request):
        raise RuntimeError("unexpected failure")

    error = stored_failure(tmp_path, handler)

    assert error == {
        "code": "INTERNAL_ERROR",
        "message": "SLM 검사 처리 중 오류가 발생했습니다.",
    }
    assert (
        'request_body={"messages": [{"role": "user", "content": "prompt"}]'
        in caplog.text
    )
    assert "response_body=null" in caplog.text


@pytest.mark.parametrize(
    ("version", "content", "logs_error"),
    [
        (4, "not json", True),
        (4, '{"has_pii": "false", "items": {}}', True),
        (
            4,
            '{"has_pii": false, "is_abuse": false, "guardrail": "NONE", "items": []}',
            False,
        ),
        (
            5,
            '{"risk": "P", "pii": false, "secret": false, "abuse": false, '
            '"gr": "NONE", "items": []}',
            False,
        ),
    ],
)
def test_model_validation_logs_bodies_at_error_level(
    caplog, version, content, logs_error
):
    caplog.set_level(logging.ERROR, logger="khan-python")
    prompt = '검사 "요청"\n100% 원문'
    body = {"choices": [{"message": {"content": content}}]}

    async def execute():
        client = AsyncJsonHttpClient(
            HttpClientOptions(max_connections=1, max_keepalive_connections=0),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body)
            ),
        )
        adapter = create_slm_inspection_adapter(
            {"model_version": version, "slm_url": "http://slm.test/inspect"}, client
        )
        try:
            await adapter.inspect(SlmInspectionRequest(prompt))
        finally:
            await client.aclose()

    asyncio.run(execute())

    if logs_error:
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 3000,
        }
        assert f"request_body={json.dumps(payload, ensure_ascii=False)}" in caplog.text
        assert f"response_body={json.dumps(body, ensure_ascii=False)}" in caplog.text
    else:
        assert "request_body=" not in caplog.text
        assert "response_body=" not in caplog.text
