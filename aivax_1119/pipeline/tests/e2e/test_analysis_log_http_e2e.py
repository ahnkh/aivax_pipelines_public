"""실제 HTTP 요청으로 분석 로그 병합 결과를 검증하는 E2E 테스트.

이 테스트는 Pipeline 프로세스를 별도 포트로 띄우고, memory merge의 병합 결과가
Fluent Bit spool NDJSON과 Valkey 사용자 알림 Stream까지 도달하는지 확인한다.
테스트 전용 Valkey를 사용하며 ``make test``로 함께 실행한다.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from valkey import Valkey

pytestmark = pytest.mark.analysis_log_e2e

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_READY_TIMEOUT_SECONDS = 30
_RESULT_TIMEOUT_SECONDS = 8


@dataclass(slots=True)
class _E2ERuntime:
    base_url: str
    bulk_directory: Path
    user_stream: str
    valkey: Valkey
    process: subprocess.Popen[str]
    server_log_path: Path
    server_log_file: Any


def _find_unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _delete_test_stream(valkey: Valkey, user_stream: str) -> None:
    """테스트가 만든 Stream과 dedupe marker만 제거한다."""
    keys = [user_stream]
    keys.extend(valkey.scan_iter(match=f"{user_stream}:dedupe:*"))
    if keys:
        valkey.delete(*keys)


def _server_output(runtime: _E2ERuntime) -> str:
    runtime.server_log_file.flush()
    if not runtime.server_log_path.exists():
        return ""
    return runtime.server_log_path.read_text(encoding="utf-8", errors="replace")


def _wait_for_server(
    base_url: str, process: subprocess.Popen[str], log_path: Path
) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = log_path.read_text(encoding="utf-8", errors="replace")
            pytest.fail(f"Pipeline E2E 서버가 시작 중 종료되었습니다.\n{output}")
        try:
            response = httpx.get(f"{base_url}/openapi.json", timeout=0.5)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)

    output = log_path.read_text(encoding="utf-8", errors="replace")
    pytest.fail(f"Pipeline E2E 서버 준비 시간이 초과되었습니다.\n{output}")


@pytest.fixture(scope="module")
def analysis_log_runtime(
    request, tmp_path_factory: pytest.TempPathFactory
) -> _E2ERuntime:
    if (
        not request.config.getoption("--run-all")
        and os.getenv("RUN_ANALYSIS_LOG_E2E") != "1"
    ):
        pytest.skip("Run make test to include HTTP/Valkey E2E")
    api_environment = request.getfixturevalue("isolated_api_environment")
    valkey = Valkey(
        host=api_environment["AIVAX_PIPELINE_VALKEY_HOST"],
        port=int(api_environment["AIVAX_PIPELINE_VALKEY_PORT"]),
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        valkey.ping()
    except Exception as error:
        pytest.fail(f"E2E Valkey 연결 실패: {error}")

    run_id = uuid4().hex
    artifact_directory = tmp_path_factory.mktemp(f"analysis-log-e2e-{run_id[:8]}")
    # 기본은 pytest 임시 경로를 써서 Fluent Bit 감시 디렉터리를 오염시키지 않는다.
    # 운영과 같은 경로 검증이 필요할 때만 명시적으로 대상 경로를 지정한다.
    configured_bulk_directory = os.getenv("AIVAX_PIPELINE_E2E_BULK_DIR")
    bulk_directory = (
        Path(configured_bulk_directory).resolve()
        if configured_bulk_directory
        else artifact_directory / "fluent-bit-bulk"
    )
    runtime_directory = artifact_directory / "runtime"
    user_stream = f"e2e:analysis-log:{run_id}:user"
    policy_stream = f"e2e:analysis-log:{run_id}:policy"
    attachment_stream = f"e2e:analysis-log:{run_id}:attachment"
    port = _find_unused_port()

    for stream in (user_stream, policy_stream, attachment_stream):
        _delete_test_stream(valkey, stream)
    child_environment = {
        key: value for key, value in os.environ.items() if not key.startswith("AIVAX_")
    }
    child_environment.update(api_environment)
    child_environment.update(
        {
            "AIVAX_PIPELINE_HOST": "127.0.0.1",
            "AIVAX_PIPELINE_PORT": str(port),
            "AIVAX_PIPELINE_WORKERS": "1",
            "AIVAX_PIPELINE_MODE_OVERRIDE": "inline",
            "AIVAX_PIPELINE_LOG_LEVEL": "warning",
            "AIVAX_PIPELINE_STREAM_LOG": "false",
            "AIVAX_PIPELINE_RUNTIME_DIR": str(runtime_directory),
            "AIVAX_PIPELINE_TRACE_LOG_PATH": str(artifact_directory / "trace-log"),
            "AIVAX_PIPELINE_LOG_BULK_DIR": str(bulk_directory),
            "AIVAX_PIPELINE_EVENT_USER_SYNC_STREAM_NAME": user_stream,
            "AIVAX_PIPELINE_EVENT_POLICY_NOTIFICATION_STREAM_NAME": policy_stream,
            "AIVAX_PIPELINE_EVENT_ATTACHMENT_STREAM_NAME": attachment_stream,
            "AIVAX_PIPELINE_EVENT_FLUSH_INTERVAL_SECONDS": "1",
        }
    )

    server_log_path = artifact_directory / "pipeline-server.log"
    server_log_file = server_log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=_PROJECT_ROOT,
        env=child_environment,
        stdout=server_log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    runtime = _E2ERuntime(
        base_url=f"http://127.0.0.1:{port}",
        bulk_directory=bulk_directory,
        user_stream=user_stream,
        valkey=valkey,
        process=process,
        server_log_path=server_log_path,
        server_log_file=server_log_file,
    )

    try:
        _wait_for_server(runtime.base_url, process, server_log_path)
        yield runtime
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server_log_file.close()
        if os.getenv("AIVAX_PIPELINE_E2E_KEEP_VALKEY_ARTIFACTS") != "1":
            for stream in (user_stream, policy_stream, attachment_stream):
                _delete_test_stream(valkey, stream)
        valkey.close()


def _input_payload(message_id: str, prompt: str) -> dict[str, Any]:
    return {
        "filter_list": ["input_filter"],
        "prompt": prompt,
        "user_id": "e2e-user",
        "user_name": "홍길동",
        "email": "e2e-user@example.com",
        "ai_service": 1,
        "client_host": "127.0.0.1",
        "session_id": "e2e-session",
        "message_id": message_id,
        "attachments": [],
        "debug": False,
    }


def _output_payload(message_id: str, output_text: str) -> dict[str, Any]:
    return {
        "llm_output": output_text,
        "user_id": "e2e-user",
        "email": "e2e-user@example.com",
        "ai_service": 1,
        "client_host": "127.0.0.1",
        "session_id": "e2e-session",
        "message_id": message_id,
        "debug": False,
    }


def _post(runtime: _E2ERuntime, path: str, payload: dict[str, Any]) -> None:
    response = httpx.post(f"{runtime.base_url}{path}", json=payload, timeout=10)
    assert response.status_code == 200, (
        f"{path} HTTP {response.status_code}: {response.text}\n"
        f"Pipeline log:\n{_server_output(runtime)}"
    )
    result = response.json()
    assert result["api_code"] == 200, result
    if path == "/v1/filter/output_filter":
        assert result["api_message_code"] == "success"
        assert result["api_command"] == "output api router"
        assert result["out_result"] == {}
        assert result["completed_date"]


def _all_documents(bulk_directory: Path) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in bulk_directory.glob("*.json"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                documents.append(json.loads(line))
    return documents


def _wait_for_document(runtime: _E2ERuntime, message_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + _RESULT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        for document in _all_documents(runtime.bulk_directory):
            if document.get("message_id") == message_id:
                return document
        time.sleep(0.1)

    pytest.fail(
        f"병합 NDJSON 문서를 찾지 못했습니다. message_id={message_id}\n"
        f"현재 문서={_all_documents(runtime.bulk_directory)}\n"
        f"Pipeline log:\n{_server_output(runtime)}",
    )


def _wait_for_user_event(runtime: _E2ERuntime, prompt: str) -> dict[str, str]:
    deadline = time.monotonic() + _RESULT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        for _, fields in runtime.valkey.xrange(runtime.user_stream):
            if fields.get("prompt") == prompt:
                return fields
        time.sleep(0.1)

    pytest.fail(
        f"사용자 알림 Stream 이벤트를 찾지 못했습니다. prompt={prompt}\n"
        f"entries={runtime.valkey.xrange(runtime.user_stream)}\n"
        f"Pipeline log:\n{_server_output(runtime)}",
    )


def _assert_merged_document(
    document: dict[str, Any],
    message_id: str,
    prompt: str,
    output_text: str,
) -> None:
    assert document["message_id"] == message_id
    assert document["prompt"] == prompt
    assert document["output_text"] == output_text
    assert document["user"]["id"] == "e2e-user"
    assert document["user"]["name"] == "홍길동"
    assert document["user"]["email"] == "e2e-user@example.com"
    assert document["client_host"] == "127.0.0.1"
    assert document["ai_service_id"] == 1
    assert document["ai_service"] == "GPT"


def _assert_user_event(
    event: dict[str, str],
    message_id: str,
    prompt: str,
) -> None:
    assert event["messageId"] == message_id
    assert event["email"] == "e2e-user@example.com"
    assert event["aiServiceId"] == "1"
    assert event["ip"] == "127.0.0.1"
    assert event["prompt"] == prompt
    assert event["name"] == "홍길동"
    assert set(event) == {"messageId", "email", "aiServiceId", "ip", "prompt", "name"}


def test_input_then_output_writes_merged_ndjson_and_user_stream(
    analysis_log_runtime: _E2ERuntime,
) -> None:
    message_id = f"e2e-normal-{uuid4().hex}"
    prompt = "E2E 정상 병합 입력입니다."
    output_text = "E2E 정상 병합 출력입니다."

    _post(
        analysis_log_runtime,
        "/v1/filter/multiple_filter",
        _input_payload(message_id, prompt),
    )
    # memory store에만 대기하므로 OUTPUT 전에는 외부 전달 문서가 만들어지면 안 된다.
    assert not any(
        document.get("message_id") == message_id
        for document in _all_documents(analysis_log_runtime.bulk_directory)
    )

    _post(
        analysis_log_runtime,
        "/v1/filter/output_filter",
        _output_payload(message_id, output_text),
    )

    document = _wait_for_document(analysis_log_runtime, message_id)
    _assert_merged_document(document, message_id, prompt, output_text)
    _assert_user_event(
        _wait_for_user_event(analysis_log_runtime, prompt),
        message_id,
        prompt,
    )


def test_output_then_input_is_merged_after_late_input(
    analysis_log_runtime: _E2ERuntime,
) -> None:
    message_id = f"e2e-reversed-{uuid4().hex}"
    prompt = "E2E 순서 역전 입력입니다."
    output_text = "E2E 순서 역전 출력입니다."

    _post(
        analysis_log_runtime,
        "/v1/filter/output_filter",
        _output_payload(message_id, output_text),
    )
    assert not any(
        document.get("message_id") == message_id
        for document in _all_documents(analysis_log_runtime.bulk_directory)
    )

    _post(
        analysis_log_runtime,
        "/v1/filter/multiple_filter",
        _input_payload(message_id, prompt),
    )

    document = _wait_for_document(analysis_log_runtime, message_id)
    _assert_merged_document(document, message_id, prompt, output_text)
    _assert_user_event(
        _wait_for_user_event(analysis_log_runtime, prompt),
        message_id,
        prompt,
    )


def test_crossed_message_ids_never_mix_prompt_and_output(
    analysis_log_runtime: _E2ERuntime,
) -> None:
    message_a = f"e2e-cross-a-{uuid4().hex}"
    message_b = f"e2e-cross-b-{uuid4().hex}"
    prompt_a = "E2E 교차 요청 A 입력입니다."
    prompt_b = "E2E 교차 요청 B 입력입니다."
    output_a = "E2E 교차 요청 A 출력입니다."
    output_b = "E2E 교차 요청 B 출력입니다."

    _post(
        analysis_log_runtime,
        "/v1/filter/multiple_filter",
        _input_payload(message_a, prompt_a),
    )
    _post(
        analysis_log_runtime,
        "/v1/filter/output_filter",
        _output_payload(message_b, output_b),
    )
    _post(
        analysis_log_runtime,
        "/v1/filter/output_filter",
        _output_payload(message_a, output_a),
    )
    _post(
        analysis_log_runtime,
        "/v1/filter/multiple_filter",
        _input_payload(message_b, prompt_b),
    )

    document_a = _wait_for_document(analysis_log_runtime, message_a)
    document_b = _wait_for_document(analysis_log_runtime, message_b)
    _assert_merged_document(document_a, message_a, prompt_a, output_a)
    _assert_merged_document(document_b, message_b, prompt_b, output_b)
    _assert_user_event(
        _wait_for_user_event(analysis_log_runtime, prompt_a),
        message_a,
        prompt_a,
    )
    _assert_user_event(
        _wait_for_user_event(analysis_log_runtime, prompt_b),
        message_b,
        prompt_b,
    )


@pytest.mark.parametrize(
    "output_text", ["", "가" * 70000], ids=["empty", "utf8-truncation"]
)
def test_output_edge_cases_preserve_file_and_stream_contract(
    analysis_log_runtime, output_text
):
    message_id = f"e2e-output-edge-{uuid4().hex}"
    prompt = f"출력 경계값 검증 {message_id}"
    _post(
        analysis_log_runtime,
        "/v1/filter/multiple_filter",
        _input_payload(message_id, prompt),
    )
    _post(
        analysis_log_runtime,
        "/v1/filter/output_filter",
        _output_payload(message_id, output_text),
    )
    expected = output_text.encode()[:200000].decode(errors="ignore")
    document = _wait_for_document(analysis_log_runtime, message_id)
    _assert_merged_document(document, message_id, prompt, expected)
    _assert_user_event(
        _wait_for_user_event(analysis_log_runtime, prompt),
        message_id,
        prompt,
    )
    matching = [
        doc
        for doc in _all_documents(analysis_log_runtime.bulk_directory)
        if doc.get("message_id") == message_id
    ]
    assert len(matching) == 1
