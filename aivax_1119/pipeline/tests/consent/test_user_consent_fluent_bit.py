"""실제 Fluent Bit binary로 파일 → bulk create/update 계약을 검증한다.

make test에서는 Docker를 사용한다. FLUENT_BIT_BIN으로 로컬 바이너리도 지정할 수 있다.
OpenSearch는 로컬 bulk API stub을 사용한다.
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

from src.adapters.analysis_log_delivery import FluentBitAnalysisLogDeliveryAdapter
from src.domain.analysis_log_delivery.models import MergedAnalysisLogDocument
from src.domain.analysis_log_delivery.models.user_consent import (
    CONSENT_LOG_TIMESTAMP_FIELD,
)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-05T23:59:59.123456Z",
        "2026-10-05T23:59:59Z",
    ],
)
def test_fluent_bit_updates_one_document_and_retries_initial_missing_document(
    tmp_path,
    timestamp,
    request,
):
    binary = os.environ.get("FLUENT_BIT_BIN")
    if not binary and not request.config.getoption("--run-all"):
        pytest.skip("Set FLUENT_BIT_BIN to run the file transport integration test")
    documents = {}
    operations = []
    condition = threading.Condition()
    failed_updates = 0

    class BulkHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            nonlocal failed_updates
            assert self.path == "/_bulk"
            lines = self.rfile.read(int(self.headers["Content-Length"])).splitlines()
            items = []
            with condition:
                for meta, record in zip(lines[::2], lines[1::2], strict=True):
                    operation, identity = next(iter(json.loads(meta).items()))
                    key = (identity["_index"], identity["_id"])
                    document = json.loads(record)
                    operations.append((operation, key))
                    status = 200
                    if operation == "create":
                        if key in documents:
                            status = 409
                        else:
                            documents[key] = document
                            status = 201
                    elif operation == "update":
                        # Force the first attempt to reproduce tail/flush ordering
                        # without relying on which file Fluent Bit reads first.
                        if failed_updates == 0 or key not in documents:
                            failed_updates += 1
                            status = 404
                        else:
                            assert "doc_as_upsert" not in document
                            documents[key].update(document["doc"])
                    else:
                        raise AssertionError(operation)
                    result = {"_index": key[0], "_id": key[1], "status": status}
                    if status == 404:
                        result["error"] = {
                            "type": "document_missing_exception",
                            "reason": "not flushed yet",
                        }
                    elif status == 409:
                        result["error"] = {
                            "type": "version_conflict_engine_exception",
                            "reason": "already exists",
                        }
                    items.append({operation: result})
                condition.notify_all()
            payload = json.dumps(
                {
                    "errors": any(
                        next(iter(i.values()))["status"] >= 400 for i in items
                    ),
                    "items": items,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), BulkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    repo = Path(__file__).resolve().parents[4]
    config = (repo / "infra/fluent-bit/fluent-bit.conf").read_text()
    config = config.replace("tls On", "tls Off").replace(
        "Flush        5", "Flush        1"
    )
    config = config.replace("Refresh_Interval 5", "Refresh_Interval 1")
    config_path = tmp_path / "fluent-bit.conf"
    config_path.write_text(config)
    parsers_path = tmp_path / "parsers.conf"
    parsers_path.write_text((repo / "infra/fluent-bit/parsers.conf").read_text())
    bulk_dir = tmp_path / "logs"
    writer = FluentBitAnalysisLogDeliveryAdapter(bulk_dir)
    original = {
        "message_id": "mid-block",
        "@timestamp": timestamp,
        "mode": "block",
        "prompt": "original prompt",
        "output_text": "blocked",
        "filter_detect": [{"filter": "secuxper", "mode": "block"}],
    }
    assert writer.deliver(MergedAnalysisLogDocument(original))
    restarted = FluentBitAnalysisLogDeliveryAdapter(bulk_dir)
    merged_consent = MergedAnalysisLogDocument(
        {
            "message_id": "mid-block",
            "@timestamp": "2026-10-07T12:00:00.000000Z",
            "mode": "accept",
            "filter_detect": [],
            "output_text": "actual model response",
            "text_truncated_bytes": 21,
            "user_consent": "on",
            CONSENT_LOG_TIMESTAMP_FIELD: timestamp,
        }
    )
    assert restarted.deliver(merged_consent)
    environment = {
        **os.environ,
        "FLUENT_BIT_PARSERS_FILE": str(parsers_path),
        "FLUENT_BIT_LOG_FILE": str(tmp_path / "fluent-bit.log"),
        "FLUENT_BIT_INPUT_PATH": str(bulk_dir / "aivax_log#*.json"),
        "FLUENT_BIT_DB_PATH": str(tmp_path / "tail.db"),
        "OPENSEARCH_HOST": "127.0.0.1",
        "OPENSEARCH_PORT": str(server.server_port),
        "OPENSEARCH_USER": "test",
        "OPENSEARCH_PASS": "test",
    }
    container_name = f"pipeline-fluent-bit-{uuid4().hex[:12]}"
    command = (
        [binary]
        if binary
        else [
            "docker",
            "run",
            "--rm",
            "--network=host",
            "--name",
            container_name,
            "--volume",
            f"{tmp_path}:{tmp_path}:Z",
            *[
                argument
                for key in environment
                if key.startswith(("FLUENT_BIT_", "OPENSEARCH_"))
                for argument in ("--env", key)
            ],
            "fluent/fluent-bit:4.1.1@sha256:"
            "2a5cb41f99b7c5f3386bb34d42ce3b19eb47fbef6d93b64c0bd4afe9e6ec389c",
        ]
    )
    with (tmp_path / "process.log").open("w") as log:
        process = subprocess.Popen(
            [*command, "-c", str(config_path)], env=environment, stdout=log, stderr=log
        )
        try:

            def wait_for_response():
                deadline = time.monotonic() + 30
                with condition:
                    while time.monotonic() < deadline:
                        if any(
                            d.get("user_consent") == "on" for d in documents.values()
                        ):
                            return
                        if process.poll() is not None:
                            break
                        condition.wait(timeout=0.2)
                pytest.fail(
                    (tmp_path / "process.log").read_text()
                    + (
                        (tmp_path / "fluent-bit.log").read_text()
                        if (tmp_path / "fluent-bit.log").exists()
                        else ""
                    )
                )

            wait_for_response()
            assert failed_updates >= 1
            assert list(documents) == [("aivax_log-20261005", "mid-block")]
            updated = next(iter(documents.values()))
            assert updated["output_text"] == "actual model response"
            assert updated["mode"] == "block"
            assert updated["filter_detect"] == original["filter_detect"]
            # Replay the patch and the original. create conflicts must not undo
            # consent, and neither operation may increase the document count.
            with condition:
                before = len(operations)
            assert writer.deliver(MergedAnalysisLogDocument(original))
            assert restarted.deliver(merged_consent)
            deadline = time.monotonic() + 10
            with condition:
                while len(operations) < before + 2 and time.monotonic() < deadline:
                    condition.wait(timeout=0.2)
                assert len(operations) >= before + 2
                assert len(documents) == 1
                assert (
                    next(iter(documents.values()))["output_text"]
                    == "actual model response"
                )
                updated = next(iter(documents.values()))
                assert updated["mode"] == "block"
                assert updated["filter_detect"] == original["filter_detect"]
                assert updated["user_consent"] == "on"
                assert "effective_decision" not in updated
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            finally:
                if not binary:
                    subprocess.run(
                        ["docker", "rm", "--force", container_name],
                        capture_output=True,
                        timeout=15,
                    )
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
