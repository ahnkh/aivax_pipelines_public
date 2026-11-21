import asyncio
import json
import logging

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from src.core.http.request_logging_middleware import RequestLoggingMiddleware


@pytest.fixture
def logger(caplog):
    logger = logging.getLogger("test.pipeline.http_requests")
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        yield logger


@pytest.fixture
def client(logger):
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"])
    app.add_middleware(RequestLoggingMiddleware, logger=logger)

    @app.post("/echo")
    async def echo(request: Request):
        return Response(await request.body())

    @app.post("/validated")
    async def validated(item: dict):
        return item

    @app.post("/upload")
    async def upload(request: Request):
        async with request.form() as form:
            file = form["files"]
            assert isinstance(file, UploadFile)
            return {
                "payload": form["payload"],
                "filename": file.filename,
                "size": len(await file.read()),
            }

    @app.get("/error")
    async def error():
        raise RuntimeError("application failure")

    with TestClient(app) as client:
        yield client


def logged(caplog):
    prefix = "http request "
    return [
        json.loads(record.getMessage()[len(prefix) :])
        for record in caplog.records
        if record.name == "test.pipeline.http_requests"
        and record.getMessage().startswith(prefix + "{")
    ]


def test_logs_json_and_query_without_changing_the_request(client, caplog):
    payload = {"prompt": "요청 본문", "message_id": "debug-test"}
    response = client.post("/echo?mode=test", json=payload)

    assert response.status_code == 200
    assert response.json() == payload
    assert logged(caplog) == [
        {"method": "POST", "path": "/echo", "query": "mode=test", "body": payload}
    ]


@pytest.mark.parametrize("payload", [["hello", "world"], "hello", False, 0, None])
def test_json_body_preserves_its_type(client, caplog, payload):
    content = json.dumps(payload)
    response = client.post(
        "/echo", content=content, headers={"content-type": "application/json"}
    )

    assert response.json() == payload
    assert logged(caplog) == [{"method": "POST", "path": "/echo", "body": payload}]


def test_logs_invalid_json_before_validation_returns_422(client, caplog):
    response = client.post(
        "/validated", content="{invalid", headers={"content-type": "application/json"}
    )

    assert response.status_code == 422
    assert logged(caplog)[0]["body"] == "{invalid"


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/missing"])
def test_logs_requests_even_when_the_app_does_not_read_a_body(client, caplog, path):
    response = client.get(path)

    assert response.status_code == (404 if path == "/missing" else 200)
    assert logged(caplog) == [{"method": "GET", "path": path}]


def test_marks_unread_body_when_the_app_does_not_consume_it(client, caplog):
    response = client.post("/missing", json={"prompt": "unread"})

    assert response.status_code == 404
    assert logged(caplog) == [
        {"method": "POST", "path": "/missing", "body_read": False}
    ]


def test_logs_cors_preflight(client, caplog):
    response = client.options(
        "/echo",
        headers={
            "origin": "https://example.com",
            "access-control-request-method": "POST",
        },
    )

    assert response.status_code == 200
    assert logged(caplog) == [{"method": "OPTIONS", "path": "/echo"}]


def test_logs_upload_metadata_and_payload_without_file_bytes(client, caplog):
    contents = b"private-file-contents\x00\xff" * 10000
    payload = '{"prompt":"test upload"}'
    response = client.post(
        "/upload",
        data={"payload": payload},
        files={"files": ("sample.bin", contents, "application/octet-stream")},
    )

    assert response.json() == {
        "payload": payload,
        "filename": "sample.bin",
        "size": len(contents),
    }
    assert logged(caplog) == [
        {
            "method": "POST",
            "path": "/upload",
            "body": {"payload": {"prompt": "test upload"}},
            "files": [
                {
                    "filename": "sample.bin",
                    "size": len(contents),
                    "content_type": "application/octet-stream",
                }
            ],
        }
    ]
    assert "private-file-contents" not in caplog.text


def test_truncates_only_the_logged_body(client, caplog):
    body = "a" * (64 * 1024) + "tail"
    response = client.post(
        "/echo", content=body, headers={"content-type": "text/plain"}
    )

    assert response.text == body
    entry = logged(caplog)[0]
    assert len(entry["body"]) == 64 * 1024
    assert entry["body_truncated"] is True
    assert entry["body_bytes"] == len(body)


def test_multipart_log_limit_does_not_change_upload_handling(client, caplog):
    payload = "a" * (64 * 1024 + 1)
    response = client.post(
        "/upload", data={"payload": payload}, files={"files": ("test.txt", b"ok")}
    )

    assert response.status_code == 200
    assert response.json()["payload"] == payload
    assert "body_log_error" in logged(caplog)[0]


def test_invalid_multipart_logging_does_not_change_the_request(client, caplog):
    response = client.post(
        "/echo",
        content=b"invalid multipart",
        headers={"content-type": "multipart/form-data"},
    )

    assert response.status_code == 200
    assert response.content == b"invalid multipart"
    assert "body_log_error" in logged(caplog)[0]


def test_omits_binary_body(client, caplog):
    contents = b"binary-secret\x00\xff"
    response = client.post(
        "/echo", content=contents, headers={"content-type": "application/octet-stream"}
    )

    assert response.content == contents
    assert "body_omitted" in logged(caplog)[0]
    assert "binary-secret" not in caplog.text


def test_info_level_disables_request_logs(client, logger, caplog):
    logger.setLevel(logging.INFO)
    response = client.post("/echo", json={"prompt": "hello"})

    assert response.status_code == 200
    assert logged(caplog) == []


@pytest.fixture
def broken_log_sink(logger, monkeypatch):
    class FailingHandler(logging.Handler):
        def emit(self, record):
            raise OSError("log sink unavailable")

    monkeypatch.setattr(logger, "handlers", [FailingHandler()])


def test_log_sink_failure_does_not_fail_the_request(client, broken_log_sink):
    response = client.post("/echo", json={"prompt": "hello"})

    assert response.status_code == 200
    assert response.json() == {"prompt": "hello"}


def test_log_sink_failure_does_not_replace_application_error(client, broken_log_sink):
    with pytest.raises(RuntimeError, match="application failure"):
        client.get("/error")


def test_application_errors_still_propagate_and_are_logged(client, caplog):
    with pytest.raises(RuntimeError, match="application failure"):
        client.get("/error")

    assert logged(caplog)[0]["path"] == "/error"


def test_chunked_body_is_forwarded_and_logged_in_order(logger, caplog):
    chunks = iter(
        [
            {"type": "http.request", "body": b'{"prompt":', "more_body": True},
            {"type": "http.request", "body": b'"chunked"}', "more_body": False},
        ]
    )
    received = []

    async def receive():
        return next(chunks)

    async def send(message):
        pass

    async def app(scope, receive, send):
        received.append(await receive())
        assert logged(caplog) == []
        received.append(await receive())
        assert logged(caplog) == [
            {"method": "POST", "path": "/stream", "body": {"prompt": "chunked"}}
        ]

    asyncio.run(
        RequestLoggingMiddleware(app, logger)(
            {
                "type": "http",
                "method": "POST",
                "path": "/stream",
                "headers": [(b"content-type", b"application/json")],
            },
            receive,
            send,
        )
    )

    assert received == [
        {"type": "http.request", "body": b'{"prompt":', "more_body": True},
        {"type": "http.request", "body": b'"chunked"}', "more_body": False},
    ]
    assert logged(caplog)[0]["body"] == {"prompt": "chunked"}


def test_multipart_headers_and_file_data_can_span_chunks(logger, caplog):
    body = (
        b'--boundary\r\nContent-Disposition: form-data; name="payload"\r\n\r\n'
        b"test payload\r\n--boundary\r\n"
        b'Content-Disposition: form-data; name="files"; filename="test.bin"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
        b"private-file-data\x00\xff\r\n--boundary--\r\n"
    )
    chunks = iter(body[index : index + 3] for index in range(0, len(body), 3))
    received = bytearray()

    async def receive():
        chunk = next(chunks, b"")
        return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}

    async def send(message):
        pass

    async def app(scope, receive, send):
        while True:
            message = await receive()
            received.extend(message["body"])
            if not message["more_body"]:
                break

    asyncio.run(
        RequestLoggingMiddleware(app, logger)(
            {
                "type": "http",
                "method": "POST",
                "path": "/upload",
                "headers": [
                    (b"content-type", b"multipart/form-data; boundary=boundary")
                ],
            },
            receive,
            send,
        )
    )

    assert received == body
    assert logged(caplog) == [
        {
            "method": "POST",
            "path": "/upload",
            "body": {"payload": "test payload"},
            "files": [
                {
                    "filename": "test.bin",
                    "size": 19,
                    "content_type": "application/octet-stream",
                }
            ],
        }
    ]
    assert "private-file-data" not in caplog.text


def test_form_fields_preserve_repeated_and_empty_values(client, caplog):
    response = client.post(
        "/echo",
        content="tag=one&tag=two&empty=&count=123",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.text == "tag=one&tag=two&empty=&count=123"
    assert logged(caplog)[0]["body"] == {
        "tag": ["one", "two"],
        "empty": "",
        "count": "123",
    }


def test_newlines_remain_within_one_log_line(client, caplog):
    client.post("/echo", json={"prompt": "hello\nworld"})

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "test.pipeline.http_requests"
    ]
    assert len(messages) == 1
    assert "\n" not in messages[0]
    assert logged(caplog)[0]["body"] == {"prompt": "hello\nworld"}
