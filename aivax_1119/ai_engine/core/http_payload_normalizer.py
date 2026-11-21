"""Conservative HTTP body cleanup shared by INLINE and MIRROR evaluation.

The policy engine normally receives an application body.  Some decrypt/mirror
sources can instead deliver a response head, an HTTP/1 chunked body, or a
complete JSON body followed by the next keep-alive request.  This module only
changes payloads when the framing is unambiguous; otherwise it reports a
suspect payload and leaves the bytes untouched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional, Tuple


_HTTP_START_RE = re.compile(
    rb"^(?:HTTP/1\.[01]\s+\d{3}(?:\s+[^\r\n]*)?|"
    rb"(?:GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+\S+\s+HTTP/1\.[01])\r?\n",
    re.IGNORECASE,
)
_NEXT_REQUEST_RE = re.compile(
    rb"\r?\n(?:GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+\S+\s+HTTP/1\.[01]\r?\n",
    re.IGNORECASE,
)
_CHUNK_LINE_RE = re.compile(rb"^([0-9A-Fa-f]{1,16})(?:;[^\r\n]*)?\r\n")
_BINARY_CONTENT_PREFIXES = (
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "image/",
    "audio/",
    "video/",
    "font/",
)


@dataclass
class PayloadNormalizationInfo:
    header_stripped: bool = False
    dechunked: bool = False
    stitch_trimmed: bool = False
    suspect: bool = False
    reason: str = ""
    original_bytes: int = 0
    normalized_bytes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _is_binary_or_upload(content_type: str) -> bool:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return ct.startswith("multipart/") or any(
        ct.startswith(prefix) for prefix in _BINARY_CONTENT_PREFIXES
    )


def _strip_http_head(payload: bytes) -> Tuple[bytes, bool]:
    if not _HTTP_START_RE.match(payload):
        return payload, False
    split = payload.find(b"\r\n\r\n")
    sep_len = 4
    if split < 0:
        split = payload.find(b"\n\n")
        sep_len = 2
    if split < 0:
        return payload, False
    return payload[split + sep_len :], True


def _dechunk(payload: bytes) -> Tuple[bytes, bool, bool]:
    """Return (body, changed, malformed).

    Parsing is attempted only when the first bytes are a valid chunk-size line.
    A transformation is committed only after the terminating zero chunk.
    """
    if not _CHUNK_LINE_RE.match(payload):
        return payload, False, False
    pos = 0
    chunks = []
    while pos < len(payload):
        match = _CHUNK_LINE_RE.match(payload[pos:])
        if not match:
            return payload, False, True
        size = int(match.group(1), 16)
        pos += match.end()
        if size == 0:
            # Optional trailers are deliberately ignored. The body is complete.
            return b"".join(chunks), True, False
        end = pos + size
        if end > len(payload):
            return payload, False, True
        chunks.append(payload[pos:end])
        pos = end
        if payload[pos : pos + 2] == b"\r\n":
            pos += 2
        elif payload[pos : pos + 1] == b"\n":
            pos += 1
        else:
            return payload, False, True
    return payload, False, True


def _trim_stitched_request(payload: bytes) -> Tuple[bytes, bool]:
    """Trim a second HTTP request only after a complete JSON value."""
    stripped = payload.lstrip()
    leading = len(payload) - len(stripped)
    if not stripped.startswith((b"{", b"[")):
        return payload, False
    try:
        text = stripped.decode("utf-8")
        _, end_chars = json.JSONDecoder().raw_decode(text)
        json_bytes = text[:end_chars].encode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload, False
    suffix = stripped[len(json_bytes) :]
    if not suffix:
        return payload, False
    candidate = suffix.lstrip(b"\r\n\t ")
    if not re.match(
        rb"^(?:GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+\S+\s+HTTP/1\.[01]\r?\n",
        candidate,
        re.IGNORECASE,
    ):
        return payload, False
    return payload[:leading] + json_bytes, True


def _suspect_reason(payload: bytes) -> Optional[str]:
    stripped = payload.lstrip()
    if _HTTP_START_RE.match(stripped):
        return "embedded_http_head"
    if _NEXT_REQUEST_RE.search(payload):
        return "stitched_http_request"
    if _CHUNK_LINE_RE.match(stripped):
        return "malformed_or_incomplete_chunked"
    sample = stripped[:512]
    controls = sum(1 for value in sample if value < 32 and value not in (9, 10, 13))
    if controls >= 8:
        return "binary_control_bytes"
    return None


def normalize_http_payload(
    payload: bytes,
    *,
    unit_type: str = "",
    content_type: str = "",
) -> tuple[bytes, Dict[str, Any]]:
    """Normalize unambiguous HTTP framing without touching upload/binary bodies."""
    raw = bytes(payload or b"")
    info = PayloadNormalizationInfo(
        original_bytes=len(raw),
        normalized_bytes=len(raw),
    )
    if not raw or _is_binary_or_upload(content_type):
        return raw, info.to_dict()
    # Masked WS frames start with opcode/mask bytes; do not flag them as HTTP binary.
    if str(unit_type or "").lower() == "ws_frame":
        return raw, info.to_dict()

    normalized, stripped = _strip_http_head(raw)
    info.header_stripped = stripped

    dechunked, changed, malformed = _dechunk(normalized)
    if changed:
        normalized = dechunked
        info.dechunked = True
    elif malformed:
        info.suspect = True
        info.reason = "malformed_or_incomplete_chunked"

    normalized, trimmed = _trim_stitched_request(normalized)
    info.stitch_trimmed = trimmed

    reason = _suspect_reason(normalized)
    if reason:
        info.suspect = True
        if not info.reason:
            info.reason = reason
    info.normalized_bytes = len(normalized)
    return normalized, info.to_dict()
