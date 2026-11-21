"""Observe incoming ASGI messages without consuming or changing the request."""

import json
import logging
from urllib.parse import parse_qsl

from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_BODY_LOG_LIMIT = 64 * 1024
_MULTIPART_PART_LIMIT = 100


def _json_or_text(value: str) -> object:
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return value


def _form_body(fields: list[tuple[str, str]]) -> dict[str, object]:
    grouped: dict[str, list[object]] = {}
    for name, value in fields:
        field = _json_or_text(value) if value.lstrip().startswith(("{", "[")) else value
        grouped.setdefault(name, []).append(field)
    return {
        name: values[0] if len(values) == 1 else values
        for name, values in grouped.items()
    }


class _MultipartLog:
    def __init__(self, content_type: str) -> None:
        _, options = parse_options_header(content_type)
        self.parts: list[dict[str, str | int]] = []
        self.remaining = _BODY_LOG_LIMIT
        self.parser = MultipartParser(
            options[b"boundary"],
            {
                name: getattr(self, name)
                for name in (
                    "on_part_begin",
                    "on_header_field",
                    "on_header_value",
                    "on_header_end",
                    "on_headers_finished",
                    "on_part_data",
                    "on_part_end",
                )
            },
        )

    def on_part_begin(self) -> None:
        if len(self.parts) >= _MULTIPART_PART_LIMIT:
            raise ValueError("multipart log part limit exceeded")
        self.headers: dict[bytes, bytes] = {}
        self.header_field = bytearray()
        self.header_value = bytearray()
        self.value = bytearray()
        self.size = 0

    def _append(self, target: bytearray, data: bytes, start: int, end: int) -> None:
        if end - start > self.remaining:
            raise ValueError("multipart log byte limit exceeded")
        target.extend(data[start:end])
        self.remaining -= end - start

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._append(self.header_field, data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._append(self.header_value, data, start, end)

    def on_header_end(self) -> None:
        self.headers[bytes(self.header_field).lower()] = bytes(self.header_value)
        self.header_field.clear()
        self.header_value.clear()

    def on_headers_finished(self) -> None:
        _, self.options = parse_options_header(self.headers.get(b"content-disposition"))

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        self.size += end - start
        if b"filename" not in self.options:
            self._append(self.value, data, start, end)

    def on_part_end(self) -> None:
        part: dict[str, str | int] = {
            "name": self.options.get(b"name", b"").decode("utf-8", errors="replace"),
        }
        if b"filename" in self.options:
            part.update(
                filename=self.options[b"filename"].decode("utf-8", errors="replace"),
                size=self.size,
                content_type=self.headers.get(b"content-type", b"").decode("latin-1"),
            )
        else:
            part["value"] = self.value.decode("utf-8", errors="replace")
        self.parts.append(part)


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp, logger: logging.Logger) -> None:
        self.app = app
        self.logger = logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.logger.isEnabledFor(logging.DEBUG):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_type = headers.get("content-type", "")
        details: dict[str, object] = {
            "method": scope["method"],
            "path": scope["path"],
        }
        query = scope.get("query_string", b"").decode("utf-8", errors="replace")
        if query:
            details["query"] = query

        media_type = content_type.partition(";")[0].strip().lower()
        is_json = media_type == "application/json" or media_type.endswith("+json")
        is_text = (
            media_type.startswith("text/")
            or media_type == "application/x-www-form-urlencoded"
            or is_json
        )
        multipart = None
        error = None
        if media_type == "multipart/form-data":
            try:
                multipart = _MultipartLog(content_type)
            except (KeyError, ValueError) as exc:
                error = str(exc)
        body = bytearray()
        size = 0
        complete = False
        read = False
        emitted = False

        def log_request() -> None:
            nonlocal emitted
            if emitted:
                return
            emitted = True
            if multipart is not None:
                fields = [
                    (str(part["name"]), str(part["value"]))
                    for part in multipart.parts
                    if "value" in part
                ]
                if fields:
                    details["body"] = _form_body(fields)
                files = [
                    {key: value for key, value in part.items() if key != "name"}
                    for part in multipart.parts
                    if "filename" in part
                ]
                if files:
                    details["files"] = files
            elif is_text and size:
                text = body.decode("utf-8", errors="replace")
                if is_json:
                    details["body"] = _json_or_text(text)
                elif media_type == "application/x-www-form-urlencoded":
                    details["body"] = _form_body(
                        parse_qsl(text, keep_blank_values=True)
                    )
                else:
                    details["body"] = text
                if size > _BODY_LOG_LIMIT:
                    details["body_truncated"] = True
                    details["body_bytes"] = size
            elif size:
                details["body_omitted"] = "binary or unsupported content type"
                details["content_type"] = content_type
            if not read and (
                content_type
                or headers.get("content-length", "0") != "0"
                or "transfer-encoding" in headers
            ):
                details["body_read"] = False
            elif read and not complete:
                details["body_complete"] = False
            if error is not None:
                details["body_log_error"] = error
            try:
                self.logger.debug(
                    "http request %s",
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                )
            except Exception:
                # Diagnostic logging must not fail requests or mask application errors.
                pass

        async def receive_logged() -> Message:
            nonlocal size, complete, read, error
            message = await receive()
            if message["type"] == "http.request":
                read = True
                chunk = message.get("body", b"")
                size += len(chunk)
                complete = not message.get("more_body", False)
                if multipart is not None and error is None:
                    try:
                        multipart.parser.write(chunk)
                    except ValueError as exc:
                        error = str(exc)
                elif is_text:
                    body.extend(chunk[: max(0, _BODY_LOG_LIMIT - len(body))])
                if complete:
                    log_request()
            return message

        try:
            await self.app(scope, receive_logged, send)
        finally:
            log_request()
