"""동의 입력·출력 병합 후 기존 BLOCK 문서를 갱신한다."""

from tests.ai_service_catalog import make_ai_service_catalog
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import orjson
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.analysis_log_delivery import FluentBitAnalysisLogDeliveryAdapter
from src.adapters.analysis_log_merge.memory_analysis_log_merge_store import (
    MemoryAnalysisLogMergeStore,
)
from src.application.services.analysis_log_merge import AnalysisLogMergeService
from src.application.use_cases.input_analysis import (
    InputAnalysisLogBuilder,
    InputAnalysisUseCase,
)
from src.application.use_cases.output_analysis import OutputAnalysisUseCase
from src.controllers.input_analysis.v1.input_analysis_controller import (
    InputAnalysisController,
)
from src.controllers.output_analysis.v1.output_analysis_controller import (
    OutputAnalysisController,
)
from src.core.constants import ERR_OK
from src.domain.analysis_log_delivery.errors import RetryableAnalysisLogDeliveryError
from src.domain.analysis_log_delivery.errors.consent_log_not_found_error import (
    ConsentLogNotFoundError,
)
from src.domain.analysis_log_delivery.models import MergedAnalysisLogDocument
from src.domain.analysis_log_merge.models.output_log import OutputLogInput
from src.domain.analysis_log_merge.models.output_log_options import OutputLogOptions
from src.domain.config_aivax.models import AivaxConfig, AivaxMode
from src.domain.inspection_input.models import (
    InputAnalysisOptions,
    InputAnalysisRequest,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
    InputPolicyReference,
)


def make_flow(tmp_path, mode="inline"):
    writer = FluentBitAnalysisLogDeliveryAdapter(tmp_path)
    delivery = Mock(wraps=writer)
    additional = Mock(deliver=Mock(return_value=True))
    merge = AnalysisLogMergeService(
        MemoryAnalysisLogMergeStore(), delivery, additional_deliveries=(additional,)
    )
    blocked = InputInspectionResult(
        inspector_type=InputInspectorType.REGEX,
        action=InputInspectionAction.BLOCK,
        block_message="차단 안내",
        policy=InputPolicyReference(id="pii", name="PII"),
    )
    inspectors = [Mock(inspect=AsyncMock(return_value=blocked)) for _ in range(4)]
    inputs = InputAnalysisUseCase(
        regex_inspector=inspectors[0],
        multimodal_inspector=inspectors[1],
        slm_inspector=inspectors[2],
        secuxper_inspector=inspectors[3],
        options=InputAnalysisOptions(),
        input_log_builder=InputAnalysisLogBuilder(make_ai_service_catalog()),
        analysis_log_merge_service=merge,
        aivax_config=Mock(
            get_config=Mock(return_value=AivaxConfig(mode=AivaxMode(mode)))
        ),
        consent_log_writer=writer,
    )
    outputs = OutputAnalysisUseCase(merge, ai_service_catalog=make_ai_service_catalog())
    return inputs, outputs, inspectors, delivery, additional


@pytest.mark.parametrize("output_consent", [False, True])
def test_consent_merges_input_and_output_before_updating_original(
    tmp_path, output_consent
):
    inputs, outputs, inspectors, delivery, additional = make_flow(tmp_path)
    merge = inputs._analysis_log_merge_service
    store = Mock(wraps=merge._store)
    merge._store = store
    request = InputAnalysisRequest(
        requested_filter_names=(
            "input_filter",
            *(kind.value for kind in InputInspectorType),
        ),
        prompt="민감한 원문",
        message_id="blocked-request",
    )

    async def scenario():
        await inputs.execute(request)
        assert delivery.deliver.call_count == 1
        for inspector in inspectors:
            inspector.inspect.reset_mock()
        for _ in range(2):
            previous_updates = {
                path.name: path.read_bytes() for path in tmp_path.glob("*.json.consent")
            }
            outcome = await inputs.execute(replace(request, user_consent=True))
            assert (
                outcome.analysis_result.final_decision.action
                is InputInspectionAction.ACCEPT
            )
            assert outcome.analysis_result.write_input_log
            assert {
                path.name: path.read_bytes() for path in tmp_path.glob("*.json.consent")
            } == previous_updates
            pending = store.set_input_and_pop_output.call_args.args[1]
            assert pending["prompt"] == request.prompt
            assert pending["user_consent"] == "on"
            assert pending["filter_detect"] == []
            assert (
                await outputs.execute(
                    OutputLogInput(
                        request.message_id,
                        "동의 후 모델 응답",
                        user_consent=output_consent,
                    )
                )
                == {}
            )
        for inspector in inspectors:
            inspector.inspect.assert_not_called()

    asyncio.run(scenario())
    originals = [
        orjson.loads(line)
        for p in tmp_path.glob("*.json")
        for line in p.read_bytes().splitlines()
    ]
    patches = [
        orjson.loads(line)
        for p in tmp_path.glob("*.json.consent")
        for line in p.read_bytes().splitlines()
    ]
    assert len(originals) == 1 and len(patches) == 2
    assert originals[0]["mode"] == "block"
    assert patches[-1] == {
        "@timestamp": originals[0]["@timestamp"],
        "message_id": request.message_id,
        "output_text": "동의 후 모델 응답",
        "text_truncated_bytes": len("동의 후 모델 응답"),
        "user_consent": "on",
        "prompt": "민감한 원문",
    }
    assert inputs._consent_log_writer.read_block_log(request.message_id) == {
        **originals[0],
        **patches[-1],
    }
    assert store.set_input_and_pop_output.call_count == 2
    if output_consent:
        assert store.pop_input.call_count == 2
        assert store.pop_input_or_set_output.call_count == 0
    else:
        assert store.pop_input_or_set_output.call_count == 2
    assert delivery.deliver.call_count == 3
    assert additional.deliver.call_count == 1


def test_update_keeps_original_day_after_adapter_restart_and_uses_safe_state_path(
    tmp_path,
):
    document = {
        "message_id": "../../arbitrary/id",
        "mode": "block",
        "@timestamp": "2026-10-05T23:59:59.123456Z",
        "output_text": "blocked",
    }
    assert FluentBitAnalysisLogDeliveryAdapter(tmp_path).deliver(
        MergedAnalysisLogDocument(document)
    )
    restarted = FluentBitAnalysisLogDeliveryAdapter(tmp_path)
    restarted.write_consent_update(document["message_id"], "answer", 6)
    patch = orjson.loads(next(tmp_path.glob("*.json.consent")).read_bytes())
    assert patch["@timestamp"] == document["@timestamp"]
    assert len(list((tmp_path / ".consent-state").glob("*.json"))) == 1


def test_expired_original_is_not_recreated_by_consent_merge(tmp_path):
    inputs, outputs, *_ = make_flow(tmp_path)
    writer = inputs._consent_log_writer
    writer.deliver(
        MergedAnalysisLogDocument({"message_id": "expired", "mode": "block"})
    )
    state = next((tmp_path / ".consent-state").glob("*.json"))
    os.utime(state, (0, 0))
    outcome = asyncio.run(
        inputs.execute(
            InputAnalysisRequest(
                requested_filter_names=(),
                prompt="original",
                message_id="expired",
                user_consent=True,
            )
        )
    )
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.ACCEPT
    assert asyncio.run(outputs.execute(OutputLogInput("expired", "answer"))) == {}
    assert not state.exists()
    assert not list(tmp_path.glob("*.json.consent"))
    assert len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.parametrize(
    "options",
    [
        OutputLogOptions(response_max_bytes=4),
        OutputLogOptions(store_response_text=False),
    ],
)
def test_consent_output_respects_existing_storage_policy(tmp_path, options):
    inputs, outputs, *_ = make_flow(tmp_path)
    writer = inputs._consent_log_writer
    writer.deliver(MergedAnalysisLogDocument({"message_id": "mid", "mode": "block"}))
    outputs.configure(options)
    asyncio.run(
        inputs.execute(
            InputAnalysisRequest(
                requested_filter_names=(),
                prompt="original",
                message_id="mid",
                user_consent=True,
            )
        )
    )
    asyncio.run(outputs.execute(OutputLogInput("mid", "가나다")))
    patch = orjson.loads(next(tmp_path.glob("*.json.consent")).read_bytes())
    assert patch["output_text"] == ("가" if options.store_response_text else None)
    assert patch["text_truncated_bytes"] == (9 if options.store_response_text else 0)
    assert patch["prompt"] == "original"


@pytest.mark.parametrize("consent", [True, "true", "on"])
def test_http_consent_contract_validates_id_and_allows_missing_block(
    tmp_path,
    consent,
):
    inputs, outputs, *_ = make_flow(tmp_path)
    app = FastAPI()
    app.include_router(InputAnalysisController(inputs).router)
    app.include_router(OutputAnalysisController(outputs).router)
    with TestClient(app) as client:
        for route in ("multiple_filter", "output_filter"):
            assert (
                client.post(
                    f"/v1/filter/{route}", json={"user_consent": "on"}
                ).status_code
                == 422
            )
        result = client.post(
            "/v1/filter/multiple_filter",
            json={
                "message_id": "mid",
                "user_consent": consent,
                "prompt": "original",
            },
        )
        assert result.status_code == 200
        assert result.json()["final_decision"]["action"] == "accept"
        result = client.post(
            "/v1/filter/output_filter",
            json={
                "message_id": "mid",
                "user_consent": consent,
                "llm_output": "answer",
            },
        )
        assert result.status_code == 200
        assert result.json()["out_result"] == {}
    assert not list(tmp_path.glob("*.json*"))


def test_masked_request_allows_consent_without_reanalysis_and_keeps_normal_output(
    tmp_path,
):
    inputs, outputs, inspectors, delivery, additional = make_flow(tmp_path)
    inspectors[0].inspect.return_value = InputInspectionResult(
        inspector_type=InputInspectorType.REGEX,
        action=InputInspectionAction.MASKING,
        masked_prompt="masked prompt",
    )
    app = FastAPI()
    app.include_router(InputAnalysisController(inputs).router)
    app.include_router(OutputAnalysisController(outputs).router)
    payload = {
        "message_id": "masked-request",
        "prompt": "original prompt",
        "filter_list": ["input_filter", "secret_filter"],
    }
    with TestClient(app) as client:
        response = client.post("/v1/filter/multiple_filter", json=payload)
        assert response.json()["final_decision"]["action"] == "masking"
        calls = inspectors[0].inspect.await_count
        response = client.post(
            "/v1/filter/multiple_filter",
            json={
                **payload,
                "prompt": "동의",
                "user_consent": "on",
            },
        )
        assert response.status_code == 200
        assert response.json()["final_decision"]["action"] == "accept"
        assert inspectors[0].inspect.await_count == calls
        response = client.post(
            "/v1/filter/output_filter",
            json={
                "message_id": "masked-request",
                "llm_output": "model response",
                "user_consent": "on",
            },
        )
        assert response.status_code == 200
        assert response.json()["out_result"] == {}
        response = client.post(
            "/v1/filter/output_filter",
            json={
                "message_id": "masked-request",
                "llm_output": "model response",
                "user_consent": "off",
            },
        )
        assert response.status_code == 200
    assert delivery.deliver.call_count == additional.deliver.call_count == 1
    document = orjson.loads(next(tmp_path.glob("*.json")).read_bytes())
    assert document["mode"] == "masking"
    assert document["output_text"] == "model response"
    assert not list(tmp_path.glob("*.json.consent"))


def test_consent_without_writer_still_allows(tmp_path):
    inputs, _, inspectors, delivery, additional = make_flow(tmp_path)
    inputs._consent_log_writer = None
    request = InputAnalysisRequest(
        requested_filter_names=tuple(kind.value for kind in InputInspectorType),
        prompt="original",
        message_id="no-state",
        user_consent=True,
    )
    outcome = asyncio.run(inputs.execute(request))
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.ACCEPT
    for inspector in inspectors:
        inspector.inspect.assert_not_called()
    delivery.deliver.assert_not_called()
    additional.deliver.assert_not_called()
    merge = Mock(record_output=Mock(return_value=ERR_OK))
    output = OutputAnalysisUseCase(merge, ai_service_catalog=make_ai_service_catalog())
    result = asyncio.run(
        output.execute(OutputLogInput("no-state", "answer", user_consent=True))
    )
    assert result == {}
    merge.record_output.assert_called_once()


def test_consent_output_without_pending_input_still_patches_block_row(tmp_path):
    """Engine arms user_consent on output_filter even when consent INPUT was skipped."""
    inputs, outputs, _, delivery, additional = make_flow(tmp_path)
    request = InputAnalysisRequest(
        requested_filter_names=("secret_filter",),
        prompt="민감한 원문",
        message_id="consent-orphan-output",
    )
    asyncio.run(inputs.execute(request))
    assert delivery.deliver.call_count == 1
    original = orjson.loads(next(tmp_path.glob("*.json")).read_bytes())
    assert original["mode"] == "block"

    # No second multiple_filter(user_consent=on) — only the model response.
    result = asyncio.run(
        outputs.execute(
            OutputLogInput(
                request.message_id,
                "동의 후 실제 모델 응답",
                user_consent=True,
            )
        )
    )
    assert result == {}
    patches = [
        orjson.loads(line)
        for path in tmp_path.glob("*.json.consent")
        for line in path.read_bytes().splitlines()
        if line.strip()
    ]
    assert len(patches) == 1
    assert patches[0]["message_id"] == request.message_id
    assert patches[0]["output_text"] == "동의 후 실제 모델 응답"
    assert patches[0]["user_consent"] == "on"
    assert patches[0]["@timestamp"] == original["@timestamp"]
    assert delivery.deliver.call_count == 2
    assert additional.deliver.call_count == 1
    updated = inputs._consent_log_writer.read_block_log(request.message_id)
    assert updated["output_text"] == "동의 후 실제 모델 응답"
    assert updated["user_consent"] == "on"


@pytest.mark.parametrize("consent", [None, "", False, "false", "off"])
def test_http_non_consent_keeps_normal_inspection(tmp_path, consent):
    inputs, _, inspectors, *_ = make_flow(tmp_path)
    app = FastAPI()
    app.include_router(InputAnalysisController(inputs).router)
    with TestClient(app) as client:
        response = client.post(
            "/v1/filter/multiple_filter",
            json={
                "message_id": "normal",
                "prompt": "original",
                "filter_list": ["secret_filter"],
                "user_consent": consent,
            },
        )
    assert response.status_code == 200
    assert response.json()["final_decision"]["action"] == "block"
    inspectors[0].inspect.assert_awaited_once()


def test_concurrent_update_files_contain_complete_records(tmp_path):
    writer = FluentBitAnalysisLogDeliveryAdapter(tmp_path)
    writer.deliver(MergedAnalysisLogDocument({"message_id": "mid", "mode": "block"}))

    def send(index):
        FluentBitAnalysisLogDeliveryAdapter(tmp_path).write_consent_update(
            "mid", str(index) * 100000, 100000
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(send, range(8)))
    patches = [
        orjson.loads(line)
        for line in next(tmp_path.glob("*.json.consent")).read_bytes().splitlines()
    ]
    assert len(patches) == 8
    assert {p["output_text"] for p in patches} == {str(i) * 100000 for i in range(8)}


def test_consent_merge_delivery_failure_is_reported(tmp_path):
    inputs, outputs, *_ = make_flow(tmp_path)
    writer = inputs._consent_log_writer
    with pytest.raises(ConsentLogNotFoundError):
        writer.write_consent_update("unknown", "answer", 6)
    writer.deliver(MergedAnalysisLogDocument({"message_id": "mid", "mode": "block"}))
    asyncio.run(
        inputs.execute(
            InputAnalysisRequest(
                requested_filter_names=(),
                prompt="original",
                message_id="mid",
                user_consent=True,
            )
        )
    )
    writer._append = Mock(side_effect=OSError("disk full"))
    app = FastAPI()
    app.include_router(OutputAnalysisController(outputs).router)
    with TestClient(app) as client:
        result = client.post(
            "/v1/filter/output_filter",
            json={
                "message_id": "mid",
                "user_consent": True,
                "llm_output": "answer",
            },
        )
    assert result.status_code == 503
    with pytest.raises(RetryableAnalysisLogDeliveryError):
        writer.write_consent_update("mid", "answer", 6)


def test_consent_input_storage_failure_does_not_return_accept(tmp_path):
    inputs, _, _, _, _ = make_flow(tmp_path)
    store = Mock(
        set_input_and_pop_output=Mock(side_effect=ConnectionError("Valkey unavailable"))
    )
    inputs._analysis_log_merge_service._store = store
    app = FastAPI()
    app.include_router(InputAnalysisController(inputs).router)
    with TestClient(app) as client:
        response = client.post(
            "/v1/filter/multiple_filter",
            json={
                "message_id": "mid",
                "prompt": "original",
                "user_consent": True,
            },
        )
    assert response.status_code == 503
    assert not list(tmp_path.glob("*.json*"))


def test_expired_consent_input_preserves_block_message_without_ai_output(
    tmp_path, monkeypatch
):
    from src.application.use_cases.expire_analysis_log_merge import (
        ExpireAnalysisLogMergeUseCase,
    )

    inputs, _, _, delivery, _ = make_flow(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(
        "src.adapters.analysis_log_merge.memory_analysis_log_merge_store.time.monotonic",
        lambda: clock[0],
    )
    store = MemoryAnalysisLogMergeStore(ttl_seconds=60, input_flush_after_seconds=10)
    merge = inputs._analysis_log_merge_service
    merge._store = store
    request = InputAnalysisRequest(
        requested_filter_names=("secret_filter",),
        prompt="original",
        message_id="mid",
    )
    asyncio.run(inputs.execute(request))
    original = inputs._consent_log_writer.read_block_log("mid")
    asyncio.run(inputs.execute(replace(request, user_consent=True)))
    assert delivery.deliver.call_count == 1
    clock[0] = 11
    ExpireAnalysisLogMergeUseCase(store, merge).execute()
    assert delivery.deliver.call_count == 2
    updated = inputs._consent_log_writer.read_block_log("mid")
    assert updated["output_text"] == original["output_text"]
    assert updated["filter_detect"] == original["filter_detect"]
    assert updated["user_consent"] == "on"
    patch = orjson.loads(next(tmp_path.glob("*.json.consent")).read_bytes())
    assert "output_text" not in patch
