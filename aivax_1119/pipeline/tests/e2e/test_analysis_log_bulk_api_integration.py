"""개발 환경에서 언제든 실행할 수 있는 분석 로그 bulk API 통합 테스트."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_READY_TIMEOUT_SECONDS = 15


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_server(base_url: str, process: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + SERVER_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = log_path.read_text(encoding="utf-8", errors="replace")
            raise AssertionError(f"개발 API 서버가 시작 중 종료되었습니다.\n{output}")
        try:
            if httpx.get(f"{base_url}/openapi.json", timeout=0.5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    output = log_path.read_text(encoding="utf-8", errors="replace")
    raise AssertionError(f"개발 API 서버 준비 시간이 초과되었습니다.\n{output}")


def _documents(directory: Path) -> list[dict]:
    return [
        json.loads(line)
        for path in directory.glob("aivax_log#*.json")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_multiple_and_output_filter_write_merged_ndjson_to_bulk_directory(
    tmp_path: Path,
    isolated_api_environment,
) -> None:
    """실제 개발 API 두 개를 호출해 병합 bulk 파일 생성까지 검증한다."""
    port = _unused_port()
    bulk_directory = tmp_path / "bulk"
    runtime_directory = tmp_path / "runtime"
    server_log_path = tmp_path / "server.log"
    message_id = f"bulk-api-{uuid4().hex}"
    prompt = "bulk API integration input"
    user_email = "user@example.com"
    output_text = "bulk API integration output"
    environment = {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AIVAX_")
        },
        **isolated_api_environment,
        "AIVAX_PIPELINE_HOST": "127.0.0.1",
        "AIVAX_PIPELINE_PORT": str(port),
        "AIVAX_PIPELINE_WORKERS": "1",
        "AIVAX_PIPELINE_MODE_OVERRIDE": "inline",
        "AIVAX_PIPELINE_RUNTIME_DIR": str(runtime_directory),
        "AIVAX_PIPELINE_TRACE_LOG_PATH": str(tmp_path / "trace-log"),
        "AIVAX_PIPELINE_LOG_BULK_DIR": str(bulk_directory),
        "AIVAX_PIPELINE_EVENT_USER_SYNC_STREAM_NAME": (
            f"test:bulk-api:{message_id}:user"
        ),
        "AIVAX_PIPELINE_EVENT_POLICY_NOTIFICATION_STREAM_NAME": (
            f"test:bulk-api:{message_id}:policy"
        ),
        "AIVAX_PIPELINE_EVENT_ATTACHMENT_STREAM_NAME": (
            f"test:bulk-api:{message_id}:attachment"
        ),
        "AIVAX_PIPELINE_LOG_LEVEL": "warning",
        "AIVAX_PIPELINE_STREAM_LOG": "false",
    }

    with server_log_path.open("w", encoding="utf-8") as server_log:
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
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            _wait_for_server(base_url, process, server_log_path)
            with httpx.Client(base_url=base_url, timeout=10) as client:
                input_response = client.post(
                    "/v1/filter/multiple_filter",
                    json={
                        "filter_list": ["input_filter"],
                        "prompt": prompt,
                        "user_id": "bulk-api-test",
                        "email": user_email,
                        "ai_service": 1,
                        "client_host": "127.0.0.1",
                        "session_id": "bulk-api-session",
                        "message_id": message_id,
                        "attachments": [],
                    },
                )
                output_response = client.post(
                    "/v1/filter/output_filter",
                    json={
                        "llm_output": output_text,
                        "user_id": "bulk-api-test",
                        "email": user_email,
                        "ai_service": 1,
                        "client_host": "127.0.0.1",
                        "session_id": "bulk-api-session",
                        "message_id": message_id,
                    },
                )

            assert input_response.status_code == 200, input_response.text
            assert output_response.status_code == 200, output_response.text
            document = next(
                item
                for item in _documents(bulk_directory)
                if item.get("message_id") == message_id
            )
            assert document["prompt"] == prompt
            assert document["output_text"] == output_text
            assert document["user"]["email"] == user_email
        finally:
            _stop(process)
