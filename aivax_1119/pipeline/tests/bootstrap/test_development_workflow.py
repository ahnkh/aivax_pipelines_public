from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scripts import smoke_pipeline

PIPELINE_ROOT = Path(__file__).resolve().parents[2]


def _requirements(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_install_requirements_are_fully_pinned() -> None:
    for relative_path in (
        "requirements.txt",
        "requirements-dev.txt",
    ):
        for requirement in _requirements(PIPELINE_ROOT / relative_path):
            assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[^=\s]+", requirement), (
                f"{relative_path} must pin every installable package: {requirement}"
            )


def test_makefile_exposes_setup_check_test_and_smoke_workflow() -> None:
    makefile = (PIPELINE_ROOT / "Makefile").read_text(encoding="utf-8")

    for target in (
        "setup",
        "check",
        "test",
        "test-unit",
        "smoke",
        "dev",
        "prod",
        "wheels",
    ):
        assert re.search(rf"^{target}:", makefile, re.MULTILINE)

    for native_thread_variable in (
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        assert f"{native_thread_variable} ?= 1" in makefile
    assert 'taskset -c "$(PIPELINE_CPUSET)"' in makefile

    assert "requirements-dev.txt" in (
        PIPELINE_ROOT / "scripts" / "bootstrap_venv.sh"
    ).read_text(encoding="utf-8")
    assert "pip==25.2" in _requirements(PIPELINE_ROOT / "requirements-dev.txt")
    assert (PIPELINE_ROOT / "scripts" / "smoke_pipeline.py").is_file()


def test_development_document_describes_the_current_test_workflow() -> None:
    document = (PIPELINE_ROOT / "README.md").read_text(encoding="utf-8")

    assert "make setup-dev" in document
    assert "make test-unit" in document
    assert "requirements-dev.txt" in document


def test_default_collection_skips_container_tests_without_option_errors() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "-k",
            "test_open_file_policy_match_returns_user_consent_notice",
        ],
        cwd=PIPELINE_ROOT,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[100%]" in result.stdout
    assert "no option named" not in result.stdout + result.stderr


def test_smoke_check_validates_server_status_and_filter_routes() -> None:
    responses = [
        {"status": True},
        {"paths": {"/v1/filter/multiple_filter": {}, "/v1/filter/output_filter": {}}},
    ]

    with patch.object(smoke_pipeline, "_load_json", side_effect=responses):
        assert smoke_pipeline.main() == 0


def test_smoke_script_runs_from_the_scripts_directory_against_a_live_http_server() -> (
    None
):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            response = (
                {"status": True}
                if self.path == "/"
                else {
                    "paths": {
                        "/v1/filter/multiple_filter": {},
                        "/v1/filter/output_filter": {},
                    }
                }
                if self.path == "/openapi.json"
                else None
            )
            if response is None:
                self.send_error(404)
                return
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        completed = subprocess.run(
            [sys.executable, "scripts/smoke_pipeline.py"],
            cwd=PIPELINE_ROOT,
            env={
                **os.environ,
                "AIVAX_PIPELINE_SMOKE_URL": f"http://127.0.0.1:{server.server_port}",
            },
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert completed.returncode == 0, completed.stderr
    assert '"ok": true' in completed.stdout
