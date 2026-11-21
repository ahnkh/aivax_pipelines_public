"""실제 Valkey와 별도 입력·출력 worker로 동의 로그 병합을 검증한다."""

from tests.ai_service_catalog import make_ai_service_catalog
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import orjson
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from valkey import Valkey

from src.adapters.analysis_log_delivery import FluentBitAnalysisLogDeliveryAdapter
from src.adapters.analysis_log_merge import ValkeyAnalysisLogMergeStore
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
from src.core.time import TimeNormalizer
from src.domain.analysis_log_delivery.models.user_consent import (
    CONSENT_LOG_TIMESTAMP_FIELD,
)
from src.domain.config_aivax.models import AivaxConfig
from src.domain.inspection_input.models import (
    InputAnalysisOptions,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
    InputPolicyReference,
)


@pytest.fixture(scope="module")
def valkey_client(tmp_path_factory):
    binary = shutil.which("valkey-server")
    if binary is None:
        pytest.fail("valkey-server must be installed on the server and available in PATH")
    directory = tmp_path_factory.mktemp("consent-merge-valkey")
    socket = directory / "valkey.sock"
    with (directory / "server.log").open("wb") as log:
        process = subprocess.Popen(
            [
                str(binary),
                "--port",
                "0",
                "--unixsocket",
                str(socket),
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                str(directory),
            ],
            stdout=log,
            stderr=log,
        )
        client = Valkey(unix_socket_path=str(socket), socket_timeout=1)
        try:
            for _ in range(100):
                try:
                    if client.ping():
                        break
                except Exception:
                    if process.poll() is not None:
                        pytest.fail((directory / "server.log").read_text())
                time.sleep(0.01)
            else:
                pytest.fail("테스트 Valkey 시작 실패")
            yield client
        finally:
            client.close()
            process.terminate()
            process.wait(timeout=5)


def records(directory, suffix):
    return [
        orjson.loads(line)
        for path in directory.glob(f"*.{suffix}")
        for line in path.read_bytes().splitlines()
    ]


@pytest.mark.parametrize("output_first", [False, True])
@pytest.mark.parametrize("output_consent", [False, True])
def test_http_consent_uses_valkey_and_preserves_original_index(
    tmp_path,
    valkey_client,
    output_first,
    output_consent,
):
    prefix = f"test:{tmp_path.name}:merge:"
    bulk = tmp_path / "bulk"
    writer = FluentBitAnalysisLogDeliveryAdapter(bulk)

    def merge_service():
        return AnalysisLogMergeService(
            ValkeyAnalysisLogMergeStore(
                valkey_client,
                key_prefix=prefix,
                ttl_seconds=2400,
                input_flush_after_seconds=0,
            ),
            FluentBitAnalysisLogDeliveryAdapter(bulk),
        )

    blocked = Mock(
        inspect=AsyncMock(
            return_value=InputInspectionResult(
                inspector_type=InputInspectorType.REGEX,
                action=InputInspectionAction.BLOCK,
                block_message="original block message",
                policy=InputPolicyReference(id="policy-1", name="PII"),
            )
        )
    )
    other_filters = [Mock(inspect=AsyncMock()) for _ in range(3)]
    inputs = InputAnalysisUseCase(
        regex_inspector=blocked,
        multimodal_inspector=other_filters[0],
        slm_inspector=other_filters[1],
        secuxper_inspector=other_filters[2],
        options=InputAnalysisOptions(),
        input_log_builder=InputAnalysisLogBuilder(make_ai_service_catalog()),
        analysis_log_merge_service=merge_service(),
        aivax_config=Mock(get_config=Mock(return_value=AivaxConfig())),
        consent_log_writer=writer,
    )
    # 출력 worker는 입력 worker의 객체나 동의 writer를 공유하지 않는다.
    app = FastAPI()
    app.include_router(InputAnalysisController(inputs).router)
    app.include_router(
        OutputAnalysisController(OutputAnalysisUseCase(merge_service(), ai_service_catalog=make_ai_service_catalog())).router
    )
    original_body = {
        "message_id": "mid",
        "prompt": "original sensitive prompt",
        "user_id": "user-1",
        "email": "user@example.com",
        "client_host": "127.0.0.1",
        "ai_service": 1,
        "filter_list": [
            "input_filter",
            "secret_filter",
            "file_block_filter",
            "slm_filter",
            "secuxper_filter",
        ],
    }
    output_body = {"message_id": "mid", "llm_output": "AI response"}
    if output_consent:
        output_body["user_consent"] = True

    with TestClient(app) as http:
        with patch.object(
            TimeNormalizer,
            "utc_now",
            return_value=datetime(2000, 1, 1, 23, 59, tzinfo=UTC),
        ):
            initial = http.post("/v1/filter/multiple_filter", json=original_body)
        assert initial.json()["final_decision"]["action"] == "block"
        original = records(bulk, "json")[0]
        assert valkey_client.keys(f"{prefix}*") == []
        if output_first:
            assert (
                http.post("/v1/filter/output_filter", json=output_body).status_code
                == 200
            )
            if output_consent:
                # 동의 OUTPUT은 INPUT을 기다리지 않고 원본 BLOCK 로그를 갱신한다.
                assert valkey_client.get(f"{prefix}outpend:mid") is None
                assert len(records(bulk, "json.consent")) == 1
            else:
                assert valkey_client.get(f"{prefix}outpend:mid") is not None
                assert not records(bulk, "json.consent")
        consent = http.post(
            "/v1/filter/multiple_filter", json={**original_body, "user_consent": True}
        )
        assert consent.status_code == 200
        assert consent.json()["final_decision"]["action"] == "accept"
        if not output_first:
            pending = orjson.loads(valkey_client.get(f"{prefix}mid"))
            assert pending["prompt"] == original_body["prompt"]
            assert pending["user"]["id"] == original_body["user_id"]
            assert pending["user_consent"] == "on"
            assert pending["filter_detect"] == []
            assert pending[CONSENT_LOG_TIMESTAMP_FIELD] == original["@timestamp"]
            assert not records(bulk, "json.consent")
            # AI 처리 중 로컬 갱신 상태가 만료돼도 Valkey에 보존한 원본 날짜를 쓴다.
            state = next((bulk / ".consent-state").glob("*.json"))
            os.utime(state, (0, 0))
            final = http.post("/v1/filter/output_filter", json=output_body)
            assert final.status_code == 200 and final.json()["out_result"] == {}

    if output_first and output_consent:
        # OUTPUT은 이미 반영됐으므로 뒤늦은 동의 INPUT은 별도로 대기한다.
        pending = orjson.loads(valkey_client.get(f"{prefix}mid"))
        assert pending["user_consent"] == "on"
        assert pending[CONSENT_LOG_TIMESTAMP_FIELD] == original["@timestamp"]
    else:
        assert valkey_client.get(f"{prefix}mid") is None
    assert valkey_client.get(f"{prefix}outpend:mid") is None
    assert len(records(bulk, "json")) == 1
    updates = records(bulk, "json.consent")
    assert len(updates) == 1
    expected_update = {
        "@timestamp": original["@timestamp"],
        "message_id": "mid",
        "output_text": "AI response",
        "text_truncated_bytes": len("AI response"),
        "user_consent": "on",
    }
    if not (output_first and output_consent):
        expected_update["prompt"] = original_body["prompt"]
    assert updates[0] == expected_update
    updated = {**original, **updates[0]}
    assert updated["mode"] == "block"
    assert updated["filter_detect"] == original["filter_detect"]
    assert updated["policy"] == original["policy"]
    assert CONSENT_LOG_TIMESTAMP_FIELD not in updated
    assert "effective_decision" not in updated
    blocked.inspect.assert_awaited_once()
    for inspector in other_filters:
        inspector.inspect.assert_not_called()
