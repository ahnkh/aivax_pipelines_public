"""Register test options before collection and provide isolated API dependencies."""

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from valkey import Valkey

from src.domain.inspection_input.models import block_phrases

ROOT = Path(__file__).resolve().parent


@pytest.fixture(scope="session")
def block_phrase_file():
    return ROOT / "tests/fixtures/block_phrases.json"


@pytest.fixture(autouse=True)
def isolated_block_phrases(monkeypatch, block_phrase_file):
    monkeypatch.setattr(
        block_phrases, "_canonical_phrase_file", lambda: block_phrase_file
    )
    monkeypatch.setenv("AIVAX_PIPELINE_BLOCK_PHRASES_FILE", str(block_phrase_file))
    monkeypatch.delenv("AIVAX_BLOCK_PHRASES_FILE", raising=False)
    block_phrases.get_block_phrases.cache_clear()
    yield
    block_phrases.get_block_phrases.cache_clear()


def pytest_addoption(parser):
    group = parser.getgroup("pipeline-integration")
    group.addoption(
        "--run-all", action="store_true", help="Run all unit, API and Docker tests"
    )
    group.addoption(
        "--run-container-integration",
        action="store_true",
        help="Run Pipeline/CSO with isolated Docker services",
    )
    group.addoption(
        "--run-engine-integration",
        action="store_true",
        help="Compare block logs with the external ai_engine chat renderer",
    )
    group.addoption("--bulk-logs-dir", default=os.getenv("AIVAX_INTEGRATION_BULK_DIR"))


def pytest_configure(config):
    if config.getoption("--run-all"):
        config.option.run_container_integration = True


def pytest_sessionstart(session):
    if not session.config.getoption("--run-all") or session.config.option.collectonly:
        return
    missing = [tool for tool in ("docker", "java", "javac") if not shutil.which(tool)]
    if missing:
        raise pytest.UsageError("전체 테스트 필수 도구 없음: " + ", ".join(missing))
    checks = [
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        ["docker", "compose", "version"],
        ["bash", str(ROOT / "src/extensions/cso/java/build.sh")],
    ]
    for command in checks:
        try:
            subprocess.run(
                command, capture_output=True, text=True, check=True, timeout=30
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise pytest.UsageError(
                f"전체 테스트 준비 실패: {' '.join(command)}\n{error.stderr or error}"
            ) from error
    if int(Path("/proc/sys/vm/max_map_count").read_text()) < 262144:
        raise pytest.UsageError("OpenSearch 테스트에 vm.max_map_count >= 262144 필요")


@pytest.fixture(scope="module")
def isolated_api_environment(tmp_path_factory, block_phrase_file):
    """Start a disposable Valkey and avoid loading development/production settings."""
    directory = tmp_path_factory.mktemp("pipeline-api")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    binary = shutil.which("valkey-server")
    if binary is None:
        pytest.fail("valkey-server must be installed on the server and available in PATH")
    with (directory / "valkey.log").open("w") as log:
        process = subprocess.Popen(
            [
                str(binary),
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
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
        client = Valkey(host="127.0.0.1", port=port, socket_timeout=0.2)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    if client.ping():
                        break
                except Exception:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        pytest.fail((directory / "valkey.log").read_text())
                time.sleep(0.02)
            mode_file = directory / "mode.json"
            mode_file.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "mode": "inline",
                        "filters": {
                            name: {"enabled": False}
                            for name in ("regex", "multimodal", "slm", "secuxper")
                        },
                    }
                )
            )
            env_file = directory / "test.env"
            env_file.write_text("")
            yield {
                "AIVAX_PIPELINE_BLOCK_PHRASES_FILE": str(block_phrase_file),
                "AIVAX_PIPELINE_ENV_FILE": str(env_file),
                "AIVAX_PIPELINE_MODE_FILE": str(mode_file),
                "AIVAX_PIPELINE_VALKEY_HOST": "127.0.0.1",
                "AIVAX_PIPELINE_VALKEY_PORT": str(port),
                "AIVAX_PIPELINE_VALKEY_PASSWORD": "",
                "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND": "memory",
                "AIVAX_PIPELINE_CSO_ENABLED": "false",
                "AIVAX_PIPELINE_CDS_DELIVERY_ENABLED": "false",
                "AIVAX_PIPELINE_LOG_BULK_BACKUP_DIR": str(directory / "archive"),
            }
        finally:
            client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
