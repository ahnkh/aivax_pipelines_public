from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any
from unittest.mock import Mock

from src.adapters.analysis_log_delivery import (
    HttpCdsDeliveryAdapter,
    LocalFileCdsAnalysisLogQueueAdapter,
)
from src.adapters.notification_audit import HttpManagementAuditNotificationAdapter
from src.application.services.analysis_log_merge import (
    AnalysisLogMergeService,
)
from src.application.use_cases.send_pending_cds_logs import (
    SendPendingCdsLogsUseCase,
)
from src.core.constants import ERR_OK
from src.domain.analysis_log_delivery.ports import (
    AnalysisLogDeliveryPort,
)
from src.domain.analysis_log_merge.ports import (
    AnalysisLogMergeStorePort,
)


class _CdsTestServer:
    def __init__(
        self,
        *,
        failed_indexes: set[int] | None = None,
    ) -> None:
        self.failed_indexes = failed_indexes or set()
        self.requests: list[dict[str, Any]] = []


@contextmanager
def _run_test_server(
    state: _CdsTestServer,
) -> Generator[str, None, None]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(
                self.headers.get("Content-Length", "0"),
            )
            body = json.loads(
                self.rfile.read(content_length),
            )

            state.requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body,
                }
            )

            if self.path == "/api/ext/v1/traffic-logs":
                rows = []
                inserted = 0

                for index, _ in enumerate(body):
                    if index in state.failed_indexes:
                        rows.append(
                            {
                                "index": index,
                                "status": "ERROR",
                                "message": "test failure",
                            }
                        )
                    else:
                        inserted += 1
                        rows.append(
                            {
                                "index": index,
                                "status": "OK",
                                "id": f"cds-{index}",
                            }
                        )

                self._send_json(
                    200,
                    {
                        "received": len(body),
                        "inserted": inserted,
                        "failed": len(body) - inserted,
                        "rows": rows,
                    },
                )
                return

            if self.path == "/api/v1/internal/audits":
                self._send_json(
                    201,
                    {
                        "statusCode": 201,
                        "message": "감사로그를 작성했습니다.",
                    },
                )
                return

            self._send_json(
                404,
                {
                    "message": "not found",
                },
            )

        def _send_json(
            self,
            status_code: int,
            body: dict[str, Any],
        ) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status_code)
            self.send_header(
                "Content-Type",
                "application/json",
            )
            self.send_header(
                "Content-Length",
                str(len(payload)),
            )
            self.end_headers()
            self.wfile.write(payload)

        def log_message(
            self,
            format: str,
            *args: object,
        ) -> None:
            # 테스트 출력에 HTTP server access log를 남기지 않는다.
            return

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        Handler,
    )
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    thread.start()

    host, port = server.server_address

    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _document(message_id: str) -> dict[str, Any]:
    return {
        "@timestamp": "2026-09-23T00:27:46Z",
        "message_id": message_id,
        "client_host": "127.0.0.1",
        "user": {
            "email": "tester@example.com",
        },
        "mode": "block",
        "policy": {
            "policy_name": "이메일",
            "category": "개인정보",
        },
        "output_text": "차단되었습니다.",
    }


def _create_merge_service(
    spool: LocalFileCdsAnalysisLogQueueAdapter,
) -> AnalysisLogMergeService:
    analysis_delivery = Mock(
        spec=AnalysisLogDeliveryPort,
    )
    analysis_delivery.deliver.return_value = True

    return AnalysisLogMergeService(
        Mock(spec=AnalysisLogMergeStorePort),
        analysis_delivery,
        additional_deliveries=(spool,),
    )


def _create_sender(
    spool: LocalFileCdsAnalysisLogQueueAdapter,
    base_url: str,
) -> tuple[
    SendPendingCdsLogsUseCase,
    HttpCdsDeliveryAdapter,
    HttpManagementAuditNotificationAdapter,
]:
    delivery = HttpCdsDeliveryAdapter(
        f"{base_url}/api/ext/v1/traffic-logs",
        "integration-test-key",
        src_port=0,
        dst_host="example.com",
        dst_port=443,
    )
    audit = HttpManagementAuditNotificationAdapter(base_url)

    use_case = SendPendingCdsLogsUseCase(
        spool,
        delivery,
        audit=audit,
    )
    return use_case, delivery, audit


def test_completed_analysis_log_is_sent_and_removed(
    tmp_path: Path,
) -> None:
    state = _CdsTestServer()

    with _run_test_server(state) as base_url:
        spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)
        merge_service = _create_merge_service(spool)

        assert (
            merge_service.deliver(
                _document("message-1"),
            )
            == ERR_OK
        )
        assert len(tuple(tmp_path.glob("cds_log#*.json"))) == 1

        sender, delivery, audit = _create_sender(
            spool,
            base_url,
        )

        try:
            assert sender.execute() == 1
        finally:
            delivery.close()
            audit.close()

    assert tuple(tmp_path.glob("cds_log#*.json")) == ()

    traffic_request = state.requests[0]
    assert traffic_request["path"] == ("/api/ext/v1/traffic-logs")
    assert traffic_request["headers"]["X-API-Key"] == ("integration-test-key")
    assert traffic_request["body"][0]["result"] == "B"
    assert traffic_request["body"][0]["detect"] == 4
    assert traffic_request["body"][0]["inspectResult"] == ("탐지: 이메일 처리: 차단")


def test_partial_failure_quarantines_failed_file_and_records_audit(
    tmp_path: Path,
) -> None:
    state = _CdsTestServer(failed_indexes={1})

    with _run_test_server(state) as base_url:
        spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)
        merge_service = _create_merge_service(spool)

        assert (
            merge_service.deliver(
                _document("message-1"),
            )
            == ERR_OK
        )
        assert (
            merge_service.deliver(
                _document("message-2"),
            )
            == ERR_OK
        )
        assert len(tuple(tmp_path.glob("cds_log#*.json"))) == 2

        sender, delivery, audit = _create_sender(
            spool,
            base_url,
        )

        try:
            assert sender.execute() == 1
        finally:
            delivery.close()
            audit.close()

    # 성공한 파일은 삭제되고 수정 없이는 성공할 수 없는 파일은 격리된다.
    assert tuple(tmp_path.glob("cds_log#*.json")) == ()
    assert len(tuple((tmp_path / "failed").glob("cds_log#*.json"))) == 1

    paths = [request["path"] for request in state.requests]
    assert paths == [
        "/api/ext/v1/traffic-logs",
        "/api/v1/internal/audits",
    ]

    audit_request = state.requests[1]
    assert audit_request["body"]["isSuccess"] is False
    assert audit_request["body"]["category"] == 4
    assert "실패 1건" in audit_request["body"]["message"]
