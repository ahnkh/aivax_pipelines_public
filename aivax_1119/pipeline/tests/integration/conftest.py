"""Opt-in, isolated Docker services and a host Pipeline with its real CSO JVM."""

from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil
import pytest
from valkey import Valkey

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PASSWORD = "PipelineIntegration_2026!"


def wait_until(probe, *, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {description}")


def java_pids(server):
    return tuple(
        child.pid
        for child in psutil.Process(server.pid).children(recursive=True)
        if child.name() == "java" and "CsoJvmWorker" in child.cmdline()
    )


@dataclass
class PipelineStack:
    api: httpx.Client
    search: httpx.Client
    artifacts: Path
    bulk_logs: Path
    server: subprocess.Popen
    jvm_pids: tuple[int, ...]
    cache: Valkey

    def indexed_document(self, message_id, *, expected_fields=None):
        def lookup():
            response = self.search.post(
                "/aivax_log-*/_search",
                json={"query": {"ids": {"values": [message_id]}}},
            )
            if response.status_code in (404, 503):
                # First flush can expose an index before its shards are searchable.
                return None
            response.raise_for_status()
            hits = response.json()["hits"]["hits"]
            if not hits:
                return None
            assert len(hits) == 1, f"Duplicate message_id: {hits}"
            # 동의 갱신은 기존 문서가 먼저 조회될 수 있어 기대 필드까지 기다린다.
            if expected_fields and any(
                hits[0]["_source"].get(key) != value
                for key, value in expected_fields.items()
            ):
                return None
            return hits[0]

        return wait_until(
            lookup, timeout=45, description=f"OpenSearch document {message_id}"
        )

    def spooled_documents(self, message_id, *, consent=False):
        suffix = ".json.consent" if consent else ".json"
        return [
            document
            for path in self.bulk_logs.glob("aivax_log#*" + suffix)
            for line in path.read_text().splitlines()
            if (document := json.loads(line)).get("message_id") == message_id
        ]

    def attachment_events(self, message_id):
        """Management로 전달할 실제 첨부파일 이벤트가 나올 때까지 기다린다."""
        return wait_until(
            lambda: [
                fields
                for _, fields in self.cache.xrange("management:file-asset:v1")
                if fields.get("messageId") == message_id
            ],
            timeout=10,
            description=f"attachment event {message_id}",
        )


@pytest.fixture(scope="session")
def pipeline_stack(request, block_phrase_file):
    if not request.config.getoption("--run-container-integration"):
        pytest.skip("Enable Docker integration with --run-container-integration")
    for executable in ("docker", "java", "javac"):
        if not shutil.which(executable):
            pytest.fail(f"{executable} is required for container integration")
    worker = ROOT / "src/extensions/cso/java/CsoJvmWorker.class"
    if not worker.is_file():
        pytest.fail("Build the CSO wrapper: bash src/extensions/cso/java/build.sh")

    run_id = uuid.uuid4().hex[:12]
    artifacts = ROOT / ".runtime/container-integration" / run_id
    configured_bulk = request.config.getoption("--bulk-logs-dir")
    bulk = (
        Path(configured_bulk).expanduser().resolve()
        if configured_bulk
        else (artifacts / "bulk_logs")
    )
    if bulk.exists() and (not bulk.is_dir() or any(bulk.iterdir())):
        pytest.fail(f"Use an empty test bulk_logs directory: {bulk}")
    bulk.mkdir(parents=True, exist_ok=True)
    state = artifacts / "fluent-bit"
    state.mkdir(parents=True)
    env = dict(os.environ)
    env.update(
        {
            "AIVAX_INTEGRATION_BULK_DIR": str(bulk),
            "AIVAX_INTEGRATION_STATE_DIR": str(state),
        }
    )
    compose = [
        "docker",
        "compose",
        "--project-name",
        f"pipeline-it-{run_id}",
        "--file",
        str(HERE / "compose.yml"),
    ]

    def docker(*arguments, timeout=240, check=True):
        return subprocess.run(
            [*compose, *arguments],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    def service_port(service, port):
        return int(docker("port", service, str(port)).stdout.strip().rsplit(":", 1)[1])

    server = None
    api = search = cache = None
    print(f"\nContainer integration artifacts: {artifacts}\nbulk_logs: {bulk}")
    try:
        started = docker(
            "up", "--detach", "--wait", "--wait-timeout", "180", check=False
        )
        (artifacts / "compose-up.log").write_text(started.stdout + started.stderr)
        assert started.returncode == 0, started.stdout + started.stderr
        search_port = service_port("opensearch", 9200)
        valkey_port = service_port("valkey", 6379)
        cache = Valkey(
            host="127.0.0.1",
            port=valkey_port,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        # O 등급 후속 검사에 사용할 실제 멀티모달 정책을 격리된 Valkey에 발행한다.
        # 기존 CSO 가드 테스트는 파일 분석 필터를 요청하지 않으므로 영향을 받지 않는다.
        cache.set(
            "pipeline:policy:multimodal:v1",
            json.dumps(
                {
                    "version": "cso-integration-policy-v1",
                    "group_members": {},
                    "settings": [],
                    "rules": [
                        {
                            "id": "cso-integration-file-policy",
                            "name": "CSO 통합 테스트 민감문서",
                            "rule": base64.b64encode(
                                b"CSO_POLICY_SECRET_[A-Z0-9]{8}"
                            ).decode(),
                            "action": "block",
                            "scope": "default",
                            "subject_id": "",
                            "category": "integration",
                            "targets": "contents",
                            "regex_flag": 0,
                        }
                    ],
                }
            ),
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            api_port = listener.getsockname()[1]

        (artifacts / "empty.env").write_text("")
        (artifacts / "mode.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "source": "container-integration",
                    "mode": "inline",
                    "filters": {
                        name: {"enabled": name == "multimodal"}
                        for name in (
                            "regex",
                            "multimodal",
                            "slm",
                            "secuxper",
                        )
                    },
                }
            )
        )
        settings = {
            "BLOCK_PHRASES_FILE": str(block_phrase_file),
            "ENV_FILE": str(artifacts / "empty.env"),
            "RUNTIME_DIR": str(artifacts / "runtime"),
            "LOG_BULK_DIR": str(bulk),
            "MODE_FILE": str(artifacts / "mode.json"),
            "HOST": "127.0.0.1",
            "PORT": str(api_port),
            "WORKERS": "1",
            "CSO_ENABLED": "on",
            "LOG_LEVEL": "warning",
            "VALKEY_HOST": "127.0.0.1",
            "VALKEY_PORT": str(valkey_port),
            "VALKEY_PASSWORD": "",
            "ANALYSIS_LOG_MERGE_BACKEND": "valkey",
            "USER_CONSENT_GLOBAL_BYPASS_ENABLED": "true",
            "USER_CONSENT_MULTIMODAL_ENABLED": "true",
            "FILE_CONTENT_CHUNK_SIZE": "256",
            "CDS_DELIVERY_ENABLED": "false",
            "WATERMARK_USE_OCR_BASE_WATERMARK": "false",
            "WATERMARK_USE_REGEX_BASE_DRM": "false",
        }
        pipeline_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AIVAX_")
        }
        pipeline_env.update({"AIVAX_PIPELINE_" + k: v for k, v in settings.items()})
        pipeline_env.update(
            {
                "PYTHONPATH": os.pathsep.join((str(HERE), str(ROOT))),
                "AIVAX_TOOLKIT_ENABLED": "false",
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
            }
        )
        (artifacts / "endpoints.json").write_text(
            json.dumps(
                {
                    "pipeline": f"http://127.0.0.1:{api_port}",
                    "opensearch": f"https://127.0.0.1:{search_port}",
                    "valkey_port": valkey_port,
                    "bulk_logs": str(bulk),
                    "compose_project": f"pipeline-it-{run_id}",
                },
                indent=2,
            )
        )
        with (artifacts / "pipeline.log").open("w") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "pipeline_entry:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                    "--log-level",
                    "warning",
                    "--no-access-log",
                ],
                cwd=ROOT,
                env=pipeline_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        api = httpx.Client(
            base_url=f"http://127.0.0.1:{api_port}", timeout=20, trust_env=False
        )
        search = httpx.Client(
            base_url=f"https://127.0.0.1:{search_port}",
            auth=("admin", PASSWORD),
            verify=False,
            timeout=5,
            trust_env=False,
        )

        def ready():
            assert server.poll() is None, (
                f"Pipeline exited; see {artifacts}/pipeline.log"
            )
            try:
                return api.get("/v1/health/live").status_code == 200
            except httpx.TransportError:
                return False

        wait_until(ready, timeout=45, description="Pipeline startup")
        pids = java_pids(server)
        assert len(pids) == 1, f"Expected one resident CSO JVM: {pids}"
        yield PipelineStack(api, search, artifacts, bulk, server, pids, cache)
    finally:
        if server is not None and server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait(timeout=5)
        for client in (api, search, cache):
            if client is not None:
                client.close()
        try:
            logs = docker("logs", "--no-color", timeout=30, check=False)
            (artifacts / "compose.log").write_text(logs.stdout + logs.stderr)
        finally:
            down = docker(
                "down", "--volumes", "--remove-orphans", timeout=60, check=False
            )
            (artifacts / "compose-down.log").write_text(down.stdout + down.stderr)
            assert down.returncode == 0, down.stdout + down.stderr
