#!/usr/bin/env python3
"""
Minimal AI policy engine server for SSLproxy.

Protocol:
- Unix domain socket stream
- Request: 4-byte big-endian length + UTF-8 JSON
- Response: 4-byte big-endian length + UTF-8 JSON

Request JSON (from SSLproxy):
{
  "request_id": "...",
  "service_id": "gpt",
  "direction": "request|response",
  "protocol": "https",
  "content_type": "...",
  "unit_type": "json_body|sse_event|...",
  "meta": {"host":"...", "client_ip":"...", "path":"...", "method":"...", "status":"..."},
  "payload": {"raw_b64":"..."}
}

Response JSON (to SSLproxy):
{
  "request_id": "...",
  "action": "ALLOW|MASK|REWRITE|BLOCK",
  "reason": "..."
}
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import struct
import sys
import time
import zlib
from threading import Lock
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import unquote

try:
    import aiohttp
except Exception:
    aiohttp = None

from ai_service_codes import resolve_ai_service_code
try:
    from http_payload_normalizer import normalize_http_payload
except ImportError:  # package import in tests/tools
    from .http_payload_normalizer import normalize_http_payload

try:
    from block_copy import (
        AIVAX_BLOCK_MODEL_INSTRUCTION,
        BLOCK_TITLE as _BLOCK_TITLE,
        CSO_BLOCK_TITLE as _CSO_BLOCK_TITLE,
        detect_type_label as _detect_type_label,
        find_footer as _find_footer,
        message_footer as _message_footer,
        format_guideline_notice as _format_guideline_notice,
        format_guideline_cso_notice_for_message as _format_guideline_cso_notice,
        format_user_visible_block as _format_user_visible_block,
        guideline_notice_enabled as _guideline_notice_enabled,
        visible_block_titles as _visible_block_titles,
        headline_quote_mark as _headline_quote_mark,
        line_matches_phrase_slot as _line_matches_phrase_slot,
        lock_line_search as _lock_line_search,
        detect_line_search as _detect_line_search,
        warn_line_search as _warn_line_search,
        detect_type_in as _detect_type_in,
        detected_content_in as _detected_content_in,
        detected_content_span as _detected_content_span,
        instruction_for as _instruction_for,
        resolve_block_title as _resolve_block_title,
        DETECT_REASON_LABEL as _DETECT_REASON_LABEL,
        GEMINI_BLOCK_MODEL_INSTRUCTION,
        GPT_BLOCK_MODEL_INSTRUCTION,
        INPUT_PROMPT_LABEL as _INPUT_PROMPT_LABEL,
        MASK_TOKEN,
        PERPLEXITY_BLOCK_MODEL_INSTRUCTION,
        SNIPER_HEADER as _SNIPER_HEADER,
        USER_CONSENT_HINT as _USER_CONSENT_HINT,
        USER_CONSENT_HINT_MARKDOWN as _USER_CONSENT_HINT_MARKDOWN,
    )
except ImportError:
    from .block_copy import (
        AIVAX_BLOCK_MODEL_INSTRUCTION,
        BLOCK_TITLE as _BLOCK_TITLE,
        CSO_BLOCK_TITLE as _CSO_BLOCK_TITLE,
        detect_type_label as _detect_type_label,
        find_footer as _find_footer,
        message_footer as _message_footer,
        format_guideline_notice as _format_guideline_notice,
        format_guideline_cso_notice_for_message as _format_guideline_cso_notice,
        format_user_visible_block as _format_user_visible_block,
        guideline_notice_enabled as _guideline_notice_enabled,
        visible_block_titles as _visible_block_titles,
        headline_quote_mark as _headline_quote_mark,
        line_matches_phrase_slot as _line_matches_phrase_slot,
        lock_line_search as _lock_line_search,
        detect_line_search as _detect_line_search,
        warn_line_search as _warn_line_search,
        detect_type_in as _detect_type_in,
        detected_content_in as _detected_content_in,
        detected_content_span as _detected_content_span,
        instruction_for as _instruction_for,
        resolve_block_title as _resolve_block_title,
        DETECT_REASON_LABEL as _DETECT_REASON_LABEL,
        GEMINI_BLOCK_MODEL_INSTRUCTION,
        GPT_BLOCK_MODEL_INSTRUCTION,
        INPUT_PROMPT_LABEL as _INPUT_PROMPT_LABEL,
        MASK_TOKEN,
        PERPLEXITY_BLOCK_MODEL_INSTRUCTION,
        SNIPER_HEADER as _SNIPER_HEADER,
        USER_CONSENT_HINT as _USER_CONSENT_HINT,
        USER_CONSENT_HINT_MARKDOWN as _USER_CONSENT_HINT_MARKDOWN,
    )


def _normalize_upload_meta(meta: Dict[str, Any]) -> Dict[str, str]:
    response_headers = meta.get("response_headers") if isinstance(meta.get("response_headers"), dict) else {}
    upload_id = str(meta.get("upload_id") or response_headers.get("x-guploader-uploadid") or "").strip()
    if not upload_id:
        # Inline Gemini upload tee: upload_id often only on x-goog-upload-url.
        for key in ("x-goog-upload-url", "x-goog-upload-control-url"):
            val = str(response_headers.get(key) or "")
            m = re.search(r"upload_id=([^&\s\"'<>]+)", val, flags=re.IGNORECASE)
            if m:
                upload_id = str(m.group(1) or "").strip()
                break
    upload_filename = str(meta.get("upload_filename") or "").strip()
    if upload_id:
        meta["upload_id"] = upload_id
    if upload_filename:
        meta["upload_filename"] = upload_filename
    return {
        "upload_id": upload_id,
        "upload_filename": upload_filename,
    }


# 기본 유닉스 소켓 경로 (SSLproxy와 AI 엔진 간 통신에 사용)
DEFAULT_SOCKET = "/tmp/sslproxy-ai-engine.sock"
# Suricata Lua output → AI 엔진 (newline-delimited JSON, 프레임 없음)
# AIVAX_EVE_SOCKET 로 오버라이드 가능(다중 인스턴스/벤치용). 여기에 나열된 경로들은
# 모두 eve(라인 JSON) 프로토콜로 취급된다.
DEFAULT_EVE_SOCKET = os.environ.get("AIVAX_EVE_SOCKET", "/tmp/suricata-eve.sock")
_EVE_SOCKET_ALIASES = frozenset(
    p for p in os.environ.get("AIVAX_EVE_SOCKET_ALIASES", "").split(os.pathsep) if p
)

# 프레임 최대 크기. 정책 UDS JSON은 본문을 base64로 담으므로 256MiB 응답은
# 약 341MiB가 된다. Envoy policy kMaxFrameBytes(512MiB)와 맞춘다.
MAX_FRAME_SIZE = 512 * 1024 * 1024
SHUTDOWN_GRACE_SECONDS = 5.0

# --- eve(Lua JSON) 소비자 성능 튜닝 ---------------------------------------
# 수신 루프는 "소켓에서 라인을 뽑아 샤드 큐에 넣기"만 하고, 실제 무거운 처리는
# 워커가 담당한다. 이렇게 수신/처리를 분리해야 커널 recv 버퍼가 비워져
# 플러그인(non-blocking send)의 EAGAIN drop 이 사라진다.
#   AIVAX_CONSUMER_WORKERS : 워커(=샤드) 수. 0 이면 기존처럼 인라인 처리(안전 롤백).
#   AIVAX_CONSUMER_QUEUE   : 인프로세스 큐 총 용량(이벤트 수). 버스트 흡수량.
#   AIVAX_UDS_RCVBUF       : accept 된 eve 연결의 SO_RCVBUF (bytes).
CONSUMER_WORKERS = int(os.environ.get("AIVAX_CONSUMER_WORKERS", "4"))
CONSUMER_QUEUE = int(os.environ.get("AIVAX_CONSUMER_QUEUE", "20000"))
UDS_RCVBUF = int(os.environ.get("AIVAX_UDS_RCVBUF", str(8 * 1024 * 1024)))

# core/ 모듈 위치 · 레포 루트(ai_engine/) · adapters/
CORE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(CORE_DIR)
MIRROR_DIR = os.path.join(BASE_DIR, "mirror")

# 어댑터 디렉토리 경로
ADAPTERS_DIR = os.path.join(BASE_DIR, "adapters")

# ChatGPT 요청 본문이 없을 때 강제로 사용할 텍스트 (기본: 빈 문자열)
DEFAULT_CHATGPT_FORCE_REQUEST_TEXT = ""

# ChatGPT BLOCK 시 Gemini와 같이 안내문+AIVAX_BLOCK_MODEL_INSTRUCTION를 요청에 붙일지 여부(기본: 켬)
DEFAULT_CHATGPT_BLOCK_SYSINSTRUCT = True

# 정책 서버의 기본 URL (외부 정책 엔진 주소)
DEFAULT_POLICY_SERVER_URL = "http://127.0.0.1"

# 정책 서버의 기본 포트 번호
DEFAULT_POLICY_SERVER_PORT = 9099

# 정책 API 호출의 기본 타임아웃(ms)
DEFAULT_POLICY_API_TIMEOUT_MS = 20000

# 정책 API로 동시에 처리 가능한 최대 요청 수
DEFAULT_POLICY_API_MAX_INFLIGHT = 1000

# HTTP 커넥션 풀의 연결 개수 제한
DEFAULT_POLICY_API_POOL_LIMIT = 1000

# 커넥션 풀에서 커넥션을 얻을 때까지 대기할 최대 시간(ms)
DEFAULT_POLICY_API_ACQUIRE_TIMEOUT_MS = 200

# 정책 서버 장애 발생 시 기본 정책 (open: 허용, closed: 차단)
DEFAULT_POLICY_API_FAILURE_POLICY = "open"

# 파이프라인 입력 필터 API의 기본 경로
DEFAULT_PIPELINE_INPUT_API_PATH = "/v1/filter/multiple_filter"

# 파이프라인 출력 필터 API의 기본 경로
DEFAULT_PIPELINE_OUTPUT_API_PATH = "/v1/filter/output_filter"

# Adapters store the model they saw on the request. The same keys are read for
# every service so a new adapter only has to set _llm_model.
_LLM_MODEL_KEYS = (
    "_llm_model",
    "_timely_model",
    "_chatgpt_model",
    "_claude_model",
    "_gemini_model",
    "_grok_model",
    "_perplexity_model",
    "_copilot_model",
    "_wrks_model",
    "_notion_model",
)
_LLM_MODEL_REJECT = frozenset(
    {"user", "assistant", "system", "model", "human", "bot", "tool", "none", "null"}
)
# Locates the JSON string at a "model" field. The captured value is stored as sent.
_LLM_MODEL_JSON_RE = re.compile(br'"model"\s*:\s*"([^"\\]{1,80})"')


def _adapter_model_name(value: Any) -> str:
    """Keep the model name that was sent. Role words are not model names."""
    text = str(value or "").strip()
    if not text or len(text) > 80 or text.lower() in _LLM_MODEL_REJECT:
        return ""
    if any(ord(ch) < 32 for ch in text):
        return ""
    return text


def _model_in_json(node: Any, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(node, dict):
        found = _adapter_model_name(node.get("model"))
        if found:
            return found
        for key, value in node.items():
            if key == "model":
                continue
            found = _model_in_json(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node[:20]:
            found = _model_in_json(item, depth + 1)
            if found:
                return found
    return ""


def llm_model_for_policy(req: Any, payload: bytes, adapter: Any = None) -> str:
    """Model id from the adapter, then from a model field in the body."""
    if isinstance(req, dict):
        for key in _LLM_MODEL_KEYS:
            found = _adapter_model_name(req.get(key))
            if found:
                return found
    if adapter is not None and hasattr(adapter, "extract_model"):
        try:
            found = _adapter_model_name(adapter.extract_model(req, payload))
        except Exception:
            logging.exception("adapter extract_model failed")
            found = ""
        if found:
            return found
    raw = payload or b""
    if raw[:1] in (b"{", b"["):
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            parsed = None
        found = _model_in_json(parsed)
        if found:
            return found
    sample = raw[:65536]
    match = _LLM_MODEL_JSON_RE.search(sample)
    if match:
        return _adapter_model_name(match.group(1).decode("utf-8", "replace"))
    return ""


def stamp_policy_model(req: Any, payload: bytes, policy_obj: Any, adapter: Any = None) -> None:
    """Copy a model id onto the pipeline payload when any service provided one."""
    if not isinstance(policy_obj, dict):
        return
    found = _adapter_model_name(policy_obj.get("model")) or llm_model_for_policy(
        req, payload, adapter
    )
    if not found:
        return
    policy_obj["model"] = found
    if isinstance(req, dict):
        req["_llm_model"] = found

DEFAULT_MIRROR_ACCEPT_TIMEOUT_MS = 5000
AIVAX_MODE_HEADER = "X-Aivax-Mode"


def _normalize_product_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    # proxy uses the inline policy path (full timeout, synchronous judgment).
    # Only mirror shortens the pipeline wait and accepts in the background.
    if mode in ("mirror", "inline", "proxy"):
        return mode
    return ""


def read_product_mode() -> str:
    env_mode = _normalize_product_mode(os.environ.get("AIVAX_PRODUCT_MODE"))
    if env_mode:
        return env_mode
    path = os.environ.get("AIVAX_MODE_JSON") or "/etc/aivax/mode.json"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            return _normalize_product_mode(payload.get("mode"))
    except Exception:
        return ""
    return ""


def iso_event_time(value: Any = None) -> str:
    if isinstance(value, (int, float)) and value > 0:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))
    text = str(value or "").strip()
    if text:
        return text
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

for _p in (BASE_DIR, CORE_DIR, MIRROR_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from account_lookup import configure as configure_account_lookup
from url_normalize import build_absolute_url
from meta_wal import get_wal as get_meta_wal
from adapter_registry import AdapterRegistry
from file_upload_manager import (
    UPLOAD_STATE_LOCK,
    get_saved_attachment,
    handle_upload_event,
    wait_for_saved_attachment,
)

# Large upload decode/persist must not run on the policy event loop: one stalled
# write freezes every other ChatGate frame. A single worker keeps registry
# mutations ordered; UPLOAD_STATE_LOCK lets loop-side readers wait safely.
_UPLOAD_SYNC_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="aivax-upload-sync"
)
_SYNC_OFFLOAD_BYTES = 256 * 1024


def is_eve_socket(socket_path: str) -> bool:
    """--socket 경로가 Suricata eve(Lua JSON line) 모드인지 판별."""
    if socket_path in _EVE_SOCKET_ALIASES or socket_path == DEFAULT_EVE_SOCKET:
        return True
    try:
        rp = os.path.realpath(socket_path)
        if rp == os.path.realpath(DEFAULT_EVE_SOCKET):
            return True
        return any(rp == os.path.realpath(a) for a in _EVE_SOCKET_ALIASES)
    except OSError:
        return socket_path == DEFAULT_EVE_SOCKET


def read_keywords(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.split(","):
        kw = part.strip().lower()
        if kw:
            out.append(kw)
    return out


def resolve_saved_policy_attachments(value: Any) -> list[Dict[str, Any]]:
    if not isinstance(value, list):
        return [{"id": "", "size": 0, "name": "", "mime_type": ""}]

    out: list[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        upload_id = str(item.get("id") or item.get("upload_id") or item.get("file_id") or "").strip()
        if any(k in item for k in ("name", "file_name", "filename")):
            fallback_name = str(item.get("name") or item.get("file_name") or item.get("filename") or "")
        else:
            fallback_name = str(item.get("name") or item.get("file_name") or item.get("filename") or "")
            if not fallback_name and upload_id:
                fallback_name = os.path.basename(upload_id)
        if any(k in item for k in ("mime_type", "mimeType")):
            fallback_mime = str(item.get("mime_type") or item.get("mimeType") or "")
        else:
            fallback_mime = str(item.get("mime_type") or item.get("mimeType") or "")
        saved = get_saved_attachment(
            upload_id,
            fallback_name=fallback_name,
            fallback_mime=fallback_mime,
        )
        if (
            not saved
            and upload_id
            and not os.path.isabs(upload_id)
        ):
            saved = wait_for_saved_attachment(
                upload_id,
                timeout_ms=int(os.environ.get("AIVAX_ATTACHMENT_WAIT_MS", "300")),
                fallback_name=fallback_name,
                fallback_mime=fallback_mime,
            )
        if saved:
            row = dict(saved)
            if any(k in item for k in ("name", "file_name", "filename")):
                row["name"] = fallback_name
            elif fallback_name:
                row["name"] = fallback_name
            if any(k in item for k in ("mime_type", "mimeType")):
                row["mime_type"] = fallback_mime
            elif fallback_mime:
                row["mime_type"] = fallback_mime
            out.append(row)
            continue
        try:
            size = int(item.get("size") or 0)
        except Exception:
            size = 0
        out.append(
            {
                "id": upload_id,
                "size": size,
                "name": fallback_name,
                "mime_type": fallback_mime,
            }
        )
    if not out:
        return [{"id": "", "size": 0, "name": "", "mime_type": ""}]
    return out


def _strip_attachment_id_extension(path_str: str) -> str:
    """경로형 attachment id에서 basename 확장자 한 번 제거 (예: .../file_xxx.pdf → .../file_xxx)."""
    p = str(path_str or "").strip()
    if not p:
        return p
    root, ext = os.path.splitext(p)
    return root if ext else p


def _policy_attachments_strip_ids_for_chatgpt(value: Any) -> Any:
    """ChatGPT 정책 요청 로그/API 본문에서 id 확장자 제거 (저장 조회 이후에만 적용)."""
    if not isinstance(value, list):
        return value
    out: list[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["id"] = _strip_attachment_id_extension(str(row.get("id") or ""))
        out.append(row)
    return out if out else value


def b64decode_payload(raw_b64: str) -> bytes:
    if not raw_b64:
        return b""
    # SSLproxy uses standard base64; be permissive for missing padding.
    pad = (-len(raw_b64)) % 4
    if pad:
        raw_b64 += "=" * pad
    return base64.b64decode(raw_b64, validate=False)


def safe_utf8(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def maybe_gzip_decompress(payload: bytes) -> tuple[bytes, bool]:
    if len(payload) < 2 or payload[:2] != b"\x1f\x8b":
        return payload, False
    try:
        # zlib 직접 경로: gzip.decompress(GzipFile/BufferedReader) 대비 훨씬 빠르고
        # C 구간에서 GIL 을 해제한다. wbits=47 → gzip/zlib 헤더 자동 감지, 출력 동일.
        return zlib.decompress(payload, 47), True
    except Exception:
        pass
    try:
        return gzip.decompress(payload), True
    except Exception:
        pass
    # Tees often lose the gzip trailer (CRC/ISIZE) while the SSE inside is complete.
    dec = zlib.decompressobj(47)
    parts: list[bytes] = []
    for i in range(0, len(payload), 16384):
        try:
            parts.append(dec.decompress(payload[i : i + 16384]))
        except Exception:
            break
    partial = b"".join(parts)
    if partial:
        return partial, True
    return payload, False


# Claude bootstrap/app_start frames often omit content size in the zstd header.
_MAX_ZSTD_OUTPUT = 64 * 1024 * 1024
# A decode error this close to the end is a missing Brotli trailer or a few
# junk bytes. A longer unconsumed tail means the body is not Brotli. HWP
# uploads were rewritten because a prefix decoded and the rest was dropped.
_BROTLI_TRUNCATED_TAIL_MAX = 64


def _zstd_decompress(payload: bytes) -> bytes:
    import zstandard as zstd

    dctx = zstd.ZstdDecompressor()
    return dctx.decompress(payload, max_output_size=_MAX_ZSTD_OUTPUT)


def maybe_brotli_decompress(payload: bytes) -> tuple[bytes, bool]:
    """Decode Brotli, retaining output when a tee omitted the stream trailer.

    Partial output is kept when the stream finishes, when every received byte
    is a valid prefix, or when the failure sits in a short tail. A failure
    that leaves a long tail is not Brotli, so the original bytes are returned.
    """
    try:
        import brotli
    except Exception:
        return payload, False
    try:
        return brotli.decompress(payload), True
    except Exception:
        pass
    decoder = brotli.Decompressor()
    parts: list[bytes] = []
    total = 0
    accepted = 0
    try:
        for index, value in enumerate(payload):
            try:
                finished = decoder.is_finished()
            except Exception:
                finished = False
            if finished:
                break
            chunk = decoder.process(bytes((value,)))
            accepted = index + 1
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_ZSTD_OUTPUT:
                return payload, False
            parts.append(chunk)
    except Exception:
        leftover = len(payload) - accepted
        if not parts or leftover > _BROTLI_TRUNCATED_TAIL_MAX:
            return payload, False
    if not parts:
        return payload, False
    return b"".join(parts), True


def maybe_content_decompress(payload: bytes, content_encoding: str = "") -> tuple[bytes, bool]:
    """Decompress HTTP body by Content-Encoding and/or magic (gzip/br/zstd).

    Suricata HTTP/2 FileContainer does not decompress zstd; Claude responses often use it.
    """
    if not payload:
        return payload, False
    ce = (content_encoding or "").lower()
    # Magic-first for common cases (header may be missing after H2 pseudo-header parse).
    if len(payload) >= 4 and payload[:4] == b"\x28\xb5\x2f\xfd":
        try:
            return _zstd_decompress(payload), True
        except Exception:
            logging.debug("zstd decompress failed", exc_info=True)
            return payload, False
    if len(payload) >= 2 and payload[:2] == b"\x1f\x8b":
        return maybe_gzip_decompress(payload)
    if "zstd" in ce:
        try:
            return _zstd_decompress(payload), True
        except Exception:
            logging.debug("zstd decompress (ce) failed", exc_info=True)
            return payload, False
    if "br" in ce or "brotli" in ce:
        # Suricata H2 often delivers already-decompressed bodies while CE still says br.
        # Re-brotli on plaintext JSON/HTML raises and is unrelated to extract failure.
        head = payload.lstrip()[:1]
        if head in (b"{", b"[", b"<") or payload.lstrip()[:5].lower() == b"data:":
            return payload, False
        out, ok = maybe_brotli_decompress(payload)
        if not ok:
            logging.debug("brotli decompress failed")
        return out, ok
    if "gzip" in ce:
        return maybe_gzip_decompress(payload)
    if "deflate" in ce:
        try:
            import zlib

            return zlib.decompress(payload), True
        except Exception:
            try:
                import zlib

                return zlib.decompress(payload, -zlib.MAX_WBITS), True
            except Exception:
                logging.debug("deflate decompress failed", exc_info=True)
                return payload, False
    # Inline Envoy may tee still-compressed bodies when Content-Encoding is
    # missing (H2 trailer / stripped header). ChatGPT upload init responses
    # are often brotli (~\x1b…) — try br/gzip before giving up.
    head = payload.lstrip()[:1]
    if head not in (b"{", b"[", b"<") and payload.lstrip()[:5].lower() != b"data:":
        out, ok = maybe_brotli_decompress(payload)
        if ok:
            return out, True
        try:
            out, ok = maybe_gzip_decompress(payload)
            if ok:
                return out, True
        except Exception:
            logging.debug("gzip heuristic decompress failed", exc_info=True)
    return payload, False


_USER_CONTENT_KEYS = (
    "messages",
    "partial_query",
    "prompt",
    "input",
    "input_message",
    "query",
    "text",
    "contents",
    "parts",
)


_FAILED_EXTRACT_DUMP_LIMIT = 200


def dump_failed_extract_body(request_id: str, service_id: str, payload: bytes) -> str:
    """Persist a response body no extractor could read. Bounded on disk."""
    if not payload:
        return ""
    try:
        dump_dir = os.environ.get(
            "AIVAX_FAILED_EXTRACT_DIR", "/home1/aivax/ai_engine/var/failed_extract"
        )
        os.makedirs(dump_dir, exist_ok=True)
        if len(os.listdir(dump_dir)) >= _FAILED_EXTRACT_DUMP_LIMIT:
            return ""
        sid = "".join(c for c in str(service_id or "unknown") if c.isalnum() or c in "-_")
        name = f"{str(request_id or 'unknown')[:36]}_{sid[:16]}_{len(payload)}.bin"
        dump_path = os.path.join(dump_dir, name)
        with open(dump_path, "wb") as fh:
            fh.write(payload[: 4 * 1024 * 1024])
        return dump_path
    except Exception:
        logging.debug("failed extract dump failed", exc_info=True)
        return ""


def is_control_only_body(payload: bytes) -> bool:
    """True for a JSON request that carries no user-content container.

    Chat services POST prepare/warm-up bodies to the same conversation path as
    a real turn; they hold only client state and must not count as a failure.
    """
    if not payload:
        return False
    try:
        obj = json.loads(safe_utf8(payload))
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    return not any(key in obj for key in _USER_CONTENT_KEYS)


def extract_text_for_policy(req: Dict[str, Any], payload: bytes) -> str:
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    text = safe_utf8(payload)

    if unit_type == "json_body" or "application/json" in content_type:
        try:
            obj = json.loads(text)
            if not isinstance(obj, dict):
                return ""
            for key in ("prompt", "input", "query", "text"):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            messages = obj.get("messages")
            if isinstance(messages, list):
                chunks: list[str] = []
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    role = str(msg.get("role") or msg.get("sender") or "").lower()
                    if role and role not in ("user", "human"):
                        continue
                    content = msg.get("content")
                    if isinstance(content, str) and content.strip():
                        chunks.append(content.strip())
                    elif isinstance(content, dict):
                        parts = content.get("parts")
                        if isinstance(parts, list):
                            chunks.extend(
                                part.strip()
                                for part in parts
                                if isinstance(part, str) and part.strip()
                            )
                if chunks:
                    return "\n".join(chunks)
            # Protocol metadata (UUID, telemetry fields, tokens) must never
            # become policy input merely because JSON parsing succeeded.
            return ""
        except Exception:
            return ""

    # For SSE events SSLproxy sends event payload text per event.
    if unit_type == "sse_event" or "text/event-stream" in content_type:
        return text

    return text


def _is_conversation_path(adapter_service_id: str, path: str) -> bool:
    """Deny-by-default allowlist for AI conversation request/response paths."""
    sid = (adapter_service_id or "").lower()
    path_lower = (path or "").split("?", 1)[0].lower()
    if sid in ("gpt", "chatgpt"):
        prefixes = (
            "/backend-api/f/conversation",
            "/backend-anon/f/conversation",
            "/backend-api/anon/conversation",
            "/backend-anon/conversation",
            "/unauth-mweb/conversation/updates",
            "/backend-api/codex/responses",
            "/v1/responses",
        )
        for prefix in prefixes:
            idx = path_lower.find(prefix)
            if idx < 0:
                continue
            after = path_lower[idx + len(prefix) :]
            return not after or after == "/resume"
        return False
    if sid == "gemini":
        # Gemini 대화는 StreamGenerate 와 geminiweb-pa /v1/processSession 이다.
        # 로그인하지 않은 프라이빗 브라우저도 processSession 으로 프롬프트를 보낸다.
        # Google AI Mode 는 www.google.com /async/folif (레거시 folwr, 껍데기 /search?udm=50).
        if "streamgenerate" in path_lower or "/v1/processsession" in path_lower:
            return True
        try:
            from adapters.gemini_engine import is_google_ai_mode_path

            return bool(is_google_ai_mode_path(path))
        except Exception:
            return False
    if sid == "claude":
        # Exact /completion only — /completion_status must not look like a chat turn.
        path_base = path_lower.rstrip("/")
        if path_base.endswith("/completion") and "chat_conversations" in path_base:
            return True
        # The web app moved turns off /completion onto ConversationService.
        # PerformAction carries the prompt. StreamTimeline carries the answer.
        if "/claudeai-rpc/" in path_base and "conversationservice/" in path_base:
            method = path_base.rsplit("/", 1)[-1]
            return method in ("performaction", "streamtimeline")
        return False
    if sid == "perplexity":
        return "perplexity_ask" in path_lower
    if sid == "grok":
        try:
            from adapters.grok_engine import is_grok_conversation_path

            return bool(is_grok_conversation_path(path))
        except Exception:
            return "/ws/mgw" in path_lower
    if sid == "timely":
        return (
            "/timely/api/stream" in path_lower
            or "/api-ai/v3/llm-completion" in path_lower
        )
    if sid == "genspark":
        return path_lower.rstrip("/") == "/api/agent/ask_proxy"
    if sid == "github_copilot":
        try:
            from adapters.github_copilot_engine import is_github_copilot_account_path

            if is_github_copilot_account_path(path):
                return False
        except Exception:
            pass
        return True
    if sid in ("notion", "wrks", "chat_version"):
        # Existing adapters already have service-specific extraction/final gates.
        return True
    return False


def build_allow_response(request_id: str, reason: str = "allow") -> Dict[str, Any]:
    return {
        "request_id": request_id,
        "action": "ALLOW",
        "reason": reason,
    }


def build_block_response(request_id: str, reason: str, block_message: str = "") -> Dict[str, Any]:
    msg = block_message or "blocked by python ai policy engine"
    body = json.dumps({"error": msg}, ensure_ascii=False, separators=(",", ":"))
    return {
        "request_id": request_id,
        "action": "BLOCK",
        "reason": reason,
        "block_message": msg,
        "block_response": {
            "status": 403,
            "content_type": "application/json",
            "body": body,
        },
    }


def build_payload_response(
    request_id: str,
    action: str,
    reason: str,
    payload: bytes,
) -> Dict[str, Any]:
    # Envoy aivax_core historically reads payload.raw_b64 / payload_utf8.
    # Keep top-level payload_b64 for older clients and harnesses.
    b64 = base64.b64encode(payload).decode("ascii")
    return {
        "request_id": request_id,
        "action": action,
        "reason": reason,
        "payload_b64": b64,
        "payload": {"raw_b64": b64},
    }


def build_mask_response(request_id: str, reason: str, payload: bytes) -> Dict[str, Any]:
    return build_payload_response(request_id, "MASK", reason, payload)


def build_rewrite_response(request_id: str, reason: str, payload: bytes) -> Dict[str, Any]:
    return build_payload_response(request_id, "REWRITE", reason, payload)


def _rewrite_in_json_value(value: Any, src: str, dst: str) -> tuple[Any, int]:
    if isinstance(value, str):
        replaced = value.replace(src, dst)
        if replaced == value:
            return value, 0
        return replaced, value.count(src)
    if isinstance(value, list):
        out: list[Any] = []
        total = 0
        for item in value:
            nval, cnt = _rewrite_in_json_value(item, src, dst)
            out.append(nval)
            total += cnt
        return out, total
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        total = 0
        for k, v in value.items():
            nval, cnt = _rewrite_in_json_value(v, src, dst)
            out[k] = nval
            total += cnt
        return out, total
    return value, 0


def rewrite_payload(req: Dict[str, Any], payload: bytes, src: str, dst: str) -> tuple[bytes, int]:
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if not src:
        return payload, 0

    if unit_type == "json_body" or "application/json" in content_type:
        text = safe_utf8(payload)
        try:
            obj = json.loads(text)
            new_obj, count = _rewrite_in_json_value(obj, src, dst)
            if count == 0:
                return payload, 0
            encoded = json.dumps(new_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return encoded, count
        except Exception:
            replaced = text.replace(src, dst)
            if replaced == text:
                return payload, 0
            return replaced.encode("utf-8"), text.count(src)

    text = safe_utf8(payload)
    replaced = text.replace(src, dst)
    if replaced == text:
        return payload, 0
    return replaced.encode("utf-8"), text.count(src)


def _first_str(obj: Dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def normalize_policy_decision(value: str) -> str:
    v = (value or "").strip().upper()
    if v in ("PASS", "ALLOW", "OK", "ACCEPT"):
        return "PASS"
    if v in ("MASKING", "MASK", "REDACT", "REWRITE"):
        return "MASKING"
    if v in ("BLOCK", "DENY", "REJECT"):
        return "BLOCK"
    return ""


def parse_policy_api_response(obj: Any) -> Optional[tuple[str, str, str]]:
    if not isinstance(obj, dict):
        return None

    masked_keys = (
        "replacement_text",
        "replacement",
        "mask_text",
        "masked_text",
        "masked_contents",
        "text",
        "prompt",
    )
    block_message_keys = (
        "block_message",
        "blocked_message",
        "message",
    )

    decision = ""
    masked_contents = ""
    block_message = ""

    def _parse_node(node: Dict[str, Any]) -> tuple[str, str, str]:
        return (
            normalize_policy_decision(
                _first_str(node, ("action", "code", "decision", "result", "status"))
            ),
            _first_str(node, masked_keys),
            _first_str(node, block_message_keys),
        )

    data = obj.get("data") if isinstance(obj.get("data"), dict) else None
    out_result = obj.get("out_result") if isinstance(obj.get("out_result"), dict) else None
    data_out = (
        data.get("out_result")
        if isinstance(data, dict) and isinstance(data.get("out_result"), dict)
        else None
    )
    # Pipeline wraps the decision as out_result.final_decision.
    # action "" / action_code 0 is allow (no hit). action accept is detect-and-allow.
    candidates = (
        obj.get("final_decision"),
        out_result.get("final_decision") if out_result else None,
        data.get("final_decision") if isinstance(data, dict) else None,
        data_out.get("final_decision") if data_out else None,
        out_result,
        data_out,
        data,
        obj,
    )

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        d, m, b = _parse_node(candidate)
        if not d and "action_code" in candidate:
            try:
                code = int(candidate.get("action_code") or 0)
            except (TypeError, ValueError):
                code = -1
            if code == 0:
                d = "PASS"
            elif code == 1:
                d = "BLOCK"
            elif code == 2:
                d = "MASKING"
        if d and not decision:
            decision = d
        if m and not masked_contents:
            masked_contents = m
        if b and not block_message:
            block_message = b

    if not decision:
        return None
    return decision, masked_contents, block_message


def extract_policy_cso(obj: Any) -> tuple[str, str]:
    """(status, message) when the policy body carries a cso object. Null is off."""
    if not isinstance(obj, dict):
        return "", ""
    nodes: list[Any] = [obj]
    data = obj.get("data")
    out_result = obj.get("out_result")
    if isinstance(data, dict):
        nodes.append(data)
    if isinstance(out_result, dict):
        nodes.append(out_result)
    for node in nodes:
        cso = node.get("cso") if isinstance(node, dict) else None
        if not isinstance(cso, dict):
            continue
        status = str(cso.get("status") or "").strip().lower()
        message = str(cso.get("message") or "").strip()
        if status or message:
            return status, message
    return "", ""


def extract_policy_detect_filter(obj: Any) -> str:
    """Blocking/masking filter id from a policy API JSON body."""
    if not isinstance(obj, dict):
        return ""
    out_result = obj.get("out_result") if isinstance(obj.get("out_result"), dict) else None
    data = obj.get("data") if isinstance(obj.get("data"), dict) else None
    for node in (
        obj.get("final_decision"),
        out_result.get("final_decision") if out_result else None,
        data.get("final_decision") if isinstance(data, dict) else None,
        out_result,
        obj,
        data,
    ):
        if not isinstance(node, dict):
            continue
        for key in ("detect_filter", "filter", "filter_name"):
            value = str(node.get(key) or "").strip()
            if value:
                return value
    filter_result = obj.get("filter_result")
    if not isinstance(filter_result, dict):
        return ""
    first_mask = ""
    for name, output in filter_result.items():
        if not isinstance(output, dict):
            continue
        action = str(output.get("action") or "").strip().lower()
        try:
            code = int(output.get("action_code") or 0)
        except (TypeError, ValueError):
            code = 0
        if action == "block" or code == 1:
            return str(name)
        if not first_mask and (action == "masking" or code == 2):
            first_mask = str(name)
    return first_mask


def _compose_block_rewrite_text(masked_contents: str, block_message: str) -> str:
    base = (masked_contents or "").strip()
    notice = (block_message or "").strip()
    if not notice:
        return base
    if not base:
        return notice
    return f"{base}\n\n{notice}"


def _insert_before_detect_type(notice: str, chunk: str) -> str:
    """Insert chunk after the lock/hint block and before 탐지 유형 details."""
    base = str(notice or "").rstrip()
    extra = str(chunk or "").strip()
    if not extra:
        return base
    if extra in base:
        return base
    insert = f"\n\n{extra}"
    label = re.escape(_detect_type_label())
    phrase_detect = _detect_line_search().lstrip("^")
    detect = re.search(
        rf"\n+(?={phrase_detect}|📛\s*{label}|❌\s*{label})",
        base,
        re.M,
    )
    if detect:
        return base[: detect.start()] + insert + base[detect.start() :]
    hint_at = base.find(_USER_CONSENT_HINT) if _USER_CONSENT_HINT else -1
    if hint_at >= 0:
        end = hint_at + len(_USER_CONSENT_HINT)
        return base[:end] + insert + base[end:]
    headline = re.search(r"차단되었습니다[^\n]*", base)
    if headline:
        return base[: headline.end()] + insert + base[headline.end() :]
    if not base:
        return extra
    return f"{base}{insert}"


def _slice_detect_type_section(notice: str) -> str:
    """Keep from 탐지 유형 through the end; drop title / lock headline."""
    text = str(notice or "").strip()
    phrase_detect = _detect_line_search().lstrip("^")
    match = re.search(
        rf"(?:{phrase_detect}|(?:📛|❌)\s*{re.escape(_detect_type_label())})",
        text,
        re.M,
    )
    if match:
        return text[match.start() :].rstrip()
    return text


# 화면 문구·지시문·마스킹 토큰은 core/block_copy.py 가 단일 출처다.
_LEADING_MARK_RE = re.compile(r"^(?:🔒|📛|❌|⚠️|🚫|🛡️|📌|⚡|🔎)\s*")


def _first_line_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(0).strip() if match else ""


_SLOT_START_RE = re.compile(r"^(?:🔒|🚫|📛|❌|⚠️|🛡️|📌|⚡)")


def _footer_boundary(line: str) -> bool:
    """푸터 리드·관리자 문장. 칸은 여기서 끊고, 이 줄은 카드에 넣지 않는다."""
    text = line.strip()
    if not text:
        return False
    if re.search(r"세부 (?:사항|지침 사항)은 관리자에게", text):
        return True
    found = _find_footer(text)
    fallback = _find_footer("\n")
    return bool(found) and found != fallback and text in found


def _slot_boundary(line: str) -> bool:
    text = line.strip()
    if not text:
        return False
    if text.startswith("[AIVAX]") or text.startswith("###"):
        return True
    if _line_matches_phrase_slot(text) or _SLOT_START_RE.match(text):
        return True
    return _footer_boundary(text)


def _take_marked_section(text: str, pattern: str) -> tuple[str, str]:
    """표식 줄부터 다음 칸 앞까지. 반환은 (본문, 바로 다음 경계 줄)."""
    match = re.search(pattern, text, re.M)
    if not match:
        return "", ""
    line_start = text.rfind("\n", 0, match.start()) + 1
    lines = text[line_start:].splitlines()
    if not lines:
        return "", ""
    kept = [lines[0]]
    next_boundary = ""
    for line in lines[1:]:
        if _slot_boundary(line):
            next_boundary = line.strip()
            break
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    first = _LEADING_MARK_RE.sub("", kept[0]).strip()
    tail = "\n".join(kept[1:]).rstrip("\n")
    body = f"{first}\n{tail}".rstrip() if tail else first
    return body, next_boundary


def _leading_prose(text: str) -> str:
    """제목 다음, 첫 칸 앞에 둔 문장. 제목에 넣은 줄바꿈과 ``**굵게**`` 다."""
    kept: list[str] = []
    started = False
    for line in text.splitlines():
        stripped = line.strip()
        if not started:
            if stripped.startswith("[AIVAX]") or stripped.startswith("###"):
                started = True
            continue
        if _slot_boundary(stripped):
            break
        kept.append(line)
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)


def _other_note_bodies(text: str, label: str) -> str:
    """탐지 유형이 아닌 ❌·📛 칸. ``탐지된 내용은`` 같은 문장이다."""
    bodies: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not re.match(r"^(?:📛|❌)", stripped):
            continue
        if label and re.search(label, stripped):
            continue
        body, _next = _take_marked_section("\n".join(lines[index:]), r"^(?:📛|❌)")
        if body:
            bodies.append(body)
    return "\n\n".join(bodies)


def _headline_quote(headline: str) -> str:
    """여러 줄과 **굵게**를 한 인용으로 붙인다."""
    lines = str(headline or "").splitlines()
    if not lines:
        return ""
    mark = _headline_quote_mark()
    quoted = [f"> {mark} {lines[0]}" if mark else f"> {lines[0]}"]
    for line in lines[1:]:
        quoted.append(">" if not line.strip() else f"> {line}")
    return "\n".join(quoted)


def _clean_detect_type(raw: str) -> str:
    """'이메일-시연테스트' 입니다. → 이메일-시연테스트. IP - v4 는 유지."""
    value = str(raw or "").strip()
    value = re.sub(r"\s*입니다\.?\s*$", "", value).strip()
    value = value.strip("'\"“”")
    return value.strip()


def _parse_block_message_parts(block_message: str) -> Dict[str, str]:
    text = str(block_message or "").strip()
    title = _resolve_block_title(text)
    label = re.escape(_detect_type_label())
    headline, _lock_next = _take_marked_section(
        text, _lock_line_search() + r"|^(?:🔒|🚫)"
    )
    preamble = _leading_prose(text)
    detect_body, _detect_next = _take_marked_section(
        text, _detect_line_search() + rf"|^(?:📛|❌).*{label}"
    )
    warn_body, _warn_next = _take_marked_section(
        text, _warn_line_search() + r"|^⚠️"
    )
    footer = _find_footer(text)
    shown_footer = _message_footer(text)
    detect_line, _, detect_extra = detect_body.partition("\n")
    detect_extra = detect_extra.strip()
    notes = _other_note_bodies(text, _detect_type_label())
    detected = _detected_content_span(text)
    if detected and detected not in notes:
        notes = f"{notes}\n\n{detected}".strip() if notes else detected
    detect_type = _clean_detect_type(_detect_type_in(text))
    if not detect_type and detect_line:
        type_match = re.search(rf"{label}[:\s은]*['\"]?(.+)", detect_line)
        detect_type = _clean_detect_type(
            type_match.group(1) if type_match else detect_line
        )
    warn_text = ""
    if warn_body:
        warn_line, _, warn_extra = warn_body.partition("\n")
        warn_split = re.search(r"(.+있습니다\.)\s+(각별히.+)", warn_line)
        warn_text = (
            f"{warn_split.group(1)}\n\n{warn_split.group(2)}"
            if warn_split
            else warn_line
        )
        if warn_extra.strip():
            warn_text = f"{warn_text}\n{warn_extra.rstrip()}"
    if not headline and not detect_type and not warn_text:
        leftover = text
        footer_text = _find_footer(text)
        leftover = re.sub(r"(?m)^\#{0,3}\s*\[AIVAX\][^\n]*", "", leftover)
        for footer_line in footer_text.splitlines():
            if footer_line:
                leftover = leftover.replace(footer_line, "")
        leftover = leftover.strip()
        if leftover:
            headline = leftover
    return {
        "title": title,
        "footer": footer,
        "headline": headline,
        "preamble": preamble,
        "detect_type": detect_type,
        "detect_extra": detect_extra,
        "notes": notes,
        "shown_footer": shown_footer,
        "warn": warn_text,
    }


_DETECT_TYPE_HINTS = (
    (re.compile(r"ipv6|ip\s*-?\s*v6", re.I), "IPv6 주소가 포함되어 있습니다."),
    (re.compile(r"ipv4|ip\s*-?\s*v4|\bip\b", re.I), "IPv4 주소가 포함되어 있습니다."),
    (re.compile(r"email|메일", re.I), "이메일 주소가 포함되어 있습니다."),
    (re.compile(r"phone|전화|휴대", re.I), "전화번호가 포함되어 있습니다."),
    (re.compile(r"rrn|주민", re.I), "주민등록번호로 보이는 값이 포함되어 있습니다."),
    (re.compile(r"card|카드", re.I), "카드번호로 보이는 값이 포함되어 있습니다."),
    (re.compile(r"api|secret|key|토큰|token", re.I), "API 키 또는 비밀값이 포함되어 있습니다."),
    (re.compile(r"pass|비밀번호", re.I), "비밀번호로 보이는 값이 포함되어 있습니다."),
    (re.compile(r"account|계좌", re.I), "계좌번호로 보이는 값이 포함되어 있습니다."),
    (re.compile(r"address|주소", re.I), "주소 정보가 포함되어 있습니다."),
)


def _explain_detect_type(detect_type: str) -> str:
    text = str(detect_type or "").strip()
    if not text:
        return "민감정보가 포함되어 있습니다."
    hints: list[str] = []
    for part in re.split(r"[,/;]+", text):
        part = part.strip()
        if not part:
            continue
        found = next((hint for rx, hint in _DETECT_TYPE_HINTS if rx.search(part)), "")
        if found and found not in hints:
            hints.append(found)
    if hints:
        return " ".join(hints)
    return f"'{text}' 유형의 민감정보가 포함되어 있습니다."


def _policy_analysis_ms(*sources: Any) -> Optional[int]:
    for src in sources:
        if not isinstance(src, dict):
            continue
        raw = src.get("_policy_analysis_ms")
        if raw is None or raw == "":
            continue
        try:
            return max(1, int(round(float(raw))))
        except (TypeError, ValueError):
            continue
    return None


_FILTER_LABELS = {
    "secret_filter": "프롬프트",
    "regex_filter": "프롬프트",
    "regex": "프롬프트",
    "input_filter": "프롬프트",
    "prompt": "프롬프트",
    "프롬프트": "프롬프트",
    "slm_filter": "SLM",
    "slm": "SLM",
    "file_block_filter": "멀티모달",
    "file_block": "멀티모달",
    "file block": "멀티모달",
    "multimodal": "멀티모달",
    "멀티모달": "멀티모달",
}


def _format_detect_filter(raw: str) -> str:
    key = str(raw or "").strip()
    if not key:
        return ""
    mapped = _FILTER_LABELS.get(key.lower())
    if mapped:
        return mapped
    if key.upper() == "SLM":
        return "SLM"
    return ""


def _infer_detect_filter(block_message: str) -> str:
    text = str(block_message or "")
    low = text.lower()
    if re.search(r"\bslm\b", low) or "SLM" in text:
        return "SLM"
    if "file_block" in low or "멀티모달" in text or "파일 차단" in text:
        return "멀티모달"
    if "secret_filter" in low or "정규식" in text or "aivax 정책" in low:
        return "프롬프트"
    return ""


def _policy_detect_filter(*sources: Any) -> str:
    for src in sources:
        if not isinstance(src, dict):
            continue
        raw = src.get("_policy_detect_filter")
        if raw:
            return str(raw).strip()
    return ""


def _resolve_detect_filter_label(detect_filter: str, block_message: str) -> str:
    return _format_detect_filter(detect_filter) or _infer_detect_filter(block_message)


def _cso_blocked_visible_response(block_message: str, cso_message: str) -> str:
    """CSO grade block: title, file notice, guidance. No detect reason, no consent."""
    if _guideline_notice_enabled():
        notice = _format_guideline_cso_notice(block_message)
        if notice:
            return notice
    raw = str(block_message or "").strip()
    lines = raw.splitlines()
    if lines and lines[0].strip().startswith("[AIVAX]"):
        title = lines[0].strip()
        rest = lines[1:]
    else:
        title = _CSO_BLOCK_TITLE
        rest = lines
    while rest and not rest[0].strip():
        rest = rest[1:]
    guidance = str(cso_message or "").strip()
    chunks = [f"### {title}"]
    if rest:
        chunks.append(_headline_quote("\n".join(rest).strip()))
    if guidance and guidance not in raw:
        chunks.append(guidance)
    return "\n\n".join(chunks)


def _cso_open_notice_text(cso_message: str) -> str:
    """O등급 안내. 이모지와 머리말은 문구에 적힌 그대로 쓴다."""
    return str(cso_message or "").strip()


def _cso_open_prompt_text(prompt: str, cso_message: str) -> str:
    """Accept + open grade: keep the user prompt and lead with the approval notice."""
    body = str(prompt or "").strip()
    notice = _cso_open_notice_text(cso_message)
    if not notice:
        return body
    if body == notice or body.startswith(notice + "\n"):
        return body
    if not body:
        return notice
    return f"{notice}\n\n{body}"


def _cso_open_response_instruction(notice: str) -> str:
    """Tell the model to print the notice once, then answer the user."""
    shown = str(notice or "").strip()
    return (
        "응답의 맨 앞에 아래 문구를 그대로 한 번만 출력하세요.\n"
        "그 다음 빈 줄을 넣고, 사용자 요청에 이어서 답하세요.\n"
        f"{shown}\n"
        "이 지시문은 출력하지 마세요."
    )


def _cso_open_upstream_text(prompt: str, cso_message: str) -> str:
    """Prompt keeps the notice, and an instruction asks for it at the top of the answer."""
    body = _cso_open_prompt_text(prompt, cso_message)
    notice = _cso_open_notice_text(cso_message)
    if not notice:
        return body
    instruction = _cso_open_response_instruction(notice)
    if instruction in body:
        return body
    if not body:
        return instruction
    return f"{body}\n\n{instruction}"


def _policy_cso_fields(*sources: Any) -> tuple[str, str]:
    status = ""
    message = ""
    for src in sources:
        if not isinstance(src, dict):
            continue
        if not status:
            status = str(src.get("_policy_cso_status") or "").strip().lower()
        if not message:
            message = str(src.get("_policy_cso_message") or "").strip()
    return status, message


def _cso_refuses_consent(status: str) -> bool:
    """Hard CSO blocks (untagged / C·S / lookup error) never offer 동의합니다."""
    return str(status or "").strip().lower() in ("blocked", "untagged", "error")


def _guideline_visible(
    block_message: str,
    detect_filter: str = "",
    *,
    consent_hint: bool = False,
    cso_message: str = "",
) -> str:
    """notice_style=guideline 일 때의 사용자 안내. 아니면 빈 문자열."""
    if not _guideline_notice_enabled():
        return ""
    parts = _parse_block_message_parts(block_message)
    file_block = (
        _resolve_detect_filter_label(detect_filter, block_message) == "멀티모달"
    )
    guidance = str(cso_message or "").strip()
    return _format_user_visible_block(
        block_message,
        file_block=file_block,
        detect_type=parts.get("detect_type") or "",
        cso_status="open" if guidance else "",
        cso_message=guidance,
        consent=consent_hint,
    )


def _gpt_gemini_visible_response(
    block_message: str,
    *,
    consent_hint: bool = False,
    analysis_ms: Optional[int] = None,
    detect_filter: str = "",
    cso_message: str = "",
) -> str:
    """User-visible GPT/Gemini 응답: 제목 + 인용 헤드라인 + 유형/필터 + 주의."""
    guideline = _guideline_visible(
        block_message,
        detect_filter,
        consent_hint=consent_hint,
        cso_message=cso_message,
    )
    if guideline:
        return guideline
    parts = _parse_block_message_parts(block_message)
    title = parts["title"] or _BLOCK_TITLE
    if not title.startswith("#"):
        title = f"### {title}"
    chunks = [title]
    headline = parts["headline"] or ""
    preamble = parts.get("preamble") or ""
    if preamble and not headline.startswith(preamble):
        chunks.append(_headline_quote(preamble))
    if parts["headline"]:
        chunks.append(_headline_quote(parts["headline"]))
    chunks.append(_DETECT_REASON_LABEL)
    reason_items: list[str] = []
    if parts["detect_type"]:
        reason_items.append(f"* **유형:** {parts['detect_type']}")
        reason_items.append(f"* **설명:** {_explain_detect_type(parts['detect_type'])}")
    filter_label = _resolve_detect_filter_label(detect_filter, block_message)
    if filter_label:
        reason_items.append(f"* **필터:** {filter_label}")
    if analysis_ms is not None:
        reason_items.append(f"* **분석시간:** {max(1, int(analysis_ms))}ms")
    chunks.append("\n".join(reason_items))
    if parts.get("detect_extra"):
        chunks.append(parts["detect_extra"])
    if parts.get("notes"):
        chunks.append(parts["notes"])
    # Open-grade file plus a normal filter block: approval guidance sits
    # under the detect reason and above the consent warning.
    guidance = str(cso_message or "").strip()
    if guidance and guidance not in "\n\n".join(chunks):
        chunks.append(guidance)
    if parts["warn"]:
        chunks.append(parts["warn"])
    if parts.get("shown_footer"):
        chunks.append(parts["shown_footer"])
    body = "\n\n".join(chunks)
    if consent_hint:
        body = f"{body.rstrip()}\n\n{_USER_CONSENT_HINT_MARKDOWN}"
    return body


def _gpt_gemini_model_input(
    masked_contents: str,
    block_message: str,
    *,
    consent_hint: bool = False,
    analysis_ms: Optional[int] = None,
    detect_filter: str = "",
    cso_message: str = "",
) -> str:
    """GPT/Gemini 모델 입력: 입력 프롬프트 → SNIPER 헤더 → 응답 본문."""
    parts = _parse_block_message_parts(block_message)
    masked = str(masked_contents or "").strip() or MASK_TOKEN
    visible = _gpt_gemini_visible_response(
        block_message,
        consent_hint=consent_hint,
        analysis_ms=analysis_ms,
        detect_filter=detect_filter,
        cso_message=cso_message,
    )
    return (
        f"{_INPUT_PROMPT_LABEL}\n"
        f"{masked}\n\n"
        f"{_SNIPER_HEADER}\n\n"
        f"{visible}"
    )


def _gpt_gemini_block_notice(
    masked_contents: str,
    block_message: str,
    *,
    consent_hint: bool = False,
    analysis_ms: Optional[int] = None,
    detect_filter: str = "",
    cso_message: str = "",
) -> str:
    """Rewrite용 모델 입력. 응답 overlay는 _gpt_gemini_visible_response."""
    return _gpt_gemini_model_input(
        masked_contents,
        block_message,
        consent_hint=consent_hint,
        analysis_ms=analysis_ms,
        detect_filter=detect_filter,
        cso_message=cso_message,
    )


def _gemini_block_visible_notice(
    masked_contents: str,
    block_message: str,
    *,
    consent_hint: bool = False,
    analysis_ms: Optional[int] = None,
    detect_filter: str = "",
    cso_message: str = "",
) -> str:
    """Gemini rewrite는 GPT와 동일한 모델 입력을 사용한다."""
    return _gpt_gemini_model_input(
        masked_contents,
        block_message,
        consent_hint=consent_hint,
        analysis_ms=analysis_ms,
        detect_filter=detect_filter,
        cso_message=cso_message,
    )


def _attach_masked_input_prompt(notice: str, masked_contents: str) -> str:
    masked = str(masked_contents or "").strip()
    if not masked:
        return str(notice or "").rstrip()
    return _insert_before_detect_type(notice, f"[입력 프롬프트]\n{masked}")


def _block_visible_response(
    block_message: str,
    *,
    consent_hint: bool = False,
    masked_contents: str = "",
    analysis_ms: Optional[int] = None,
    detect_filter: str = "",
    cso_message: str = "",
) -> str:
    """GPT/Gemini 사용자 응답. masked는 응답에 넣지 않는다."""
    _ = masked_contents
    return _gpt_gemini_visible_response(
        block_message,
        consent_hint=consent_hint,
        analysis_ms=analysis_ms,
        detect_filter=detect_filter,
        cso_message=cso_message,
    )

def _block_model_input_prompt(
    masked_contents: str,
    block_message: str,
    *,
    consent_hint: bool = False,
    analysis_ms: Optional[int] = None,
    detect_filter: str = "",
    cso_message: str = "",
) -> str:
    """GPT/Gemini rewrite: SNIPER 헤더 + 입력 프롬프트 + 응답 본문."""
    return _gpt_gemini_model_input(
        masked_contents,
        block_message,
        consent_hint=consent_hint,
        analysis_ms=analysis_ms,
        detect_filter=detect_filter,
        cso_message=cso_message,
    )


def _block_display_text(
    masked_contents: str,
    block_message: str,
    *,
    consent_hint: bool = False,
    cso_message: str = "",
) -> str:
    """BLOCK 안내문: 원문은 [입력 프롬프트]로, 동의 안내는 탐지 유형 앞에 둔다."""
    guideline = _guideline_visible(
        block_message, consent_hint=consent_hint, cso_message=cso_message
    )
    if guideline:
        return guideline
    notice = (block_message or "").strip()
    guidance = str(cso_message or "").strip()
    if guidance and guidance not in notice:
        notice = f"{notice}\n\n{guidance}" if notice else guidance
    if not notice:
        return (
            (masked_contents or "").strip()
            or "blocked by python ai policy engine"
        )
    if consent_hint:
        notice = append_user_consent_hint(notice)
    return _attach_masked_input_prompt(notice, masked_contents)


# 지시문은 block_copy 가 제목·끝 앵커·마스킹 토큰으로 만든다.
# AIVAX_ / GPT_ / GEMINI_ / CSO_ / PERPLEXITY_BLOCK_MODEL_INSTRUCTION


class PolicyApiError(RuntimeError):
    pass


def _upload_info_has_file(info: Dict[str, Any]) -> bool:
    if info.get("upload_id") or info.get("saved_path") or info.get("upload_filename"):
        return True
    saved = info.get("saved_files")
    return isinstance(saved, list) and any(
        isinstance(row, dict)
        and (row.get("upload_id") or row.get("saved_path") or row.get("filename"))
        for row in saved
    )


def _prepare_upload(
    adapter: Any,
    adapter_req: Dict[str, Any],
    req: Dict[str, Any],
    payload: bytes,
    content_encoding: str,
    gate_is_none: bool,
):
    """Decompress, normalize, and persist under the upload lock.

    Returns (payload, upload_info, normalize_error, upload_result). upload_result
    is None when normalize already decided to return before persistence, matching
    the previous inline order.
    """
    with UPLOAD_STATE_LOCK:
        upload_payload = payload
        try:
            upload_payload, _ = maybe_content_decompress(payload, content_encoding)
        except Exception:
            upload_payload = payload
        upload_info = None
        norm_error: Optional[BaseException] = None
        if adapter is not None and hasattr(adapter, "normalize_upload"):
            try:
                upload_info = adapter.normalize_upload(adapter_req, upload_payload)
            except Exception as exc:
                norm_error = exc
        stop_before_persist = False
        if isinstance(upload_info, dict):
            meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
            if upload_info.get("upload_id"):
                meta["upload_id"] = upload_info["upload_id"]
            if upload_info.get("upload_filename"):
                meta["upload_filename"] = upload_info["upload_filename"]
            _normalize_upload_meta(meta)
            if upload_info.get("handled") and (
                _upload_info_has_file(upload_info) or gate_is_none
            ):
                stop_before_persist = True
        upload_result = None
        if not stop_before_persist:
            upload_result = handle_upload_event(req, upload_payload)
        return upload_payload, upload_info, norm_error, upload_result


def _decode_policy_frame(frame: bytes) -> tuple[Dict[str, Any], bytes]:
    req = json.loads(frame.decode("utf-8", errors="strict"))
    if not isinstance(req, dict):
        raise ValueError("request root is not object")
    payload_obj = req.get("payload")
    raw_b64 = ""
    if isinstance(payload_obj, dict):
        raw_b64 = str(payload_obj.get("raw_b64") or "")
    return req, b64decode_payload(raw_b64)


def _encode_policy_response(resp_obj: Dict[str, Any]) -> bytes:
    return json.dumps(resp_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class PolicyEngine:
    def __init__(
        self,
        keywords: Iterable[str],
        payload_preview_bytes: int,
        log_payload: bool,
        rewrite_from: str,
        rewrite_to: str,
        adapters_dir: str,
        adapters_hot_reload: bool,
        chatgpt_force_request_text: str,
        chatgpt_block_sysinstruct: bool,
        pipeline_server_url: str,
        pipeline_server_port: int,
        pipeline_input_api_path: str,
        pipeline_output_api_path: str,
        policy_api_timeout_ms: int,
        policy_api_max_inflight: int,
        policy_api_pool_limit: int,
        policy_api_acquire_timeout_ms: int,
        policy_api_failure_policy: str,
    ) -> None:
        self.keywords = [k.lower() for k in keywords]
        self.payload_preview_bytes = payload_preview_bytes
        self.log_payload = log_payload
        self.rewrite_from = rewrite_from
        self.rewrite_to = rewrite_to
        self.chatgpt_force_request_text = chatgpt_force_request_text
        self.chatgpt_block_sysinstruct = bool(chatgpt_block_sysinstruct)
        self.pipeline_server_url = (pipeline_server_url or "").strip().rstrip("/")
        if self.pipeline_server_url and "://" not in self.pipeline_server_url:
            self.pipeline_server_url = f"http://{self.pipeline_server_url}"
        self.pipeline_server_port = max(1, int(pipeline_server_port))
        self.pipeline_input_api_path = (pipeline_input_api_path or "").strip()
        self.pipeline_output_api_path = (pipeline_output_api_path or "").strip()
        if self.pipeline_input_api_path and not self.pipeline_input_api_path.startswith("/"):
            self.pipeline_input_api_path = "/" + self.pipeline_input_api_path
        if self.pipeline_output_api_path and not self.pipeline_output_api_path.startswith("/"):
            self.pipeline_output_api_path = "/" + self.pipeline_output_api_path
        self.policy_api_timeout_ms = max(1, int(policy_api_timeout_ms))
        self.policy_api_max_inflight = max(1, int(policy_api_max_inflight))
        self.policy_api_pool_limit = max(1, int(policy_api_pool_limit))
        self.policy_api_acquire_timeout_ms = max(1, int(policy_api_acquire_timeout_ms))
        failure_policy = (policy_api_failure_policy or DEFAULT_POLICY_API_FAILURE_POLICY).strip().lower()
        self.policy_api_failure_policy = (
            failure_policy if failure_policy in ("open", "close") else DEFAULT_POLICY_API_FAILURE_POLICY
        )
        self.policy_api_fail_open = self.policy_api_failure_policy == "open"
        self.adapter_registry = AdapterRegistry(adapters_dir, hot_reload=adapters_hot_reload)
        self._response_override_by_session: Dict[str, str] = {}
        self._response_override_by_request_id: Dict[str, str] = {}
        self._response_override_session_by_request_id: Dict[str, str] = {}
        self._response_override_pending_by_service: Dict[str, list[str]] = {}
        # O등급 안내를 응답 첫 텍스트 앞에 한 번만 붙인다. (text, expires)
        self._cso_response_prefix_by_session: Dict[str, Tuple[str, float]] = {}
        self._cso_response_prefix_by_request: Dict[str, Tuple[str, float]] = {}
        self._cso_response_prefix_pending: Dict[str, list[Tuple[str, float]]] = {}
        # Survives response_override clear so follow-up batchexecute/history
        # responses cannot collect upstream model text after a BLOCK.
        self._block_suppress_by_session: Dict[str, Tuple[float, str]] = {}
        self._block_suppress_last_by_adapter: Dict[str, Tuple[float, str, str]] = {}
        self._block_suppress_session_ttl_sec = 120.0
        self._block_suppress_adapter_ttl_sec = 15.0
        # user_consent pending: key -> {prompt, message_id, expires, client_ip}
        self._user_consent_pending: Dict[str, Dict[str, Any]] = {}
        # Bridges consent request turn -> response output_filter (patch BLOCK doc).
        self._user_consent_output_pending: Dict[str, Dict[str, Any]] = {}
        # Session/request keys that must not apply BLOCK response_override
        # after a consent pass (late SSE still carries the BLOCK request_id).
        self._user_consent_pass_until: Dict[str, float] = {}
        self._user_consent_lock = Lock()
        self._output_log_seen_keys: Dict[str, None] = {}
        self._output_log_seen_lock = Lock()
        self._output_log_seen_limit = 4096
        # processSession answers grow across long-polls. Hold the longest text
        # and publish it only after the body stops growing.
        self._gemini_growing_quiet_sec = 15.0
        self._gemini_growing_outputs: Dict[str, Dict[str, Any]] = {}
        self._gemini_growing_tasks: Dict[str, asyncio.Task] = {}
        self._gemini_growing_posted: Dict[str, int] = {}
        # message_id -> (posted length, first log timestamp). A longer ChatGPT
        # answer revises that document instead of being dropped as a duplicate.
        self._output_posted: Dict[str, tuple] = {}
        # Codex tool steps hold the assistant message. Publish it once the
        # socket stays quiet, unless a later tool-free answer already did.
        self._codex_held_quiet_sec = 20.0
        self._codex_held_tasks: Dict[str, asyncio.Task] = {}
        self._policy_cross_source_seen: Dict[str, float] = {}
        self._policy_cross_source_lock = Lock()
        self._policy_cross_source_ttl_sec = 120.0
        self._policy_cross_source_limit = 8192
        # request_id -> (expires, call_n, decision, analysis_ms, detect_filter)
        self._policy_decision_by_request: Dict[str, tuple] = {}
        self._policy_decision_ttl_sec = 90.0
        self._policy_api_sem: Optional[asyncio.Semaphore] = None
        self._policy_http_session: Any = None
        self._policy_http_session_lock: Optional[asyncio.Lock] = None
        self.product_mode = read_product_mode()
        self.is_mirror = self.product_mode == "mirror"
        if self.is_mirror:
            accept_ms = int(
                os.environ.get(
                    "AIVAX_POLICY_API_ACCEPT_TIMEOUT_MS",
                    str(DEFAULT_MIRROR_ACCEPT_TIMEOUT_MS),
                )
                or DEFAULT_MIRROR_ACCEPT_TIMEOUT_MS
            )
            self.policy_api_timeout_ms = max(1, accept_ms)
            acquire_ms = int(
                os.environ.get(
                    "AIVAX_POLICY_API_ACQUIRE_TIMEOUT_MS",
                    str(self.policy_api_timeout_ms),
                )
                or self.policy_api_timeout_ms
            )
            self.policy_api_acquire_timeout_ms = max(
                self.policy_api_timeout_ms, max(1, acquire_ms)
            )

    async def start(self) -> None:
        self._policy_api_sem = asyncio.Semaphore(self.policy_api_max_inflight)
        self._policy_http_session_lock = asyncio.Lock()
        if self.pipeline_server_url and aiohttp is None:
            logging.warning("aiohttp is unavailable, policy api calls will use thread fallback")

    async def close(self) -> None:
        session = self._policy_http_session
        self._policy_http_session = None
        if session is None:
            return
        try:
            await session.close()
        except Exception:
            logging.exception("failed to close policy http session")

    def _mark_block_output_suppress(
        self,
        adapter_service_id: str,
        session_id: str,
        override_text: str,
    ) -> None:
        text = str(override_text or "").strip()
        if not text:
            return
        now = time.monotonic()
        sid = str(session_id or "").strip()
        adapter_sid = (adapter_service_id or "").lower().strip()
        if sid:
            self._block_suppress_by_session[sid] = (
                now + float(self._block_suppress_session_ttl_sec),
                text,
            )
        if adapter_sid:
            self._block_suppress_last_by_adapter[adapter_sid] = (
                now + float(self._block_suppress_adapter_ttl_sec),
                sid,
                text,
            )
        # Bound map sizes.
        if len(self._block_suppress_by_session) > 2048:
            cutoff = now
            dead = [k for k, (until, _) in self._block_suppress_by_session.items() if until <= cutoff]
            for k in dead:
                self._block_suppress_by_session.pop(k, None)

    def _lookup_block_output_suppress(
        self,
        adapter_service_id: str,
        session_id: str,
    ) -> str:
        now = time.monotonic()
        sid = str(session_id or "").strip()
        adapter_sid = (adapter_service_id or "").lower().strip()
        if sid:
            ent = self._block_suppress_by_session.get(sid)
            if ent and float(ent[0]) > now:
                return str(ent[1] or "").strip()
            if ent:
                self._block_suppress_by_session.pop(sid, None)
        # Follow-up history/batchexecute often has empty session right after BLOCK.
        if adapter_sid:
            ent = self._block_suppress_last_by_adapter.get(adapter_sid)
            if ent and float(ent[0]) > now:
                return str(ent[2] or "").strip()
            if ent:
                self._block_suppress_last_by_adapter.pop(adapter_sid, None)
        return ""

    def _clear_block_output_suppress(self, adapter_service_id: str, session_id: str) -> None:
        sid = str(session_id or "").strip()
        adapter_sid = (adapter_service_id or "").lower().strip()
        if sid:
            self._block_suppress_by_session.pop(sid, None)
        if adapter_sid:
            self._block_suppress_last_by_adapter.pop(adapter_sid, None)

    def _cached_policy_action(self, request_id: str) -> str:
        """Policy action already decided for this request_id, or empty."""
        rid = str(request_id or "").strip()
        if not rid:
            return ""
        cached = self._policy_decision_by_request.get(rid)
        if not cached or len(cached) < 3:
            return ""
        try:
            expires = float(cached[0])
            decision = cached[2]
        except Exception:
            logging.warning(
                "request_id=%s policy_decision_cache_unpack_failed n=%d",
                rid,
                len(cached),
            )
            return ""
        if expires <= time.monotonic():
            return ""
        code = ""
        if isinstance(decision, (tuple, list)) and decision:
            code = str(decision[0] or "")
        return code.upper()

    def _turn_has_own_nonblock_decision(self, request_id: str) -> bool:
        """PASS/MASKING (and any other non-BLOCK) must not inherit another turn's override."""
        action = self._cached_policy_action(request_id)
        return bool(action) and action != "BLOCK"

    def _request_was_blocked(self, request_id: str) -> bool:
        """True when this request_id already got a BLOCK decision (or has a block override)."""
        rid = str(request_id or "").strip()
        if not rid:
            return False
        if str(self._response_override_by_request_id.get(rid) or "").strip():
            return True
        return self._cached_policy_action(rid) == "BLOCK"

    def _queue_pending_response_override(self, adapter_service_id: str, override_text: str) -> None:
        sid = (adapter_service_id or "").lower().strip()
        text = (override_text or "").strip()
        if not sid or not text:
            return
        q = self._response_override_pending_by_service.setdefault(sid, [])
        q.append(text)
        # Prevent unbounded growth if upstream keeps failing to provide session IDs.
        if len(q) > 64:
            del q[:-64]

    def _peek_pending_response_override(self, adapter_service_id: str) -> str:
        """Non-destructive: pending BLOCK override may exist before response session binds."""
        sid = (adapter_service_id or "").lower().strip()
        if not sid:
            return ""
        q = self._response_override_pending_by_service.get(sid)
        if not q:
            return ""
        return str(q[0] or "").strip()

    def _pop_pending_response_override(self, adapter_service_id: str) -> str:
        sid = (adapter_service_id or "").lower().strip()
        if not sid:
            return ""
        q = self._response_override_pending_by_service.get(sid)
        if not q:
            return ""
        text = q.pop(0)
        if not q:
            self._response_override_pending_by_service.pop(sid, None)
        return text

    def _set_response_override(
        self,
        session_id: str,
        request_id: str,
        override_text: str,
    ) -> None:
        text = str(override_text or "").strip()
        if not text:
            return
        sid = (session_id or "").strip()
        rid = (request_id or "").strip()
        if sid:
            self._response_override_by_session[sid] = text
        if rid:
            self._response_override_by_request_id[rid] = text
            if sid:
                self._response_override_session_by_request_id[rid] = sid

    def _arm_cso_response_prefix(
        self,
        session_id: str,
        request_id: str,
        adapter_service_id: str,
        notice: str,
    ) -> None:
        text = str(notice or "").strip()
        if not text:
            return
        entry = (text, time.monotonic() + 120.0)
        sid = str(session_id or "").strip()
        rid = str(request_id or "").strip()
        if sid:
            self._cso_response_prefix_by_session[sid] = entry
        if rid:
            self._cso_response_prefix_by_request[rid] = entry
        if not sid and not rid:
            svc = (adapter_service_id or "").lower().strip()
            if svc:
                self._cso_response_prefix_pending.setdefault(svc, []).append(entry)

    def _fresh_cso_prefix(self, entry: Any) -> str:
        if not isinstance(entry, tuple) or len(entry) != 2:
            return ""
        text, expires = entry
        if float(expires or 0) <= time.monotonic():
            return ""
        return str(text or "").strip()

    def _lookup_cso_response_prefix(
        self,
        session_id: str,
        request_id: str,
        adapter_service_id: str,
    ) -> str:
        sid = str(session_id or "").strip()
        rid = str(request_id or "").strip()
        svc = (adapter_service_id or "").lower().strip()
        if sid and sid in self._cso_response_prefix_by_session:
            text = self._fresh_cso_prefix(self._cso_response_prefix_by_session.get(sid))
            if text:
                return text
            self._cso_response_prefix_by_session.pop(sid, None)
        if rid and rid in self._cso_response_prefix_by_request:
            text = self._fresh_cso_prefix(self._cso_response_prefix_by_request.get(rid))
            if text:
                return text
            self._cso_response_prefix_by_request.pop(rid, None)
        pending = self._cso_response_prefix_pending.get(svc) or []
        while pending:
            entry = pending.pop(0)
            text = self._fresh_cso_prefix(entry)
            if not text:
                continue
            if sid:
                self._cso_response_prefix_by_session[sid] = entry
            elif rid:
                self._cso_response_prefix_by_request[rid] = entry
            return text
        if svc and not pending:
            self._cso_response_prefix_pending.pop(svc, None)
        return ""

    def _clear_cso_response_prefix(self, session_id: str, request_id: str) -> None:
        sid = str(session_id or "").strip()
        rid = str(request_id or "").strip()
        if sid:
            self._cso_response_prefix_by_session.pop(sid, None)
        if rid:
            self._cso_response_prefix_by_request.pop(rid, None)

    def _rewrite_cso_open_response_prefix(
        self,
        adapter: Any,
        adapter_req: Dict[str, Any],
        payload: bytes,
        session_id: str,
        request_id: str,
        adapter_service_id: str,
        service_id: str,
        stream_done: bool,
    ) -> Optional[Dict[str, Any]]:
        if not adapter or not hasattr(adapter, "prefix_response"):
            return None
        prefix = self._lookup_cso_response_prefix(
            session_id, request_id, adapter_service_id
        )
        if not prefix:
            return None
        key = str(session_id or request_id or "").strip()
        adapter_req["_cso_response_prefix_key"] = key
        rewritten = None
        try:
            rewritten = adapter.prefix_response(adapter_req, payload, prefix)
        except Exception:
            logging.exception(
                "request_id=%s service=%s cso_open_response_prefix failed",
                request_id,
                service_id,
            )
            rewritten = None
        if stream_done:
            self._clear_cso_response_prefix(session_id, request_id)
            if hasattr(adapter, "clear_response_prefix_state"):
                try:
                    adapter.clear_response_prefix_state(key)
                except Exception:
                    logging.exception(
                        "request_id=%s service=%s clear_response_prefix_state failed",
                        request_id,
                        service_id,
                    )
        if rewritten is None:
            return None
        if not (isinstance(rewritten, tuple) and len(rewritten) == 2):
            return None
        rewritten_payload, reason = rewritten
        if not isinstance(rewritten_payload, (bytes, bytearray)) or not rewritten_payload:
            return None
        logging.info(
            "request_id=%s service=%s cso_open_response_prefix ok reason=%s in_bytes=%d out_bytes=%d",
            request_id,
            service_id,
            reason,
            len(payload or b""),
            len(rewritten_payload),
        )
        return build_rewrite_response(
            request_id,
            reason=f"policy_api:cso_open_notice:{reason}",
            payload=bytes(rewritten_payload),
        )

    def _lookup_response_override_session(self, session_id: str, request_id: str) -> str:
        sid = (session_id or "").strip()
        if sid:
            return sid
        rid = (request_id or "").strip()
        if rid:
            return str(self._response_override_session_by_request_id.get(rid) or "").strip()
        return ""

    def _lookup_response_override(
        self,
        session_id: str,
        request_id: str,
        adapter_service_id: str,
    ) -> str:
        sid = (session_id or "").strip()
        rid = (request_id or "").strip()
        adapter_sid = (adapter_service_id or "").lower().strip()
        pending_session_id = f"pending:{adapter_sid}" if adapter_sid else ""

        # This turn's own override wins, even on a shared conversation session.
        if rid:
            own = str(self._response_override_by_request_id.get(rid) or "").strip()
            if own:
                return own
        # A PASS/MASKING request must not pick up a later BLOCK stored on the
        # same chat session. Otherwise its answer is dropped or rewritten.
        if self._turn_has_own_nonblock_decision(rid):
            return ""

        for key in (sid, pending_session_id):
            if not key:
                continue
            val = str(self._response_override_by_session.get(key) or "").strip()
            if val:
                return val
        # Request BLOCK may have queued override before a stable session id existed.
        pending = self._peek_pending_response_override(adapter_service_id)
        if pending:
            return pending
        return ""

    def _bind_pending_response_override(
        self,
        adapter_req: Dict[str, Any],
        payload: bytes,
        adapter: Any,
        adapter_service_id: str,
        request_id: str,
        service_id: str,
    ) -> str:
        """Activate pending BLOCK override onto this response session before output_filter."""
        session_id = self._extract_session_id(adapter_req, payload, adapter)
        if self._user_consent_skip_override(
            adapter_service_id, session_id, request_id
        ):
            return session_id
        # Do not attach a queued BLOCK override onto a different turn that
        # already passed or was masked. Leave it for the blocked response.
        if self._turn_has_own_nonblock_decision(request_id):
            return session_id
        _asid = (adapter_service_id or "").lower().strip()
        pending_session_id = f"pending:{_asid}" if _asid else ""

        if not session_id:
            pending_text = self._pop_pending_response_override(adapter_service_id)
            if pending_text:
                session_id = pending_session_id
                self._set_response_override(session_id, request_id, pending_text)
                logging.info(
                    "request_id=%s service=%s response_override_activated_pending session_id=%s",
                    request_id,
                    service_id,
                    session_id,
                )
            elif pending_session_id in self._response_override_by_session:
                session_id = pending_session_id
        elif session_id not in self._response_override_by_session:
            pending_text = self._pop_pending_response_override(adapter_service_id)
            if pending_text:
                self._set_response_override(session_id, request_id, pending_text)
                logging.info(
                    "request_id=%s service=%s response_override_attached session_id=%s",
                    request_id,
                    service_id,
                    session_id,
                )
            elif pending_session_id in self._response_override_by_session:
                pending_text = self._response_override_by_session.pop(pending_session_id)
                self._set_response_override(session_id, request_id, pending_text)
                logging.info(
                    "request_id=%s service=%s response_override_rebind session_id=%s",
                    request_id,
                    service_id,
                    session_id,
                )
        if (
            session_id
            and not str(session_id).startswith("pending:")
            and (adapter_service_id or "").lower() != "timely"
        ):
            # Timely's response session_id is not the chat_id on the next send.
            # Moving the pending slot onto it makes 동의 miss the blocked turn.
            self._user_consent_rebind_request_to_session(
                adapter_service_id, request_id, session_id
            )
            if user_consent_enabled():
                extras = self._collect_consent_session_ids(
                    adapter, adapter_req, payload, session_id
                )
                self._user_consent_alias_sessions(
                    adapter_service_id, session_id, extras
                )
        return session_id

    def _clear_response_override(self, session_id: str, request_id: str) -> None:
        sid = (session_id or "").strip()
        rid = (request_id or "").strip()
        if sid:
            self._response_override_by_session.pop(sid, None)
            dead_rids = [
                k
                for k, v in self._response_override_session_by_request_id.items()
                if str(v or "").strip() == sid
            ]
            for k in dead_rids:
                self._response_override_session_by_request_id.pop(k, None)
                self._response_override_by_request_id.pop(k, None)
        if rid:
            self._response_override_by_request_id.pop(rid, None)
            self._response_override_session_by_request_id.pop(rid, None)

    def _mark_output_log_once(self, key: str) -> bool:
        token = (key or "").strip()
        if not token:
            return True
        with self._output_log_seen_lock:
            if token in self._output_log_seen_keys:
                return False
            self._output_log_seen_keys[token] = None
            while len(self._output_log_seen_keys) > self._output_log_seen_limit:
                oldest = next(iter(self._output_log_seen_keys))
                self._output_log_seen_keys.pop(oldest, None)
        return True

    @staticmethod
    def _policy_cross_source_dedup_key(
        adapter_service_id: str,
        direction: str,
        host: str,
        path: str,
        text: str,
        message_id: str = "",
    ) -> str:
        """Collapse the same answer seen twice for one turn.

        message_id is part of the key so a later turn that happens to repeat
        the same short answer (for example "ㄹㅇㅋㅋ") is still logged.
        """
        normalized = (text or "").strip()[:4096]
        digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]
        parts = [
            (direction or "").lower(),
            (adapter_service_id or "").lower(),
            (host or "").lower(),
            (path or "").split("?", 1)[0].lower(),
            digest,
        ]
        mid = (message_id or "").strip()
        if mid:
            parts.append(mid)
        return "|".join(parts)

    def _mark_policy_cross_source_once(self, key: str) -> bool:
        token = (key or "").strip()
        if not token:
            return True
        now = time.monotonic()
        with self._policy_cross_source_lock:
            expired = [
                k
                for k, ts in self._policy_cross_source_seen.items()
                if (now - ts) > self._policy_cross_source_ttl_sec
            ]
            for k in expired:
                self._policy_cross_source_seen.pop(k, None)
            if token in self._policy_cross_source_seen:
                return False
            self._policy_cross_source_seen[token] = now
            while len(self._policy_cross_source_seen) > self._policy_cross_source_limit:
                oldest = next(iter(self._policy_cross_source_seen))
                self._policy_cross_source_seen.pop(oldest, None)
        return True

    def _policy_api_url_for_direction(self, direction: str) -> str:
        if not self.pipeline_server_url:
            return ""
        path = self.pipeline_input_api_path
        if (direction or "").lower() == "response":
            path = self.pipeline_output_api_path
        if not path:
            return ""
        return f"{self.pipeline_server_url}:{self.pipeline_server_port}{path}"

    @staticmethod
    def _normalize_client_ip(raw: str) -> str:
        """Return a bare IPv4/IPv6 address, or "" (never a hostname / ip:port)."""
        s = str(raw or "").strip()
        if not s:
            return ""
        # [2001:db8::1]:443
        if s.startswith("["):
            end = s.find("]")
            if end > 1:
                s = s[1:end]
        elif s.count(":") == 1:
            # 10.0.3.115:58651 (Envoy Address::asString)
            host, _, port = s.partition(":")
            if port.isdigit():
                s = host
        try:
            return str(ipaddress.ip_address(s))
        except ValueError:
            return ""

    @staticmethod
    def _resolve_client_host(req: Dict[str, Any]) -> str:
        meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        req_headers = meta.get("request_headers") if isinstance(meta.get("request_headers"), dict) else {}

        def from_forwarded(raw: Any) -> str:
            s = str(raw or "").strip()
            if not s:
                return ""
            return PolicyEngine._normalize_client_ip(s.split(",", 1)[0].strip())

        # Product: decryptor attaches XFF on AI traffic — that hop is the user IP.
        for raw in (
            meta.get("x_forwarded_for"),
            req_headers.get("x-forwarded-for"),
        ):
            ip = from_forwarded(raw)
            if ip:
                return ip
        for key in ("client_ip", "clientIp"):
            ip = PolicyEngine._normalize_client_ip(str(meta.get(key) or ""))
            if ip:
                return ip
        for key in ("true_client_ip", "x_real_ip"):
            ip = from_forwarded(meta.get(key))
            if ip:
                return ip
        # Do NOT fall back to meta.host (e.g. chatgpt.com) — UI shows it as 사용자 IP.
        return ""

    @staticmethod
    def _resolve_ai_service_code(adapter_service_id: str) -> int:
        return resolve_ai_service_code(adapter_service_id)

    @staticmethod
    def _resolve_adapter_service_id(req: Dict[str, Any], service_id: str) -> str:
        sid = (service_id or "").lower()
        meta = req.get("meta")
        host = ""
        path = ""
        if isinstance(meta, dict):
            host = str(meta.get("host") or "").lower()
            path = str(meta.get("path") or "").lower()

        # Host/path-based override first: avoid wrong adapter when sslproxy service_id is stale/misdetected.
        if "accounts.google.com" in host and path.split("?", 1)[0].rstrip("/") == "/listaccounts":
            return "gemini"
        if "gemini.google.com" in host or "bard.google.com" in host:
            return "gemini"
        if "geminiweb-pa.clients6.google.com" in host:
            return "gemini"
        if "push.clients6.google.com" in host and "/upload" in path:
            return "gemini"
        try:
            from adapters.gemini_engine import is_google_ai_mode_request

            if is_google_ai_mode_request(host, path):
                return "gemini"
        except Exception:
            pass
        if "chatgpt.com" in host and "conversation" in path:
            return "gpt"
        if "oaiusercontent.com" in host:
            try:
                from adapters import github_copilot_upload as gcu

                copilot_sid = gcu.resolve_oai_upload_service_id(host, path)
                if copilot_sid:
                    return copilot_sid
            except Exception:
                pass
            return "gpt"
        if "chatgpt.com" in host and "/backend-api/files" in path:
            return "gpt"
        if "claude.ai" in host:
            return "claude"
        if "grok.com" in host:
            return "grok"
        if "storage.googleapis.com" in host and "xai-enterprise-api-mc-assets-prod" in path:
            return "grok"
        if "perplexity.ai" in host:
            return "perplexity"
        if "ppl-ai-file-upload" in host and (
            "amazonaws.com" in host or "cloudfront.net" in host
        ):
            return "perplexity"
        if "prod-files-secure" in host and "amazonaws.com" in host:
            return "notion"
        if "file.notion.com" in host:
            return "notion"
        if ("notion.so" in host or "notion.com" in host or host.endswith("notion.site")) and (
            "runinferencetranscript" in path
            or "savetransactionsfanout" in path
            or "getuploadfileurlforassistantchattranscriptupload" in path
            or "getsignedfileurls" in path
        ):
            return "notion"
        if (
            "githubcopilot.com" in host
            or "copilot-proxy.githubusercontent.com" in host
            or "origin-tracker.githubusercontent.com" in host
            or "copilot-telemetry.githubusercontent.com" in host
        ):
            return "github_copilot"
        if "uploads.github.com" in host and "/copilot/chat/attachments" in path:
            return "github_copilot"
        if "github.com" in host and "github-copilot/chat/attachments" in path:
            return "github_copilot"
        if "user-attachments.githubusercontent.com" in host or "private-user-images.githubusercontent.com" in host:
            return "github_copilot"
        if "api.github.com" in host and (
            "copilot" in path
            or "/copilot_" in path
            or "/user/copilot" in path
            or path.split("?", 1)[0].rstrip("/") in ("/user", "/user/emails")
        ):
            return "github_copilot"
        if "gateway-api.wrks.ai" in host or "wrks.ai" in host:
            return "wrks"
        if "timelyai.io" in host:
            return "timely"
        if _is_genspark_host(host):
            return "genspark"
        if "10.0.50.84" in host and (
            "/api/agents/chat" in path or path.rstrip("/") == "/api/user"
        ):
            return "chat_version"

        if sid and sid != "generic":
            return sid
        return sid

    def _build_policy_api_request_obj(
        self,
        req: Dict[str, Any],
        payload: bytes,
        extracted_text: str,
        adapter_service_id: str,
        adapter: Any,
    ) -> Optional[Dict[str, Any]]:
        direction = str(req.get("direction") or "").lower()
        service_code = self._resolve_ai_service_code(adapter_service_id)
        client_host = self._resolve_client_host(req)

        policy_req_obj: Optional[Dict[str, Any]] = None
        adapter_built = False
        if adapter and hasattr(adapter, "build_policy_api_payload"):
            try:
                candidate = adapter.build_policy_api_payload(req, payload, extracted_text)
                adapter_built = True
            except Exception:
                logging.exception("adapter build_policy_api_payload failed service=%s", adapter_service_id)
                candidate = None
            if isinstance(candidate, dict):
                policy_req_obj = candidate

        if policy_req_obj is None:
            if direction == "response":
                policy_req_obj = {
                    "llm_output": extracted_text,
                    "user_id": "",
                    "email": "",
                    "ai_service": service_code,
                    "client_host": client_host,
                    "session_id": "",
                    "message_id": "",
                }
            elif adapter_built:
                # Adapter explicitly declined (e.g. empty / non-prompt). Do not invent
                # prompt="" OpenSearch/UI rows via the generic fallback.
                return None
            else:
                policy_req_obj = {
                    "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
                    "prompt": extracted_text,
                    "encoding": False,
                    "user_id": "",
                    "email": "",
                    "ai_service": service_code,
                    "client_host": client_host,
                    "session_id": "",
                    "attachments": [{"id": "", "size": 0, "name": "", "mime_type": ""}],
                    "message_id": "",
                }

        adapter_sid = (adapter_service_id or "").lower()
        payload_ai_service = int(policy_req_obj.get("ai_service") or 0)
        if adapter_sid == "chat_version" and payload_ai_service:
            pass
        elif service_code:
            policy_req_obj["ai_service"] = service_code
        policy_req_obj["client_host"] = client_host
        meta_for_event = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        policy_req_obj["event_time"] = iso_event_time(
            req.get("event_time") or meta_for_event.get("event_time")
        )
        if direction == "request":
            attachments_final = bool(getattr(adapter, "POLICY_ATTACHMENTS_FINAL", False)) if adapter else False
            if not attachments_final:
                policy_req_obj["attachments"] = resolve_saved_policy_attachments(
                    policy_req_obj.get("attachments")
                )
                if (adapter_service_id or "").lower() in ("gpt", "chatgpt"):
                    policy_req_obj["attachments"] = _policy_attachments_strip_ids_for_chatgpt(
                        policy_req_obj.get("attachments")
                    )
        # Unify message_id with request_id so Input/Output Filter match for UI (no store, no lock).
        # Grok /ws/mgw request tees use "<upgrade_id>-ws-<hex>" — strip suffix so output
        # (which keeps the upgrade request_id) can merge into the same UI row.
        _meta_early = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        ui_message_id = (
            str(req.get("_chatgpt_ui_message_id") or "").strip()
            or str(req.get("_gemini_ui_message_id") or "").strip()
            or str(req.get("_grok_ui_message_id") or "").strip()
            or str(req.get("_claude_ui_message_id") or "").strip()
            or str(req.get("_timely_ui_message_id") or "").strip()
            or str(req.get("_github_copilot_ui_message_id") or "").strip()
            or str(req.get("_genspark_ui_message_id") or "").strip()
        )
        if not ui_message_id:
            rid = str(req.get("request_id") or "")
            if "-ws-" in rid:
                ui_message_id = rid.rsplit("-ws-", 1)[0]
        policy_req_obj["message_id"] = ui_message_id or str(req.get("request_id") or "")
        if ui_message_id and ui_message_id != str(req.get("request_id") or ""):
            logging.info(
                "request_id=%s service=%s input_message_id_remap parent=%s path=%s",
                str(req.get("request_id") or ""),
                adapter_service_id,
                ui_message_id,
                str(_meta_early.get("path") or ""),
            )

        # Inject file upload metadata from sslproxy meta (upload_id, upload_filename)
        meta = _meta_early
        # HA global identity (central key = sensor_instance_id + sensor_boot_id + request_id + direction)
        policy_req_obj["sensor_instance_id"] = str(
            meta.get("sensor_instance_id") or os.environ.get("AIVAX_SENSOR_INSTANCE_ID") or ""
        )
        policy_req_obj["sensor_boot_id"] = str(
            meta.get("sensor_boot_id") or os.environ.get("AIVAX_SENSOR_BOOT_ID") or ""
        )
        policy_req_obj["policy_generation"] = str(
            os.environ.get("AIVAX_POLICY_GENERATION") or ""
        )
        upload_meta = _normalize_upload_meta(meta)
        upload_id = upload_meta["upload_id"]
        upload_filename = upload_meta["upload_filename"]
        if upload_id or upload_filename:
            policy_req_obj["upload_id"] = upload_id
            policy_req_obj["upload_filename"] = upload_filename
            logging.info(
                "request_id=%s service=%s upload_id=%s upload_filename=%s",
                str(req.get("request_id") or ""),
                adapter_service_id,
                upload_id,
                upload_filename,
            )

        stamp_policy_model(req, payload, policy_req_obj, adapter)
        return policy_req_obj

    async def _ensure_policy_http_session(self) -> Any:
        if aiohttp is None:
            return None
        session = self._policy_http_session
        if session is not None and not session.closed:
            return session

        if self._policy_http_session_lock is None:
            self._policy_http_session_lock = asyncio.Lock()
        async with self._policy_http_session_lock:
            session = self._policy_http_session
            if session is not None and not session.closed:
                return session

            timeout = aiohttp.ClientTimeout(total=self.policy_api_timeout_ms / 1000.0)
            connector = aiohttp.TCPConnector(
                limit=self.policy_api_pool_limit,
                limit_per_host=self.policy_api_pool_limit,
                ttl_dns_cache=60,
            )
            self._policy_http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
            return self._policy_http_session

    def _policy_http_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "accept": "application/json"}
        # Label only. The pipeline judges from mode.json, not this header.
        # proxy is sent as itself and still takes the non-mirror path above.
        if self.product_mode in ("mirror", "inline", "proxy"):
            headers[AIVAX_MODE_HEADER] = self.product_mode
        return headers

    def _post_json_sync(self, url: str, body: bytes, timeout_ms: int) -> tuple[int, bytes]:
        http_req = urllib.request.Request(
            url,
            data=body,
            headers=self._policy_http_headers(),
            method="POST",
        )

        raw = b""
        status = 0
        try:
            with urllib.request.urlopen(http_req, timeout=timeout_ms / 1000.0) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            raw = exc.read() if hasattr(exc, "read") else b""
        return status, raw

    async def _post_json_async(self, url: str, body: bytes) -> tuple[int, bytes]:
        if aiohttp is None:
            try:
                return await asyncio.to_thread(self._post_json_sync, url, body, self.policy_api_timeout_ms)
            except Exception as exc:
                raise PolicyApiError("request_failed") from exc

        session = await self._ensure_policy_http_session()
        if session is None:
            try:
                return await asyncio.to_thread(self._post_json_sync, url, body, self.policy_api_timeout_ms)
            except Exception as exc:
                raise PolicyApiError("request_failed") from exc

        try:
            async with session.post(url, data=body, headers=self._policy_http_headers()) as resp:
                return int(resp.status or 0), await resp.read()
        except asyncio.TimeoutError as exc:
            raise PolicyApiError("timeout") from exc
        except Exception as exc:
            raise PolicyApiError("request_failed") from exc

    async def _call_policy_api(
        self,
        req: Dict[str, Any],
        payload: bytes,
        extracted_text: str,
        adapter_service_id: str,
        adapter: Any,
    ) -> Optional[tuple[str, str, str]]:
        direction = str(req.get("direction") or "").lower()
        api_url = self._policy_api_url_for_direction(direction)
        if not api_url:
            return None
        if direction != "request":
            return None
        if (adapter_service_id or "").lower() not in (
            "gpt",
            "chatgpt",
            "gemini",
            "claude",
            "grok",
            "perplexity",
            "notion",
            "github_copilot",
            "wrks",
            "chat_version",
            "timely",
            "genspark",
        ):
            return None

        policy_req_obj = self._build_policy_api_request_obj(
            req=req,
            payload=payload,
            extracted_text=extracted_text,
            adapter_service_id=adapter_service_id,
            adapter=adapter,
        )
        # 확장자/크기 차단은 pipeline file_block_filter가 담당 (engine 선차단 비활성).
        # denied = str(req.get("_chatgpt_file_collection_denied") or "").strip()
        # if denied:
        #     logging.info(
        #         "request_id=%s service=%s file_collection_denied=%s → BLOCK",
        #         str(req.get("request_id") or ""),
        #         adapter_service_id,
        #         denied,
        #     )
        #     return (
        #         "BLOCK",
        #         "",
        #         "허용되지 않은 파일 확장자이거나 크기 제한을 초과했습니다.",
        #     )
        if policy_req_obj is None:
            return None
        try:
            mid = str(policy_req_obj.get("message_id") or "").strip()
            if mid:
                req["_policy_message_id"] = mid
        except Exception:
            pass
        # Visibility: how many named attachments go to Pipeline/UI (multi-file).
        try:
            atts = policy_req_obj.get("attachments") if isinstance(policy_req_obj, dict) else None
            _sid = (adapter_service_id or "").lower()
            if isinstance(atts, list) and _sid in (
                "gemini",
                "perplexity",
                "grok",
                "claude",
                "wrks",
                "timely",
            ):
                named = [
                    str((a or {}).get("name") or "")[:60]
                    for a in atts
                    if isinstance(a, dict) and str((a or {}).get("name") or "").strip()
                ]
                on_disk = sum(
                    1
                    for a in atts
                    if isinstance(a, dict)
                    and str((a or {}).get("id") or "").startswith("/run/aivax/attachments/")
                )
                if named or on_disk:
                    logging.info(
                        "request_id=%s service=%s attachments named=%d on_disk=%d names=%s",
                        str(req.get("request_id") or ""),
                        adapter_service_id,
                        len(named),
                        on_disk,
                        named[:8],
                    )
        except Exception:
            pass
        body = json.dumps(policy_req_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        rid = str(req.get("request_id") or "")

        # Dedup: same conversation request_id can hit evaluate multiple times
        # (early body + full eve + post-MASK re-eval). Pipeline AddToLogQueue runs
        # on every multiple_filter call, so skip repeat HTTP calls.
        now = time.monotonic()
        cached = self._policy_decision_by_request.get(rid) if rid else None
        if cached and float(cached[0]) > now:
            call_n = int(cached[1]) + 1
            decision = cached[2]
            self._policy_decision_by_request[rid] = (
                cached[0],
                call_n,
                decision,
                cached[3] if len(cached) > 3 else None,
                cached[4] if len(cached) > 4 else None,
                cached[5] if len(cached) > 5 else None,
            )
            if len(cached) > 3 and cached[3] is not None:
                req["_policy_analysis_ms"] = cached[3]
            if len(cached) > 4 and cached[4]:
                req["_policy_detect_filter"] = cached[4]
            cso_cached = cached[5] if len(cached) > 5 else None
            if isinstance(cso_cached, tuple) and len(cso_cached) >= 2:
                if cso_cached[0]:
                    req["_policy_cso_status"] = cso_cached[0]
                if cso_cached[1]:
                    req["_policy_cso_message"] = cso_cached[1]
            logging.info(
                "request_id=%s service=%s policy_api_cache_hit call_n=%d decision=%s",
                rid,
                adapter_service_id,
                call_n,
                decision[0],
            )
            return decision

        logging.debug(
            "input_filter request json request_id=%s url=%s body=%s",
            rid,
            api_url,
            body.decode("utf-8", errors="replace"),
        )

        if self._policy_api_sem is None:
            # 동시에 정책 서버에 물어볼 수 있는 질문의 개수 (Throughput 유지)
            self._policy_api_sem = asyncio.Semaphore(self.policy_api_max_inflight)
        acquired = False
        try:
            await asyncio.wait_for(
                self._policy_api_sem.acquire(),
                timeout=self.policy_api_acquire_timeout_ms / 1000.0,
            )
            acquired = True
        except asyncio.TimeoutError as exc:
            raise PolicyApiError("queue_timeout") from exc

        try:
            t0 = time.monotonic()
            status, raw = await self._post_json_async(api_url, body)
            analysis_ms = max(1, int(round((time.monotonic() - t0) * 1000)))
            req["_policy_analysis_ms"] = analysis_ms
        finally:
            if acquired:
                self._policy_api_sem.release()

        logging.debug(
            "input_filter response json request_id=%s http_status=%s body=%s",
            rid,
            status,
            safe_utf8(raw) if raw else "",
        )

        if status >= 400:
            logging.warning("policy api returned http error status=%s", status)
        if not raw:
            raise PolicyApiError(f"empty_response:{status}")

        try:
            obj = json.loads(safe_utf8(raw))
        except Exception as exc:
            raise PolicyApiError(f"invalid_json:{status}") from exc

        decision = parse_policy_api_response(obj)
        if decision is None:
            raise PolicyApiError(f"decision_not_found:{status}")
        detect_filter = extract_policy_detect_filter(obj)
        if detect_filter:
            req["_policy_detect_filter"] = detect_filter
        cso_status, cso_message = extract_policy_cso(obj)
        if cso_status:
            req["_policy_cso_status"] = cso_status
        if cso_message:
            req["_policy_cso_message"] = cso_message
        if rid:
            expires = time.monotonic() + float(self._policy_decision_ttl_sec)
            self._policy_decision_by_request[rid] = (
                expires,
                1,
                decision,
                req.get("_policy_analysis_ms"),
                req.get("_policy_detect_filter"),
                (cso_status, cso_message),
            )
            if len(self._policy_decision_by_request) > 4096:
                cutoff = time.monotonic()
                dead = [
                    k
                    for k, row in self._policy_decision_by_request.items()
                    if float(row[0]) <= cutoff
                ]
                for k in dead:
                    self._policy_decision_by_request.pop(k, None)
        return decision

    @staticmethod

    @staticmethod
    def _user_consent_canonical_service(adapter_service_id: str) -> str:
        return _canonical_service_gate_key(adapter_service_id)

    def _user_consent_key(self, adapter_service_id: str, session_id: str) -> str:
        svc = self._user_consent_canonical_service(adapter_service_id)
        sid = str(session_id or "").strip()
        return f"{svc}|session|{sid}"

    def _user_consent_ip_key(self, adapter_service_id: str, client_ip: str) -> str:
        svc = self._user_consent_canonical_service(adapter_service_id)
        ip = str(client_ip or "").strip()
        return f"{svc}|ip|{ip}"

    def _user_consent_store(
        self,
        adapter_service_id: str,
        session_id: str,
        client_ip: str,
        prompt: str,
        message_id: str,
        attachments: Any = None,
        *,
        cso_blocked: bool = False,
        cso_status: str = "",
        cso_message: str = "",
        block_message: str = "",
    ) -> bool:
        if not user_consent_enabled():
            return False
        text = str(prompt or "").strip()
        if is_user_consent_phrase(text):
            return False
        # A file-only turn extracts no prompt, but the card still asks for 동의.
        # Keep the slot so the replay can put the file refs back.
        if not text:
            if cso_blocked or attachments is None:
                return False
            text = "첨부파일"
        sid = str(session_id or "").strip()
        ip = str(client_ip or "").strip()
        mid = str(message_id or "").strip()
        if not sid and not ip and not mid:
            return False
        expires = time.monotonic() + float(user_consent_ttl_sec())
        stored_status = (
            str(cso_status or "").strip().lower()
            if cso_blocked
            else ""
        )
        entry = {
            "prompt": text,
            "message_id": mid,
            "expires": expires,
            "client_ip": ip,
            "session_id": sid,
            # File refs of the blocked turn. Replayed only on the same chat + IP.
            # A CSO grade block keeps the slot only so 동의 cannot replay it.
            "attachments": None if cso_blocked else attachments,
            "cso_blocked": bool(cso_blocked),
            "cso_status": stored_status,
            "cso_message": str(cso_message or ""),
            "block_message": str(block_message or ""),
        }
        with self._user_consent_lock:
            if sid:
                self._user_consent_pending[self._user_consent_key(adapter_service_id, sid)] = dict(entry)
            elif ip:
                self._user_consent_pending[self._user_consent_ip_key(adapter_service_id, ip)] = dict(entry)
            else:
                svc = self._user_consent_canonical_service(adapter_service_id)
                self._user_consent_pending[f"{svc}|rid|{mid}"] = dict(entry)
        return True

    @staticmethod
    def _restore_consent_attachments(
        adapter: Any,
        adapter_req: Dict[str, Any],
        payload: bytes,
        consent_hit: Dict[str, Any],
        session_ids: list[str],
        client_ip: str,
        prompt: str,
        request_id: str,
        service_id: str,
    ) -> bytes:
        """Put the blocked turn's files back on the 동의 replay.

        Only the same chat and client get them back. On any mismatch or
        failure the replay stays prompt-only, which is the old behavior.
        """
        state = consent_hit.get("attachments")
        if not state or not adapter or not hasattr(adapter, "restore_consent_attachments"):
            return payload
        stored_sid = str(consent_hit.get("session_id") or "").strip()
        ids = {str(s or "").strip() for s in session_ids if str(s or "").strip()}
        if stored_sid and stored_sid not in ids:
            logging.info(
                "request_id=%s service=%s user_consent_attachments_skip reason=session_mismatch",
                request_id,
                service_id,
            )
            return payload
        stored_ip = str(consent_hit.get("client_ip") or "").strip()
        if stored_ip and client_ip and stored_ip != client_ip:
            logging.info(
                "request_id=%s service=%s user_consent_attachments_skip reason=client_mismatch",
                request_id,
                service_id,
            )
            return payload
        try:
            try:
                restored = adapter.restore_consent_attachments(
                    adapter_req, payload, state, prompt=prompt
                )
            except TypeError:
                restored = adapter.restore_consent_attachments(adapter_req, payload, state)
        except Exception:
            logging.exception(
                "request_id=%s service=%s user_consent_attachments_restore failed",
                request_id,
                service_id,
            )
            return payload
        if not isinstance(restored, (bytes, bytearray)) or not restored:
            logging.warning(
                "request_id=%s service=%s user_consent_attachments_restore_failed; prompt only",
                request_id,
                service_id,
            )
            return payload
        logging.info(
            "request_id=%s service=%s user_consent_attachments_restored in_bytes=%d out_bytes=%d",
            request_id,
            service_id,
            len(payload or b""),
            len(restored),
        )
        return bytes(restored)

    @staticmethod
    def _restore_cso_open_attachments(
        adapter: Any,
        adapter_req: Dict[str, Any],
        original_payload: bytes,
        rewritten_payload: bytes,
        prompt: str,
        request_id: str,
        service_id: str,
    ) -> bytes:
        """O등급 안내를 앞에 붙인 뒤에도 원래 첨부 참조를 보존한다.

        기존 MASKING 리라이터는 외부 전송을 막기 위해 파일 참조를 제거한다.
        O등급은 전송 허용이므로, 같은 어댑터의 동의 재첨부 구현을 바로 적용한다.
        재첨부할 수 없으면 안내문보다 원문을 우선해 파일 없는 전송을 막는다.
        """
        if (
            not adapter
            or not hasattr(adapter, "consent_attachment_state")
            or not hasattr(adapter, "restore_consent_attachments")
        ):
            return rewritten_payload
        try:
            state = adapter.consent_attachment_state(adapter_req, original_payload)
        except Exception:
            logging.exception(
                "request_id=%s service=%s cso_open_attachment_state failed",
                request_id,
                service_id,
            )
            return original_payload
        if not state:
            return rewritten_payload
        try:
            try:
                restored = adapter.restore_consent_attachments(
                    adapter_req, rewritten_payload, state, prompt=prompt
                )
            except TypeError:
                restored = adapter.restore_consent_attachments(
                    adapter_req, rewritten_payload, state
                )
        except Exception:
            logging.exception(
                "request_id=%s service=%s cso_open_attachment_restore failed",
                request_id,
                service_id,
            )
            return original_payload
        if not isinstance(restored, (bytes, bytearray)) or not restored:
            logging.warning(
                "request_id=%s service=%s cso_open_attachment_restore_failed; "
                "sending original request",
                request_id,
                service_id,
            )
            return original_payload
        logging.info(
            "request_id=%s service=%s cso_open_attachments_restored "
            "in_bytes=%d out_bytes=%d",
            request_id,
            service_id,
            len(rewritten_payload or b""),
            len(restored),
        )
        return bytes(restored)

    def _user_consent_lookup(
        self, adapter_service_id: str, session_id: str, client_ip: str = ""
    ) -> Optional[Dict[str, Any]]:
        sid = str(session_id or "").strip()
        now = time.monotonic()
        with self._user_consent_lock:
            dead = [k for k, ent in self._user_consent_pending.items() if float(ent.get("expires") or 0) <= now]
            for k in dead:
                self._user_consent_pending.pop(k, None)
            if not sid:
                # Session-bearing lookup never falls back to IP (prevents cross-chat leak).
                return None
            key = self._user_consent_key(adapter_service_id, sid)
            hit = self._user_consent_pending.get(key)
            if isinstance(hit, dict):
                return dict(hit)
            return None

    def _user_consent_lookup_ip_only(
        self, adapter_service_id: str, client_ip: str
    ) -> Optional[Dict[str, Any]]:
        ip = str(client_ip or "").strip()
        if not ip:
            return None
        now = time.monotonic()
        with self._user_consent_lock:
            dead = [k for k, ent in self._user_consent_pending.items() if float(ent.get("expires") or 0) <= now]
            for k in dead:
                self._user_consent_pending.pop(k, None)
            key = self._user_consent_ip_key(adapter_service_id, ip)
            hit = self._user_consent_pending.get(key)
            if isinstance(hit, dict):
                return dict(hit)
            return None

    def _user_consent_consume(
        self, adapter_service_id: str, session_id: str, client_ip: str = ""
    ) -> Optional[Dict[str, Any]]:
        sid = str(session_id or "").strip()
        now = time.monotonic()
        with self._user_consent_lock:
            dead = [k for k, ent in self._user_consent_pending.items() if float(ent.get("expires") or 0) <= now]
            for k in dead:
                self._user_consent_pending.pop(k, None)
            if not sid:
                return None
            key = self._user_consent_key(adapter_service_id, sid)
            hit = self._user_consent_pending.pop(key, None)
            if isinstance(hit, dict):
                return dict(hit)
            return None

    def _user_consent_clear_session(self, adapter_service_id: str, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        key = self._user_consent_key(adapter_service_id, sid)
        with self._user_consent_lock:
            self._user_consent_pending.pop(key, None)
            self._user_consent_output_pending.pop(key, None)

    def _collect_consent_session_ids(
        self,
        adapter: Any,
        adapter_req: Dict[str, Any],
        payload: bytes,
        session_id: str = "",
    ) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()

        def _add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in seen:
                seen.add(text)
                ids.append(text)

        _add(session_id)
        if adapter is not None and hasattr(adapter, "collect_consent_session_ids"):
            try:
                for item in adapter.collect_consent_session_ids(adapter_req, payload) or []:
                    _add(item)
            except Exception:
                logging.exception(
                    "collect_consent_session_ids failed service=%s",
                    adapter_req.get("service_id") if isinstance(adapter_req, dict) else "",
                )
        return ids

    def _consent_bind_session(self, session_id: str, consent_ids: list[str]) -> str:
        """Session key for a pending slot. A collected chat id wins over the IP fallback."""
        sid = str(session_id or "").strip()
        if sid:
            return sid
        for item in consent_ids:
            text = str(item or "").strip()
            if text:
                return text
        return ""

    def _user_consent_alias_sessions(
        self,
        adapter_service_id: str,
        source_session: str,
        extra_ids: list[str],
    ) -> bool:
        """Copy pending onto extra thread ids (Perplexity backend_uuid vs frontend uuid)."""
        extras = [str(x or "").strip() for x in extra_ids if str(x or "").strip()]
        src = str(source_session or "").strip()
        ids: list[str] = []
        for sid in [src, *extras]:
            if sid and sid not in ids:
                ids.append(sid)
        if not ids:
            return False
        hit = self._user_consent_lookup(adapter_service_id, src) if src else None
        if hit is None:
            for sid in extras:
                hit = self._user_consent_lookup(adapter_service_id, sid)
                if hit is not None:
                    break
        if not isinstance(hit, dict):
            return False
        with self._user_consent_lock:
            for sid in ids:
                self._user_consent_pending[
                    self._user_consent_key(adapter_service_id, sid)
                ] = dict(hit)
        return True

    def _user_consent_lookup_any(
        self, adapter_service_id: str, session_ids: list[str], client_ip: str = ""
    ) -> Optional[Dict[str, Any]]:
        for sid in session_ids:
            hit = self._user_consent_lookup(adapter_service_id, sid, client_ip)
            if hit is not None:
                return hit
        return None

    def _user_consent_consume_any(
        self,
        adapter_service_id: str,
        session_ids: list[str],
        client_ip: str = "",
    ) -> Optional[Dict[str, Any]]:
        ids = [str(x or "").strip() for x in session_ids if str(x or "").strip()]
        now = time.monotonic()
        svc = self._user_consent_canonical_service(adapter_service_id)
        prefix = f"{svc}|session|"
        with self._user_consent_lock:
            dead = [
                k
                for k, ent in self._user_consent_pending.items()
                if float(ent.get("expires") or 0) <= now
            ]
            for k in dead:
                self._user_consent_pending.pop(k, None)
            hit = None
            for sid in ids:
                key = self._user_consent_key(adapter_service_id, sid)
                ent = self._user_consent_pending.get(key)
                if isinstance(ent, dict):
                    hit = dict(ent)
                    break
            if not isinstance(hit, dict):
                return None
            mid = str(hit.get("message_id") or "").strip()
            drop = []
            for key, ent in self._user_consent_pending.items():
                if not str(key).startswith(prefix) or not isinstance(ent, dict):
                    continue
                if mid and str(ent.get("message_id") or "").strip() == mid:
                    drop.append(key)
            for key in drop:
                self._user_consent_pending.pop(key, None)
            return hit

    def _user_consent_rebind_ip_to_session(
        self, adapter_service_id: str, client_ip: str, session_id: str
    ) -> bool:
        """Move empty-session IP pending onto a concrete session_id (same BLOCK turn)."""
        sid = str(session_id or "").strip()
        ip = str(client_ip or "").strip()
        # pending:{service} is a placeholder until conversation_id exists.
        # Binding IP pending onto it hides the slot; later 동의 misses the real session.
        if not sid or not ip or sid.startswith("pending:"):
            return False
        ip_key = self._user_consent_ip_key(adapter_service_id, ip)
        sess_key = self._user_consent_key(adapter_service_id, sid)
        with self._user_consent_lock:
            hit = self._user_consent_pending.pop(ip_key, None)
            if not isinstance(hit, dict):
                return False
            if float(hit.get("expires") or 0) <= time.monotonic():
                return False
            self._user_consent_pending[sess_key] = dict(hit)
        return True

    def _user_consent_rebind_request_to_session(
        self,
        adapter_service_id: str,
        request_id: str,
        session_id: str,
    ) -> bool:
        """Move empty-session pending onto the session that appears on the BLOCK response."""
        rid = str(request_id or "").strip()
        sid = str(session_id or "").strip()
        if not rid or not sid or sid.startswith("pending:"):
            return False
        sess_key = self._user_consent_key(adapter_service_id, sid)
        now = time.monotonic()
        with self._user_consent_lock:
            hit_key = None
            hit = None
            for key, ent in list(self._user_consent_pending.items()):
                if not isinstance(ent, dict):
                    continue
                if str(ent.get("message_id") or "").strip() != rid:
                    continue
                hit_key, hit = key, ent
                break
            if not isinstance(hit, dict) or not hit_key:
                return False
            if float(hit.get("expires") or 0) <= now:
                self._user_consent_pending.pop(hit_key, None)
                return False
            if hit_key != sess_key:
                self._user_consent_pending.pop(hit_key, None)
                self._user_consent_pending[sess_key] = dict(hit)
        logging.info(
            "user_consent_rebind_request service=%s request_id=%s session=%s",
            self._user_consent_canonical_service(adapter_service_id),
            rid[:64],
            sid[:64],
        )
        return True

    def _user_consent_arm_output_patch(
        self,
        adapter_service_id: str,
        session_id: str,
        client_ip: str,
        message_id: str,
    ) -> bool:
        """Remember BLOCK message_id so the consent response can patch that log row."""
        mid = str(message_id or "").strip()
        if not mid:
            return False
        sid = str(session_id or "").strip()
        ip = str(client_ip or "").strip()
        if not sid and not ip:
            return False
        entry = {
            "message_id": mid,
            "expires": time.monotonic() + float(user_consent_ttl_sec()),
        }
        with self._user_consent_lock:
            if sid:
                self._user_consent_output_pending[
                    self._user_consent_key(adapter_service_id, sid)
                ] = dict(entry)
            else:
                self._user_consent_output_pending[
                    self._user_consent_ip_key(adapter_service_id, ip)
                ] = dict(entry)
        return True

    async def _register_user_consent_input_log(
        self,
        *,
        adapter_service_id: str,
        consent_message_id: str,
        original_prompt: str,
        client_ip: str,
        request_id: str = "",
    ) -> None:
        """Tell Pipeline the BLOCK row will be patched after the model answers.

        Without this, output_filter parks the real reply as late-output and the
        expiry job discards it — the dashboard keeps the block notice forever.
        """
        mid = str(consent_message_id or "").strip()
        if not mid:
            return
        api_url = self._policy_api_url_for_direction("request")
        if not api_url:
            return
        body_obj = {
            "filter_list": ["input_filter"],
            "prompt": str(original_prompt or ""),
            "encoding": False,
            "user_id": "",
            "email": "",
            "ai_service": self._resolve_ai_service_code(adapter_service_id),
            "client_host": str(client_ip or ""),
            "session_id": "",
            "attachments": [],
            "message_id": mid,
            "user_consent": "on",
        }
        body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        try:
            status, raw = await self._post_json_async(api_url, body)
            logging.info(
                "request_id=%s service=%s user_consent_input_registered "
                "message_id=%s status=%s body=%s",
                request_id or "-",
                adapter_service_id,
                mid[:64],
                status,
                (safe_utf8(raw) if raw else "")[:200],
            )
        except Exception:
            logging.exception(
                "request_id=%s service=%s user_consent_input_register_failed message_id=%s",
                request_id or "-",
                adapter_service_id,
                mid[:64],
            )

    def _user_consent_take_output_patch(
        self,
        adapter_service_id: str,
        session_id: str,
        client_ip: str = "",
    ) -> str:
        sid = str(session_id or "").strip()
        ip = str(client_ip or "").strip()
        now = time.monotonic()
        with self._user_consent_lock:
            dead = [
                k
                for k, ent in self._user_consent_output_pending.items()
                if float(ent.get("expires") or 0) <= now
            ]
            for k in dead:
                self._user_consent_output_pending.pop(k, None)
            hit = None
            if sid:
                hit = self._user_consent_output_pending.pop(
                    self._user_consent_key(adapter_service_id, sid), None
                )
            if not isinstance(hit, dict) and ip:
                hit = self._user_consent_output_pending.pop(
                    self._user_consent_ip_key(adapter_service_id, ip), None
                )
            if isinstance(hit, dict):
                return str(hit.get("message_id") or "").strip()
            return ""

    def _user_consent_clear_output_patch(
        self, adapter_service_id: str, session_id: str
    ) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        key = self._user_consent_key(adapter_service_id, sid)
        with self._user_consent_lock:
            self._user_consent_output_pending.pop(key, None)

    def _user_consent_mark_pass(
        self,
        adapter_service_id: str,
        session_id: str,
        block_request_id: str = "",
    ) -> None:
        """After consume: drop BLOCK UI override so the model SSE is not rewritten."""
        sid = str(session_id or "").strip()
        mid = str(block_request_id or "").strip()
        # Only pin the original BLOCK request_id. Session-wide skip would
        # hide the next BLOCK UI in the same chat for the whole TTL.
        until = time.monotonic() + float(user_consent_ttl_sec())
        with self._user_consent_lock:
            if mid:
                self._user_consent_pass_until[f"rid|{mid}"] = until
        self._clear_response_override(sid, mid)
        self._clear_block_output_suppress(adapter_service_id, sid)
        _asid = (adapter_service_id or "").lower().strip()
        if _asid:
            self._response_override_by_session.pop(f"pending:{_asid}", None)
            self._response_override_pending_by_service.pop(_asid, None)
            self._block_suppress_last_by_adapter.pop(_asid, None)

    def _user_consent_skip_override(
        self,
        adapter_service_id: str,
        session_id: str,
        request_id: str = "",
    ) -> bool:
        now = time.monotonic()
        sid = str(session_id or "").strip()
        rid = str(request_id or "").strip()
        candidates = []
        if rid:
            candidates.append(f"rid|{rid}")
        with self._user_consent_lock:
            dead = [k for k, exp in self._user_consent_pass_until.items() if float(exp) <= now]
            for k in dead:
                self._user_consent_pass_until.pop(k, None)
            return any(k in self._user_consent_pass_until for k in candidates)

    def _extract_session_id(
        self,
        req: Dict[str, Any],
        payload: bytes,
        adapter: Any,
    ) -> str:
        if adapter and hasattr(adapter, "extract_session_id"):
            try:
                value = adapter.extract_session_id(req, payload)
                if isinstance(value, str):
                    return value.strip()
            except Exception:
                logging.exception("adapter extract_session_id failed")

        direction = str(req.get("direction") or "").lower()
        unit_type = str(req.get("unit_type") or "").lower()
        content_type = str(req.get("content_type") or "").lower()

        text = safe_utf8(payload)
        try:
            obj = json.loads(text)
        except Exception:
            return ""
        if not isinstance(obj, dict):
            return ""

        if direction == "request" and (unit_type == "json_body" or "application/json" in content_type):
            for key in ("conversation_id", "session_id"):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        if direction == "response" and (unit_type == "sse_event" or "text/event-stream" in content_type):
            value = obj.get("conversation_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
            v = obj.get("v")
            if isinstance(v, dict):
                value = v.get("conversation_id")
                if isinstance(value, str) and value.strip():
                    return value.strip()

        return ""

    @staticmethod
    def _is_response_stream_done(
        req: Dict[str, Any],
        payload: bytes,
        adapter: Any,
    ) -> bool:
        if adapter and hasattr(adapter, "response_stream_done"):
            try:
                return bool(adapter.response_stream_done(req, payload))
            except Exception:
                logging.exception("adapter response_stream_done failed")
        return False

    def _post_output_log_sync(
        self,
        req: Dict[str, Any],
        payload: bytes,
        extracted_text: str,
        adapter_service_id: str,
        adapter: Any,
        request_id: str,
        service_id: str,
        skip_cross_source_dedup: bool = False,
    ) -> None:
        api_url = self._policy_api_url_for_direction("response")
        if not api_url:
            return
        if not (extracted_text or "").strip():
            return
        # Never attach AIVAX BLOCK UI to output_filter / consent patches — the BLOCK
        # input row already holds the notice. Gemini may still stream it after block.
        if looks_like_aivax_policy_block_notice(extracted_text):
            logging.info(
                "request_id=%s service=%s output_skip_aivax_block_notice len=%d",
                request_id,
                service_id,
                len(extracted_text or ""),
            )
            return

        service_code = self._resolve_ai_service_code(adapter_service_id)
        client_host = self._resolve_client_host(req)

        output_req_obj: Optional[Dict[str, Any]] = None
        if adapter and hasattr(adapter, "build_policy_api_payload"):
            try:
                candidate = adapter.build_policy_api_payload(req, payload, extracted_text)
            except Exception:
                logging.exception("adapter build output payload failed service=%s", adapter_service_id)
                candidate = None
            if isinstance(candidate, dict):
                output_req_obj = candidate

        if output_req_obj is not None:
            adapter_sid = (adapter_service_id or "").lower()
            payload_ai_service = int(output_req_obj.get("ai_service") or 0)
            if adapter_sid == "chat_version" and payload_ai_service:
                pass
            elif service_code:
                output_req_obj["ai_service"] = service_code
            output_req_obj["client_host"] = client_host
            # Deep INLINE flush reuses request-direction req → adapter may return
            # input-shaped JSON (prompt/filter_list, no llm_output). Normalize.
            for _k in ("filter_list", "prompt", "encoding", "attachments"):
                output_req_obj.pop(_k, None)

        if output_req_obj is None:
            output_req_obj = {
                "llm_output": extracted_text,
                "user_id": "",
                "email": "",
                "ai_service": service_code,
                "client_host": client_host,
                "session_id": "",
                "message_id": "",
            }

        # output_filter stores modelItem.llm_output → output_text; never leave empty.
        output_req_obj["llm_output"] = extracted_text
        try:
            from adapters.chatgpt_engine import generated_image_attachments

            generated_files = generated_image_attachments(extracted_text)
            if generated_files:
                output_req_obj["generated_files"] = generated_files
                logging.info(
                    "request_id=%s service=%s chatgpt_generated_image_attach count=%d",
                    request_id,
                    service_id,
                    len(generated_files),
                )
        except Exception:
            logging.exception(
                "request_id=%s chatgpt generated image attach failed", request_id
            )

        meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}

        # Unify message_id with request_id so Input/Output Filter match for UI (no store, no lock).
        # ChatGPT high-reasoning resume SSE / Gemini Pro Extended may use a *different*
        # HTTP request_id — prefer remapped parent.
        # Grok WS request tees append "-ws-<hex>"; strip so prompt+response share one id.
        ui_message_id = (
            str(req.get("_chatgpt_ui_message_id") or "").strip()
            or str(req.get("_gemini_ui_message_id") or "").strip()
            or str(req.get("_grok_ui_message_id") or "").strip()
            or str(req.get("_claude_ui_message_id") or "").strip()
            or str(req.get("_timely_ui_message_id") or "").strip()
            or str(req.get("_github_copilot_ui_message_id") or "").strip()
            or str(req.get("_genspark_ui_message_id") or "").strip()
        )
        if not ui_message_id:
            rid = str(request_id or "")
            if "-ws-" in rid:
                ui_message_id = rid.rsplit("-ws-", 1)[0]
        consent_mid = str(
            req.get("_user_consent_output_message_id")
            or (req.get("_engine_cfg") or {}).get("_user_consent_output_message_id")
            or ""
        ).strip()
        if not consent_mid:
            try:
                sid_for_consent = self._extract_session_id(req, payload, adapter)
            except Exception:
                sid_for_consent = ""
            consent_mid = self._user_consent_take_output_patch(
                adapter_service_id,
                sid_for_consent,
                client_host,
            )
        if consent_mid:
            output_req_obj["message_id"] = consent_mid
            output_req_obj["user_consent"] = "on"
            logging.info(
                "request_id=%s service=%s output_message_id_consent parent=%s path=%s",
                request_id,
                service_id,
                consent_mid,
                str(meta.get("path") or ""),
            )
        else:
            output_req_obj["message_id"] = ui_message_id or str(request_id or "")
            if ui_message_id and ui_message_id != str(request_id or ""):
                logging.info(
                    "request_id=%s service=%s output_message_id_remap parent=%s path=%s",
                    request_id,
                    service_id,
                    ui_message_id,
                    str(meta.get("path") or ""),
                )

        try:
            from adapters.chatgpt_engine import register_generated_artifact_links

            generated_artifacts = register_generated_artifact_links(
                extracted_text,
                str(output_req_obj.get("message_id") or ""),
            )
            if generated_artifacts:
                existing_generated = output_req_obj.setdefault("generated_files", [])
                existing_generated.extend(generated_artifacts)
                logging.info(
                    "request_id=%s service=%s chatgpt_generated_artifact_attach count=%d",
                    request_id,
                    service_id,
                    len(generated_artifacts),
                )
        except Exception:
            logging.exception(
                "request_id=%s chatgpt generated artifact registration failed",
                request_id,
            )

        stamp_policy_model(req, payload, output_req_obj, adapter)
        output_req_obj["sensor_instance_id"] = str(
            meta.get("sensor_instance_id") or os.environ.get("AIVAX_SENSOR_INSTANCE_ID") or ""
        )
        output_req_obj["sensor_boot_id"] = str(
            meta.get("sensor_boot_id") or os.environ.get("AIVAX_SENSOR_BOOT_ID") or ""
        )
        output_req_obj["policy_generation"] = str(os.environ.get("AIVAX_POLICY_GENERATION") or "")
        if not skip_cross_source_dedup:
            cross_dedup_key = self._policy_cross_source_dedup_key(
                adapter_service_id,
                "response",
                str(meta.get("host") or ""),
                str(meta.get("path") or ""),
                extracted_text,
                str(output_req_obj.get("message_id") or ""),
            )
            if not self._mark_policy_cross_source_once(cross_dedup_key):
                logging.info(
                    "request_id=%s service=%s output_filter_skip_cross_source_duplicate key=%s",
                    request_id,
                    service_id,
                    cross_dedup_key,
                )
                return

        # Perplexity can emit duplicated final envelopes; avoid duplicated output_filter calls
        # for the same response message.
        message_id = str(output_req_obj.get("message_id") or "").strip()
        revision = bool(req.get("_output_revision"))
        if message_id and not revision:
            previous = self._output_posted.get(message_id)
            if previous and len(extracted_text or "") > int(previous[0]):
                revision = True
                req["_output_revision"] = True
                output_req_obj["logged_at"] = previous[1]
        if revision:
            output_req_obj["output_revision"] = True
        if message_id:
            session_id = str(output_req_obj.get("session_id") or "").strip()
            dedup_key = f"{(adapter_service_id or '').lower()}|{session_id}|{message_id}"
            if revision:
                dedup_key = f"{dedup_key}|rev|{len(extracted_text or '')}"
            if not self._mark_output_log_once(dedup_key):
                logging.info(
                    "request_id=%s service=%s output_filter_skip_duplicate key=%s",
                    request_id,
                    service_id,
                    dedup_key,
                )
                return
            from datetime import datetime, timezone

            now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            previous = self._output_posted.get(message_id)
            if previous is None:
                self._output_posted[message_id] = (len(extracted_text or ""), now_iso)
            elif len(extracted_text or "") > int(previous[0]):
                self._output_posted[message_id] = (len(extracted_text or ""), previous[1])
            while len(self._output_posted) > 4096:
                self._output_posted.pop(next(iter(self._output_posted)), None)

        body = json.dumps(output_req_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        http_req = urllib.request.Request(
            api_url,
            data=body,
            headers=self._policy_http_headers(),
            method="POST",
        )

        raw = b""
        status = 0
        try:
            with urllib.request.urlopen(http_req, timeout=self.policy_api_timeout_ms / 1000.0) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            raw = exc.read() if hasattr(exc, "read") else b""
            logging.warning(
                "request_id=%s service=%s output_filter http_error status=%d body=%s",
                request_id,
                service_id,
                status,
                safe_utf8(raw)[:500],
            )
            return
        except Exception:
            logging.exception("request_id=%s service=%s output_filter call failed", request_id, service_id)
            return

        api_code = 0
        if raw:
            try:
                obj = json.loads(safe_utf8(raw))
                if isinstance(obj, dict):
                    api_code = int(obj.get("api_code") or 0)
            except Exception:
                pass
        logging.info(
            "request_id=%s service=%s output_filter_logged status=%d api_code=%d response=%s",
            request_id,
            service_id,
            status,
            api_code,
            safe_utf8(raw)[:500],
        )

    def _post_generated_file_patch_sync(
        self,
        record: Dict[str, Any],
        client_host: str,
    ) -> None:
        message_id = str(record.get("message_id") or "").strip()
        if not message_id:
            return
        generated = {
            key: record.get(key)
            for key in ("file_name", "file_backup_path", "mime_type", "size")
        }
        body = json.dumps(
            {
                "llm_output": "",
                "generated_files": [generated],
                "generated_only": True,
                "message_id": message_id,
                "client_host": client_host,
                "ai_service": self._resolve_ai_service_code("gpt"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self._policy_api_url_for_direction("response"),
            data=body,
            headers=self._policy_http_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.policy_api_timeout_ms / 1000.0,
            ) as response:
                response.read()
            logging.info(
                "message_id=%s chatgpt_generated_artifact_patch_logged name=%s",
                message_id,
                str(record.get("file_name") or ""),
            )
        except Exception:
            logging.exception(
                "message_id=%s chatgpt generated artifact patch failed",
                message_id,
            )

    def _submit_generated_file_patch_async(
        self,
        record: Dict[str, Any],
        client_host: str,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                asyncio.to_thread(
                    self._post_generated_file_patch_sync,
                    dict(record),
                    client_host,
                )
            )
        except RuntimeError:
            logging.exception("chatgpt generated artifact patch scheduling failed")

    def _drop_gemini_growing_output(self, key: str, reason: str = "") -> None:
        """Cancel a held Gemini/Claude growing snapshot (e.g. after user consent)."""
        mid = str(key or "").strip()
        if not mid:
            return
        task = self._gemini_growing_tasks.pop(mid, None)
        if task is not None and not task.done():
            task.cancel()
        dropped = self._gemini_growing_outputs.pop(mid, None)
        # Allow a later real answer even if the block notice was longer.
        self._gemini_growing_posted.pop(mid, None)
        if dropped is not None:
            logging.info(
                "gemini_growing_output_drop key=%s len=%d reason=%s",
                mid,
                len(str(dropped.get("text") or "")),
                reason or "manual",
            )

    def _defer_gemini_growing_output(
        self,
        req: Dict[str, Any],
        payload: bytes,
        extracted_text: str,
        adapter_service_id: str,
        adapter: Any,
        request_id: str,
        service_id: str,
    ) -> None:
        """Keep the longest processSession snapshot and publish after it settles."""
        text = (extracted_text or "").strip()
        if not text:
            return
        if looks_like_aivax_policy_block_notice(text):
            logging.info(
                "request_id=%s service=%s gemini_growing_output_skip_block_notice len=%d",
                request_id,
                service_id,
                len(text),
            )
            return
        key = str(
            req.get("_gemini_ui_message_id")
            or req.get("_claude_ui_message_id")
            or request_id
            or ""
        ).strip()
        if not key:
            return
        posted = int(self._gemini_growing_posted.get(key) or 0)
        pending = self._gemini_growing_outputs.get(key)
        pending_len = len(pending["text"]) if pending else 0
        if len(text) <= max(posted, pending_len):
            return
        stored_req = dict(req)
        stored_req["_gemini_response_growing"] = False
        stored_req["_output_revision"] = posted > 0
        self._gemini_growing_outputs[key] = {
            "text": text,
            "req": stored_req,
            "payload": payload,
            "adapter_service_id": adapter_service_id,
            "adapter": adapter,
            "request_id": request_id,
            "service_id": service_id,
        }
        while len(self._gemini_growing_outputs) > 1024:
            oldest = next(iter(self._gemini_growing_outputs))
            self._gemini_growing_outputs.pop(oldest, None)
            task = self._gemini_growing_tasks.pop(oldest, None)
            if task and not task.done():
                task.cancel()
        previous = self._gemini_growing_tasks.get(key)
        if previous and not previous.done():
            previous.cancel()
        self._gemini_growing_tasks[key] = asyncio.create_task(
            self._flush_gemini_growing_output(key)
        )
        logging.info(
            "request_id=%s service=%s gemini_growing_output_hold key=%s len=%d revision=%s",
            request_id,
            service_id,
            key,
            len(text),
            stored_req["_output_revision"],
        )

    async def _flush_gemini_growing_output(self, key: str) -> None:
        try:
            await asyncio.sleep(self._gemini_growing_quiet_sec)
        except asyncio.CancelledError:
            return
        item = self._gemini_growing_outputs.pop(key, None)
        self._gemini_growing_tasks.pop(key, None)
        if not item:
            return
        text = str(item["text"] or "")
        self._gemini_growing_posted[key] = len(text)
        while len(self._gemini_growing_posted) > 4096:
            self._gemini_growing_posted.pop(next(iter(self._gemini_growing_posted)), None)
        logging.info(
            "request_id=%s service=%s gemini_growing_output_flush key=%s len=%d revision=%s",
            item["request_id"],
            item["service_id"],
            key,
            len(text),
            bool(item["req"].get("_output_revision")),
        )
        self._submit_output_log_async(
            item["req"],
            item["payload"],
            text,
            item["adapter_service_id"],
            item["adapter"],
            item["request_id"],
            item["service_id"],
        )

    def _defer_codex_held_answer(
        self,
        req: Dict[str, Any],
        payload: bytes,
        text: str,
        message_id: str,
        adapter_service_id: str,
        adapter: Any,
        request_id: str,
        service_id: str,
    ) -> None:
        """Publish a Codex tool-step message if no later answer replaces it."""
        visible = (text or "").strip()
        mid = (message_id or "").strip()
        if not visible or not mid:
            return
        stored_req = dict(req)
        stored_req["_chatgpt_sse_control_only"] = False
        stored_req["_extract_final"] = True
        stored_req["_chatgpt_ui_message_id"] = mid
        stored_req.pop("_codex_held_answer", None)
        stored_req.pop("_codex_held_message_id", None)
        previous = self._codex_held_tasks.get(mid)
        if previous is not None and not previous.done():
            previous.cancel()
        self._codex_held_tasks[mid] = asyncio.create_task(
            self._flush_codex_held_answer(
                mid,
                visible,
                stored_req,
                payload,
                adapter_service_id,
                adapter,
                request_id,
                service_id,
            )
        )
        logging.info(
            "request_id=%s service=%s codex_held_answer_wait message_id=%s len=%d",
            request_id,
            service_id,
            mid,
            len(visible),
        )

    async def _flush_codex_held_answer(
        self,
        message_id: str,
        expected: str,
        req: Dict[str, Any],
        payload: bytes,
        adapter_service_id: str,
        adapter: Any,
        request_id: str,
        service_id: str,
    ) -> None:
        try:
            await asyncio.sleep(self._codex_held_quiet_sec)
        except asyncio.CancelledError:
            return
        self._codex_held_tasks.pop(message_id, None)
        try:
            from adapters.chatgpt_engine import take_codex_held_answer
        except Exception:
            logging.exception(
                "request_id=%s service=%s codex_held_answer_import_failed",
                request_id,
                service_id,
            )
            return
        text = take_codex_held_answer(message_id, expected)
        if not text:
            return
        logging.info(
            "request_id=%s service=%s codex_held_answer_flush message_id=%s len=%d",
            request_id,
            service_id,
            message_id,
            len(text),
        )
        self._submit_output_log_async(
            req,
            payload,
            text,
            adapter_service_id,
            adapter,
            request_id,
            service_id,
        )

    def _cancel_codex_held_answer(self, message_id: str) -> None:
        mid = (message_id or "").strip()
        if not mid:
            return
        task = self._codex_held_tasks.pop(mid, None)
        if task is not None and not task.done():
            task.cancel()

    def _submit_output_log_async(
        self,
        req: Dict[str, Any],
        payload: bytes,
        extracted_text: str,
        adapter_service_id: str,
        adapter: Any,
        request_id: str,
        service_id: str,
        skip_cross_source_dedup: bool = False,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                asyncio.to_thread(
                    self._post_output_log_sync,
                    dict(req),
                    bytes(payload),
                    extracted_text,
                    adapter_service_id,
                    adapter,
                    request_id,
                    service_id,
                    skip_cross_source_dedup,
                )
            )
        except RuntimeError:
            # Fallback for contexts without event loop.
            self._post_output_log_sync(
                dict(req),
                bytes(payload),
                extracted_text,
                adapter_service_id,
                adapter,
                request_id,
                service_id,
                skip_cross_source_dedup,
            )

    def _flush_chatgpt_inline_output_log(
        self,
        adapter_req: Dict[str, Any],
        payload: bytes,
        adapter: Any,
        adapter_service_id: str,
        request_id: str,
        service_id: str,
        output_text: str,
    ) -> None:
        """Deep INLINE fallback: BLOCK (and rare MASK rewrite_failed) still need output_filter
        to complete pipeline merge when no useful model response will be logged.

        PASS/MASKING rely on Envoy response-direction UDS for real model output.
        """
        sid = (adapter_service_id or "").lower()
        if sid not in ("gpt", "chatgpt"):
            return
        meta = adapter_req.get("meta") if isinstance(adapter_req.get("meta"), dict) else {}
        path = str(meta.get("path") or "")
        try:
            from adapters.chatgpt_engine import is_chatgpt_conversation_turn_path

            if not is_chatgpt_conversation_turn_path(path):
                return
        except Exception:
            logging.exception(
                "request_id=%s chatgpt inline output flush path-check failed", request_id
            )
            return
        text = (output_text or "").strip() or "—"
        # Force response-shaped adapter payload (identity/session kept from request meta).
        out_req = dict(adapter_req)
        out_req["direction"] = "response"
        # PASS placeholder is identical across turns; skip cross-source content dedup
        # so each request_id still reaches output_filter / merge.
        logging.info(
            "request_id=%s service=%s inline_output_flush path=%s out_len=%d",
            request_id,
            service_id,
            path,
            len(text),
        )
        self._submit_output_log_async(
            out_req,
            payload,
            text,
            adapter_service_id,
            adapter,
            request_id,
            service_id,
            skip_cross_source_dedup=True,
        )

    def _rewrite_prompt_with_adapter(
        self,
        adapter: Any,
        adapter_req: Dict[str, Any],
        payload: bytes,
        replacement_text: str,
        policy_code: str,
        adapter_service_id: Optional[str] = None,
        source_prompt: str = "",
    ) -> Optional[tuple[bytes, str]]:
        if not adapter:
            return None
        out = None
        code_u = str(policy_code or "").strip().upper()
        is_masking = code_u in ("MASKING", "CSO_NOTICE")
        is_consent = code_u == "CONSENT"
        sid = (adapter_service_id or "").lower()
        # Consent: original prompt only. Gemini restores prior BLOCK-notice slots too.
        if is_consent and hasattr(adapter, "rewrite_prompt_for_consent"):
            out = adapter.rewrite_prompt_for_consent(
                adapter_req, payload, replacement_text, source_prompt
            )
        if out is None and is_consent and sid in (
            "gemini",
            "claude",
            "grok",
            "perplexity",
            "wrks",
            "notion",
            "github_copilot",
        ) and hasattr(adapter, "rewrite_prompt_masking"):
            out = adapter.rewrite_prompt_masking(
                adapter_req, payload, replacement_text, source_prompt
            )
        if out is None and is_consent and hasattr(adapter, "rewrite_prompt"):
            out = adapter.rewrite_prompt(
                adapter_req, payload, replacement_text, policy_code
            )
        # Gemini/Claude MASKING: 프롬프트만 치환. rewrite_prompt 폴백 금지.
        elif is_masking and sid in (
            "gemini",
            "claude",
            "grok",
            "perplexity",
            "wrks",
            "notion",
            "github_copilot",
        ):
            if not hasattr(adapter, "rewrite_prompt_masking"):
                return None
            out = adapter.rewrite_prompt_masking(
                adapter_req, payload, replacement_text, source_prompt
            )
        elif is_masking and hasattr(adapter, "rewrite_prompt_masking"):
            out = adapter.rewrite_prompt_masking(adapter_req, payload, replacement_text)
        elif hasattr(adapter, "rewrite_prompt"):
            out = adapter.rewrite_prompt(adapter_req, payload, replacement_text, policy_code)
        else:
            return None
        if out is None:
            return None
        if not (isinstance(out, tuple) and len(out) == 2 and isinstance(out[1], str)):
            raise ValueError("adapter rewrite_prompt must return (bytes, str) or None")
        rewritten_payload = out[0]
        if not isinstance(rewritten_payload, (bytes, bytearray)):
            raise ValueError("adapter rewrite_prompt payload must be bytes")
        return bytes(rewritten_payload), out[1]

    @staticmethod
    def _rewrite_response_with_adapter(
        adapter: Any,
        adapter_req: Dict[str, Any],
        payload: bytes,
        replacement_text: str,
    ) -> Optional[tuple[bytes, str]]:
        if not adapter or not hasattr(adapter, "rewrite_response"):
            return None

        out = adapter.rewrite_response(adapter_req, payload, replacement_text)
        if out is None:
            return None
        if not (isinstance(out, tuple) and len(out) == 2 and isinstance(out[1], str)):
            raise ValueError("adapter rewrite_response must return (bytes, str) or None")
        rewritten_payload = out[0]
        if not isinstance(rewritten_payload, (bytes, bytearray)):
            raise ValueError("adapter rewrite_response payload must be bytes")
        return bytes(rewritten_payload), out[1]

    @staticmethod
    def _clear_adapter_response_state(adapter: Any, session_id: str) -> None:
        if not session_id:
            return
        if adapter and hasattr(adapter, "clear_response_override_state"):
            try:
                adapter.clear_response_override_state(session_id)
            except Exception:
                logging.exception("adapter clear_response_override_state failed")

    def _clear_response_override_for_masking(
        self,
        adapter_service_id: str,
        session_id: str,
        adapter: Any,
        adapter_req: Optional[Dict[str, Any]] = None,
        payload: Optional[bytes] = None,
    ) -> None:
        """
        MASKING은 프롬프트(요청)만 치환하고 응답 바디는 REWRITE하지 않는다.
        이전 BLOCK 등으로 남은 session/pending 오버라이드가 있으면 이번 응답이 덮어씌워지므로 제거한다.
        extract_session_id가 하나만 잡히면 다른 c_* 키에 오버라이드가 남을 수 있으므로(특히 Gemini)
        어댑터가 제공하면 요청 f.req에서 등장하는 세션 id를 모두 제거한다.
        """
        ids_to_clear: set[str] = set()
        sess = (session_id or "").strip()
        if sess:
            ids_to_clear.add(sess)
        if (
            adapter is not None
            and hasattr(adapter, "collect_session_ids_for_masking_clear")
            and adapter_req is not None
            and payload is not None
        ):
            try:
                for x in adapter.collect_session_ids_for_masking_clear(adapter_req, payload):
                    t = str(x or "").strip()
                    if t:
                        ids_to_clear.add(t)
            except Exception:
                logging.exception(
                    "collect_session_ids_for_masking_clear failed service=%s",
                    adapter_service_id,
                )

        for k in ids_to_clear:
            self._response_override_by_session.pop(k, None)
            self._clear_adapter_response_state(adapter, k)

        sid = (adapter_service_id or "").lower().strip()
        if sid:
            self._response_override_by_session.pop(f"pending:{sid}", None)
            self._response_override_pending_by_service.pop(sid, None)

    async def evaluate(self, req: Dict[str, Any], payload: bytes) -> Dict[str, Any]:
        request_id = str(req.get("request_id") or "")
        service_id = str(req.get("service_id") or "")
        adapter_service_id = self._resolve_adapter_service_id(req, service_id)
        direction = str(req.get("direction") or "")
        unit_type = str(req.get("unit_type") or "")
        meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        _normalize_upload_meta(meta)
        host = str(meta.get("host") or "")
        path = str(meta.get("path") or "")
        adapter_req = req
        adapter_cfg = {
            "chatgpt_force_request_text": self.chatgpt_force_request_text,
        }
        if adapter_cfg:
            adapter_req = dict(req)
            adapter_req["_engine_cfg"] = adapter_cfg

        adapter = self.adapter_registry.get(adapter_service_id)
        adapter_module = getattr(adapter, "__name__", "-") if adapter else "-"
        logging.debug(
            "request_id=%s service=%s adapter_service=%s adapter_loaded=%s adapter_module=%s has_extract=%s",
            request_id,
            service_id,
            adapter_service_id or "-",
            "yes" if adapter else "no",
            adapter_module,
            "yes" if (adapter and hasattr(adapter, "extract")) else "no",
        )

        # File collection meta + upload capture must run even when the service
        # gate would short-circuit chat (block/inspect_off). Mirror DLP still
        # needs multipart/CDN bytes; gate applies to extract/policy below.
        maybe_reload_dyn_policy()
        gate_sid = adapter_service_id or service_id
        gate_resp = _apply_service_gate(request_id, gate_sid, str(direction or ""))

        # File upload handling entry point.
        # 확장자/크기 차단은 pipeline file_block_filter가 담당.
        # policy_signal meta 주입은 유지(미러/로그용)하되 engine 선차단은 하지 않음.
        if _FILE_COLLECTION_META:
            if not isinstance(meta, dict):
                meta = {}
                req["meta"] = meta
            for k, v in _FILE_COLLECTION_META.items():
                meta.setdefault(k, v)

        # Deep INLINE may tee gzip/br bodies; decompress before normalize_upload so
        # ChatGPT init responses (file_id / upload_url) parse correctly.
        # Persist runs on one worker so a large tmpfs write cannot stall the loop.
        ce = str(
            req.get("content_encoding")
            or meta.get("content_encoding")
            or ""
        )
        (
            upload_payload,
            upload_info,
            norm_error,
            upload_result,
        ) = await asyncio.get_running_loop().run_in_executor(
            _UPLOAD_SYNC_EXECUTOR,
            _prepare_upload,
            adapter,
            adapter_req,
            req,
            payload,
            ce,
            gate_resp is None,
        )
        if norm_error is not None:
            logging.error(
                "adapter normalize_upload failed service=%s",
                service_id,
                exc_info=norm_error,
            )

        # 1) Let the adapter enrich upload_id/upload_filename first.
        pending_upload_passthrough: Optional[Dict[str, Any]] = None
        if isinstance(upload_info, dict) and upload_info.get("handled"):
            skipped = bool(upload_info.get("skipped"))
            skip_reason = str(upload_info.get("skip_reason") or "")
            logging.info(
                "request_id=%s service=%s normalize_upload handled id=%s name=%s skipped=%s reason=%s",
                request_id, service_id,
                upload_info.get("upload_id", ""), upload_info.get("upload_filename", ""),
                skipped, skip_reason,
            )
            passthrough = build_allow_response(
                request_id, reason="file_upload_passthrough"
            )
            # Real file capture always wins (even under block).
            # Empty handled (Grok auxiliary REST) defers to the gate
            # so access-control modes still deny non-upload traffic.
            if _upload_info_has_file(upload_info) or gate_resp is None:
                return passthrough
            pending_upload_passthrough = passthrough

        # 2) Run shared upload persistence once metadata is normalized.
        if upload_result is not None and upload_result["handled"]:
            skipped = bool(upload_result.get("skipped"))
            skip_reason = str(upload_result.get("skip_reason") or "")
            logging.info(
                "request_id=%s service=%s file_upload id=%s name=%s path=%s skipped=%s reason=%s",
                request_id, service_id,
                upload_result["upload_id"], upload_result["filename"], upload_result["saved_path"],
                skipped, skip_reason,
            )
            passthrough = build_allow_response(request_id, reason="file_upload_passthrough")
            if (
                upload_result.get("upload_id")
                or upload_result.get("saved_path")
                or upload_result.get("filename")
                or gate_resp is None
            ):
                return passthrough
            pending_upload_passthrough = passthrough

        if gate_resp is not None:
            return gate_resp
        if pending_upload_passthrough is not None:
            return pending_upload_passthrough

        # Deep INLINE may tee still-compressed bodies (esp. Claude H2 with
        # stripped/missing Content-Encoding). Decompress before ChatGate extract
        # for both request and response — mirror eve path already does both.
        payload_for_text = payload
        try:
            ce = str(
                req.get("content_encoding")
                or meta.get("content_encoding")
                or ""
            )
            if len(payload_for_text) >= _SYNC_OFFLOAD_BYTES:
                payload_for_text, _ = await asyncio.to_thread(
                    maybe_content_decompress, payload_for_text, ce
                )
            else:
                payload_for_text, _ = maybe_content_decompress(payload_for_text, ce)
        except Exception:
            logging.debug(
                "request_id=%s content decompress skipped", request_id, exc_info=True
            )
            payload_for_text = payload
        # The download click carries the sandbox name. Its body is only a
        # redirect; the PPT arrives on the following /files/<id>/raw response.
        if (
            direction.lower() == "request"
            and (adapter_service_id or "").lower() in ("gpt", "chatgpt")
        ):
            try:
                from adapters.chatgpt_engine import (
                    is_chatgpt_generated_artifact_path,
                    note_chatgpt_artifact_download,
                )

                if is_chatgpt_generated_artifact_path(path):
                    note_chatgpt_artifact_download(path)
            except Exception:
                logging.exception(
                    "request_id=%s chatgpt generated artifact note failed",
                    request_id,
                )
        # ChatGPT generated files only exist on this session's download. Save
        # them and return before binary bytes are treated as conversation text.
        if direction.lower() == "response" and (adapter_service_id or "").lower() in (
            "gpt",
            "chatgpt",
        ):
            try:
                from adapters.chatgpt_engine import (
                    capture_chatgpt_generated_artifact,
                    capture_chatgpt_generated_image,
                    is_chatgpt_generated_artifact_path,
                    is_chatgpt_generated_file_raw_path,
                    is_chatgpt_generated_image_path,
                )

                if is_chatgpt_generated_image_path(path):
                    capture_chatgpt_generated_image(path, payload_for_text)
                    return build_allow_response(
                        request_id, reason="chatgpt_generated_image"
                    )
                if is_chatgpt_generated_artifact_path(path):
                    artifact = capture_chatgpt_generated_artifact(
                        path,
                        payload_for_text,
                    )
                    if artifact and artifact.get("message_id"):
                        self._submit_generated_file_patch_async(
                            artifact,
                            self._resolve_client_host(req),
                        )
                    if direction.lower() == "response":
                        return build_allow_response(
                            request_id, reason="chatgpt_generated_artifact"
                        )
                if is_chatgpt_generated_file_raw_path(path):
                    artifact = capture_chatgpt_generated_artifact(
                        path,
                        payload_for_text,
                    )
                    if artifact and artifact.get("message_id"):
                        self._submit_generated_file_patch_async(
                            artifact,
                            self._resolve_client_host(req),
                        )
                    return build_allow_response(
                        request_id, reason="chatgpt_generated_file_raw"
                    )
            except Exception:
                logging.exception(
                    "request_id=%s chatgpt generated file capture failed",
                    request_id,
                )
        if adapter and hasattr(adapter, "preprocess"):
            try:
                processed = adapter.preprocess(adapter_req, payload_for_text)
                if isinstance(processed, (bytes, bytearray)):
                    payload_for_text = bytes(processed)
            except Exception:
                logging.exception("adapter preprocess failed service=%s", service_id)

        adapter_sid_norm = (adapter_service_id or "").lower()
        content_type = str(req.get("content_type") or "")
        payload_for_text, normalization = normalize_http_payload(
            payload_for_text,
            unit_type=unit_type,
            content_type=content_type,
        )
        if adapter_req is req:
            adapter_req["_aivax_payload_norm"] = normalization
        else:
            adapter_req["_aivax_payload_norm"] = normalization
            req["_aivax_payload_norm"] = normalization
        if (
            normalization.get("header_stripped")
            or normalization.get("dechunked")
            or normalization.get("stitch_trimmed")
            or normalization.get("suspect")
        ):
            log_fn = logging.warning if normalization.get("suspect") else logging.info
            log_fn(
                "request_id=%s service=%s path=%s payload_normalized "
                "header=%s dechunk=%s stitch=%s suspect=%s reason=%s bytes=%s->%s",
                request_id,
                service_id,
                path,
                bool(normalization.get("header_stripped")),
                bool(normalization.get("dechunked")),
                bool(normalization.get("stitch_trimmed")),
                bool(normalization.get("suspect")),
                normalization.get("reason") or "-",
                normalization.get("original_bytes"),
                normalization.get("normalized_bytes"),
            )
        conversation_path = _is_conversation_path(adapter_sid_norm, path)

        extracted: Optional[str] = None
        # Eve full-body path: Suricata가 스트리밍 LLM 응답 전체 body를 한 번에 전달할 때만 사용.
        # 계정 등록·auth session 등은 _eve_full_body=False 로 extract() 경로(account_lookup) 유지.
        _eve_full = bool(req.get("_eve_full_body"))
        if _eve_full and adapter and hasattr(adapter, "extract_full_body"):
            try:
                extracted = adapter.extract_full_body(adapter_req, payload_for_text)
                if extracted:
                    logging.debug(
                        "request_id=%s service=%s eve_full_body extracted=%d chars",
                        request_id, service_id, len(extracted),
                    )
            except Exception:
                logging.exception("adapter extract_full_body failed service=%s", service_id)
                extracted = None
        elif adapter and hasattr(adapter, "extract"):
            try:
                extracted = adapter.extract(adapter_req, payload_for_text)
            except Exception:
                logging.exception("adapter extract failed service=%s", service_id)
                extracted = None
        if extracted is None:
            # Suricata full-body 스트림 파싱 실패 시 raw SSE/NDJSON fallback 금지 (오탐 방지)
            if _eve_full and adapter and hasattr(adapter, "extract_full_body"):
                extracted = ""
            # Notion: 전체 JSON fallback 금지 → UUID·구조 필드 오탐 MASKING 방지
            elif adapter_sid_norm == "notion" and adapter and hasattr(adapter, "notion_should_skip_policy"):
                try:
                    if adapter.notion_should_skip_policy(adapter_req):
                        extracted = ""
                    else:
                        extracted = ""
                except Exception:
                    extracted = ""
            # Gemini: StreamGenerate가 아닌 batchexecute 등에서 extract()가 None이면
            # form 전체(f.req=...)를 프롬프트로 보내면 안 됨 (새로고침 RPC 오탐).
            elif adapter_sid_norm == "gemini":
                extracted = ""
            else:
                extracted = extract_text_for_policy(adapter_req, payload_for_text)
        elif isinstance(extracted, (bytes, bytearray)):
            extracted = safe_utf8(bytes(extracted))
        else:
            extracted = str(extracted)
        extract_final = bool(
            adapter_req.get("_extract_final") or adapter_req.get("_chatgpt_extract_final")
        )
        # Grok HTTP fallback: load-responses response carries the human prompt when
        # WS user-turn was missed. Fire input policy with that text before output.
        if (
            direction.lower() == "response"
            and adapter_sid_norm == "grok"
        ):
            deferred_human = str(
                adapter_req.get("_grok_deferred_human_prompt") or ""
            ).strip()
            if deferred_human:
                syn_req = dict(adapter_req)
                syn_req["direction"] = "request"
                syn_req.pop("_grok_ws_skip", None)
                logging.info(
                    "request_id=%s service=%s grok_deferred_human len=%d path=%s",
                    request_id,
                    service_id,
                    len(deferred_human),
                    path,
                )
                try:
                    await self._call_policy_api(
                        syn_req,
                        payload_for_text,
                        deferred_human,
                        adapter_service_id,
                        adapter,
                    )
                except Exception:
                    logging.exception(
                        "request_id=%s service=%s grok_deferred_human policy failed",
                        request_id,
                        service_id,
                    )
        elif direction.lower() == "response" and adapter_sid_norm == "timely":
            # Subscribe POST has no prompt. The user text arrives on the SSE
            # response as edit_chat_title before the assistant tokens.
            deferred_human = str(
                adapter_req.get("_timely_deferred_human_prompt") or ""
            ).strip()
            if deferred_human:
                syn_req = dict(adapter_req)
                syn_req["direction"] = "request"
                logging.info(
                    "request_id=%s service=%s timely_deferred_human len=%d path=%s",
                    request_id,
                    service_id,
                    len(deferred_human),
                    path,
                )
                try:
                    await self._call_policy_api(
                        syn_req,
                        payload_for_text,
                        deferred_human,
                        adapter_service_id,
                        adapter,
                    )
                except Exception:
                    logging.exception(
                        "request_id=%s service=%s timely_deferred_human policy failed",
                        request_id,
                        service_id,
                    )
        preview = extracted[: self.payload_preview_bytes]
        is_sse_unit = (
            unit_type.lower() == "sse_event"
            or "text/event-stream" in content_type.lower()
        )
        is_ws_unit = unit_type.lower() == "ws_frame"
        if conversation_path and not (extracted or "").strip():
            # _eve_full_body is also set on WS/SSE frames. Those are often
            # control/partial tees — only treat a finished extract as a failure.
            response_body_complete = (not is_sse_unit) and (not is_ws_unit)
            should_report_extract_fail = direction.lower() == "request" or (
                direction.lower() == "response"
                and (
                    response_body_complete
                    or bool(adapter_req.get("_extract_final"))
                    or bool(adapter_req.get("_chatgpt_extract_final"))
                )
            )
            if should_report_extract_fail:
                # processSession opens with init/context/ack/long-poll frames that
                # carry no user text. Those are transport, not failed extracts.
                gemini_transport = (
                    adapter_sid_norm == "gemini"
                    and "/v1/processsession" in str(path or "").lower()
                )
                claude_rpc_transport = (
                    adapter_sid_norm == "claude"
                    and "/claudeai-rpc/" in str(path or "").lower()
                )
                control_only = (
                    gemini_transport
                    or claude_rpc_transport
                    or bool(adapter_req.get("_chatgpt_sse_control_only"))
                    or (
                        direction.lower() == "request"
                        and is_control_only_body(payload_for_text)
                    )
                )
                reason = normalization.get("reason") or (
                    "empty_response"
                    if direction.lower() == "response"
                    else ("no_user_content" if control_only else "empty_prompt")
                )
                # A prepare/control POST on a conversation path carries no user
                # text by design; reporting it as a failure buries real ones.
                log_extract_fail = logging.info if control_only else logging.warning
                # A response body the extractor could not read is the only copy of
                # that answer. Without it on disk the prompt-only log is all that
                # is left and the parser can never be corrected.
                dump_path = ""
                if direction.lower() == "response" and not control_only:
                    dump_path = dump_failed_extract_body(
                        request_id, service_id, payload_for_text or b""
                    )
                log_extract_fail(
                    "request_id=%s service=%s path=%s extract_fail "
                    "direction=%s reason=%s bytes=%d suspect=%s dump=%s",
                    request_id,
                    service_id,
                    path,
                    direction,
                    reason,
                    len(payload_for_text or b""),
                    bool(normalization.get("suspect")),
                    dump_path or "-",
                )

        # Gemini: StreamGenerate 프롬프트 추출 결과를 DEBUG로 남긴다
        # (batchexecute gemini_no_prompt 와 구분; policy API off여도 동일).
        if (
            adapter_sid_norm == "gemini"
            and direction.lower() == "request"
            and "StreamGenerate" in str(path or "")
        ):
            logging.debug(
                "request_id=%s service=%s gemini_prompt extracted_len=%d preview=%r policy_api=%s",
                request_id,
                service_id,
                len(extracted or ""),
                (extracted[: self.payload_preview_bytes] if self.log_payload else ""),
                "on" if self.pipeline_server_url else "off",
            )

        logging.debug(
            "request_id=%s service=%s adapter=%s dir=%s unit=%s host=%s path=%s payload_bytes=%d preview=%r",
            request_id,
            service_id,
            adapter_service_id or "-",
            direction,
            unit_type,
            host,
            path,
            len(payload),
            preview if self.log_payload else "",
        )
        if direction.lower() == "response" and is_sse_unit and extract_final:
            logging.debug(
                "request_id=%s service=%s response_final_extracted=%r",
                request_id,
                service_id,
                extracted if self.log_payload else "",
            )
        if direction.lower() == "response":
            # ChatGPT high-reasoning: HTTP /f/conversation SSE is often only
            # stream_handoff; the model text arrives on /ws/user. That path must
            # not be a *request* conversation allowlist (presence frames), but
            # extracted assistant text still has to hit output_filter.
            chatgpt_ws_output = False
            if adapter_sid_norm in ("gpt", "chatgpt"):
                try:
                    from adapters.chatgpt_engine import is_chatgpt_response_collect_path

                    chatgpt_ws_output = bool(is_chatgpt_response_collect_path(path))
                except Exception:
                    chatgpt_ws_output = "/ws/user" in str(path or "").lower()
            should_log_output = bool((extracted or "").strip()) and (
                conversation_path or chatgpt_ws_output
            )
            notion_ndjson_stream = adapter_sid_norm == "notion"
            # Grok /ws/mgw streams tokens across many tees — only log on extract_final.
            if is_sse_unit or notion_ndjson_stream or is_ws_unit:
                should_log_output = should_log_output and extract_final
            # ChatGPT control SSE (resume_conversation_token / stream_handoff / …)
            # is not model output — never invent "[sse_empty:…]" into the UI.
            if adapter_req.get("_chatgpt_sse_control_only"):
                should_log_output = False
                logging.info(
                    "request_id=%s service=%s response_sse_control_skip types=%s",
                    request_id,
                    service_id,
                    (adapter_req.get("_chatgpt_sse_empty_meta") or {}).get("types"),
                )
                held_text = str(adapter_req.get("_codex_held_answer") or "").strip()
                held_mid = str(adapter_req.get("_codex_held_message_id") or "").strip()
                if held_text and held_mid and adapter_sid_norm in ("gpt", "chatgpt"):
                    self._defer_codex_held_answer(
                        adapter_req,
                        payload_for_text,
                        held_text,
                        held_mid,
                        adapter_service_id,
                        adapter,
                        request_id,
                        service_id,
                    )
            if adapter_req.get("_gemini_thinking_only"):
                should_log_output = False
                logging.info(
                    "request_id=%s service=%s gemini_thinking_only_skip bytes=%d",
                    request_id,
                    service_id,
                    len(payload_for_text or b""),
                )
            if should_log_output and adapter_sid_norm in ("gpt", "chatgpt"):
                self._cancel_codex_held_answer(
                    str(adapter_req.get("_chatgpt_ui_message_id") or request_id or "")
                )
            if should_log_output:
                output_text = extracted
                skip_output_log = False
                adapter_sid = (adapter_service_id or "").lower()
                response_session_id = ""
                override_text = ""
                if adapter_sid in (
                    "claude",
                    "grok",
                    "perplexity",
                    "gpt",
                    "chatgpt",
                    "github_copilot",
                    "gemini",
                    "notion",
                    "wrks",
                    "chat_version",
                    "timely",
                    "genspark",
                ):
                    # Bind pending BLOCK override BEFORE output_filter so blocked
                    # upstream model text is not collected as a normal response.
                    response_session_id = self._bind_pending_response_override(
                        adapter_req,
                        payload_for_text,
                        adapter,
                        adapter_service_id,
                        request_id,
                        service_id,
                    )
                    if (
                        user_consent_enabled()
                        and response_session_id
                        and adapter_sid != "timely"
                    ):
                        # Timely keeps an id-less slot on the client IP. The SSE
                        # session_id never comes back on the consent send.
                        self._user_consent_rebind_ip_to_session(
                            adapter_service_id,
                            self._resolve_client_host(adapter_req),
                            response_session_id,
                        )
                    if self._user_consent_skip_override(
                        adapter_service_id, response_session_id, request_id
                    ):
                        override_text = ""
                    else:
                        override_text = self._lookup_response_override(
                            response_session_id, request_id, adapter_service_id
                        )
                    if not override_text:
                        # Session/adapter suppress is only for the blocked request itself.
                        # A later PASS on the same chat must still collect model output.
                        if self._request_was_blocked(request_id) and not self._user_consent_skip_override(
                            adapter_service_id, response_session_id, request_id
                        ):
                            override_text = self._lookup_block_output_suppress(
                                adapter_service_id, response_session_id
                            )
                    if override_text:
                        output_text = override_text
                        # BLOCK is already recorded by input_filter; never re-log via
                        # output_filter (that creates a second "collection" row).
                        skip_output_log = True
                if not skip_output_log:
                    if "[이미지 생성]" in (output_text or ""):
                        try:
                            from adapters.chatgpt_engine import wait_generated_image_files

                            await wait_generated_image_files(output_text)
                        except Exception:
                            logging.exception(
                                "request_id=%s chatgpt generated image wait failed",
                                request_id,
                            )
                    if (
                        adapter_req.get("_gemini_response_growing")
                        or adapter_req.get("_claude_response_growing")
                    ) and not override_text:
                        self._defer_gemini_growing_output(
                            adapter_req,
                            payload_for_text,
                            output_text,
                            adapter_service_id,
                            adapter,
                            request_id,
                            service_id,
                        )
                    else:
                        self._submit_output_log_async(
                            adapter_req,
                            payload_for_text,
                            output_text,
                            adapter_service_id,
                            adapter,
                            request_id,
                            service_id,
                        )

        policy_decision: Optional[tuple[str, str, str]] = None
        if direction.lower() == "request":
            skip_policy = False
            if adapter_sid_norm == "notion" and adapter and hasattr(adapter, "notion_should_skip_policy"):
                try:
                    skip_policy = bool(adapter.notion_should_skip_policy(adapter_req))
                except Exception:
                    skip_policy = False
            elif adapter_sid_norm == "grok" and adapter and hasattr(adapter, "grok_should_skip_policy"):
                try:
                    skip_policy = bool(adapter.grok_should_skip_policy(adapter_req, payload))
                except Exception:
                    skip_policy = False
            elif adapter_sid_norm == "perplexity" and adapter and hasattr(
                adapter, "perplexity_should_skip_policy"
            ):
                try:
                    skip_policy = bool(adapter.perplexity_should_skip_policy(adapter_req, payload))
                except Exception:
                    skip_policy = False
            elif adapter_sid_norm == "claude" and adapter and hasattr(adapter, "claude_should_skip_policy"):
                try:
                    skip_policy = bool(adapter.claude_should_skip_policy(adapter_req, payload))
                except Exception:
                    skip_policy = False
            elif adapter_sid_norm == "wrks" and adapter and hasattr(adapter, "wrks_should_skip_policy"):
                try:
                    skip_policy = bool(adapter.wrks_should_skip_policy(adapter_req, payload))
                except Exception:
                    skip_policy = False
            elif adapter_sid_norm == "timely" and adapter and hasattr(adapter, "timely_should_skip_policy"):
                try:
                    skip_policy = bool(adapter.timely_should_skip_policy(adapter_req, payload))
                except Exception:
                    skip_policy = False
            elif adapter_sid_norm == "genspark" and adapter and hasattr(
                adapter, "genspark_should_skip_policy"
            ):
                try:
                    skip_policy = bool(adapter.genspark_should_skip_policy(adapter_req, payload))
                except Exception:
                    skip_policy = False
            elif adapter_sid_norm in ("gpt", "chatgpt") and adapter and hasattr(
                adapter, "chatgpt_should_skip_policy"
            ):
                try:
                    skip_policy = bool(
                        adapter.chatgpt_should_skip_policy(adapter_req, payload_for_text)
                    )
                except Exception:
                    skip_policy = False
            elif adapter_sid_norm == "github_copilot" and adapter and hasattr(
                adapter, "github_copilot_should_skip_policy"
            ):
                try:
                    skip_policy = bool(adapter.github_copilot_should_skip_policy(adapter_req, payload))
                except Exception:
                    skip_policy = False
            # Gemini batchexecute/refresh RPCs: extract() returns None → empty prompt.
            # Do not send raw f.req=... form body to the policy API.
            # StreamGenerate with attachments-only still needs file_block_filter.
            elif adapter_sid_norm == "gemini" and not (extracted or "").strip():
                _meta_gate = adapter_req.get("meta") if isinstance(adapter_req.get("meta"), dict) else {}
                _path_gate = str(_meta_gate.get("path") or "")
                if "StreamGenerate" in _path_gate:
                    # Pro Extended continuation HTTP: no new prompt, known c_* parent.
                    cont = False
                    if adapter and hasattr(adapter, "gemini_is_continuation_request"):
                        try:
                            cont = bool(
                                adapter.gemini_is_continuation_request(
                                    adapter_req, payload_for_text
                                )
                            )
                        except Exception:
                            cont = False
                    skip_policy = bool(cont)
                else:
                    skip_policy = True
            if skip_policy:
                if adapter_sid_norm == "notion" and adapter and hasattr(
                    adapter, "clear_response_override_state"
                ):
                    try:
                        sid = self._extract_session_id(
                            adapter_req, payload_for_text, adapter
                        )
                        if sid:
                            adapter.clear_response_override_state(sid)
                    except Exception:
                        logging.exception(
                            "request_id=%s service=%s notion_passthrough_clear_state failed",
                            request_id,
                            service_id,
                        )
                passthrough_reason = (
                    "policy_api:grok_upload_passthrough"
                    if adapter_sid_norm == "grok"
                    else (
                        "policy_api:perplexity_non_prompt"
                        if adapter_sid_norm == "perplexity"
                        else (
                            "policy_api:claude_passthrough"
                            if adapter_sid_norm == "claude"
                            else (
                                "policy_api:chatgpt_non_prompt"
                                if adapter_sid_norm in ("gpt", "chatgpt")
                                else (
                                    "policy_api:github_copilot_passthrough"
                                    if adapter_sid_norm == "github_copilot"
                                    else (
                                        "policy_api:gemini_continuation"
                                        if (
                                            adapter_sid_norm == "gemini"
                                            and "StreamGenerate"
                                            in str(
                                                (
                                                    adapter_req.get("meta")
                                                    if isinstance(adapter_req.get("meta"), dict)
                                                    else {}
                                                ).get("path")
                                                or ""
                                            )
                                        )
                                        else (
                                            "policy_api:gemini_no_prompt"
                                            if adapter_sid_norm == "gemini"
                                            else "policy_api:notion_passthrough"
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
                logging.info(
                    "request_id=%s service=%s passthrough skip_policy=%s reason=%s extracted_len=%d",
                    request_id,
                    service_id,
                    skip_policy,
                    passthrough_reason,
                    len(extracted or ""),
                )
                return build_allow_response(request_id, reason=passthrough_reason)
            # --- user_consent one-shot bypass ---
            session_for_consent = self._extract_session_id(
                adapter_req, payload_for_text, adapter
            )
            consent_session_ids = self._collect_consent_session_ids(
                adapter, adapter_req, payload_for_text, session_for_consent
            )
            client_ip_for_consent = self._resolve_client_host(adapter_req)
            if user_consent_enabled() and _is_conversation_reload_path(
                adapter_service_id, path
            ):
                for sid in consent_session_ids or [session_for_consent]:
                    self._user_consent_clear_session(adapter_service_id, sid)
            consent_hit = None
            if (
                user_consent_enabled()
                and is_user_consent_phrase(extracted or "")
            ):
                consent_hit = self._user_consent_consume_any(
                    adapter_service_id, consent_session_ids, client_ip_for_consent
                )
                if consent_hit is None and not consent_session_ids:
                    # Empty-session pending (new chat before conversation_id).
                    with self._user_consent_lock:
                        ip_key = self._user_consent_ip_key(
                            adapter_service_id, client_ip_for_consent
                        )
                        hit = self._user_consent_pending.pop(ip_key, None)
                        if isinstance(hit, dict) and float(hit.get("expires") or 0) > time.monotonic():
                            consent_hit = dict(hit)
                if consent_hit is None:
                    logging.info(
                        "request_id=%s service=%s user_consent_miss session=%s aliases=%s extracted=%r",
                        request_id,
                        service_id,
                        (session_for_consent or "")[:64] or "-",
                        [s[:32] for s in consent_session_ids[:6]],
                        (extracted or "")[:32],
                    )
            if consent_hit is None and user_consent_enabled() and not is_user_consent_phrase(
                extracted or ""
            ):
                # Same blocked prompt retry (Gemini often resends StreamGenerate)
                # must keep pending. Any other follow-up invalidates it.
                keep_same_prompt = False
                existing = self._user_consent_lookup_any(
                    adapter_service_id, consent_session_ids, client_ip_for_consent
                )
                if existing and str(existing.get("prompt") or "").strip() == str(
                    extracted or ""
                ).strip():
                    keep_same_prompt = True
                if consent_session_ids and not keep_same_prompt:
                    for sid in consent_session_ids:
                        self._user_consent_clear_session(
                            adapter_service_id, sid
                        )
            if isinstance(consent_hit, dict) and consent_hit.get("cso_blocked"):
                # Grade block is not a consent offer. Put the slot back and
                # show the same notice again. The original prompt stays here.
                cso_again_status = (
                    str(consent_hit.get("cso_status") or "blocked").strip().lower()
                    or "blocked"
                )
                self._user_consent_store(
                    adapter_service_id,
                    str(consent_hit.get("session_id") or session_for_consent or ""),
                    str(consent_hit.get("client_ip") or client_ip_for_consent or ""),
                    str(consent_hit.get("prompt") or ""),
                    str(consent_hit.get("message_id") or ""),
                    cso_blocked=True,
                    cso_status=cso_again_status,
                    cso_message=str(consent_hit.get("cso_message") or ""),
                    block_message=str(consent_hit.get("block_message") or ""),
                )
                self._user_consent_alias_sessions(
                    adapter_service_id,
                    str(consent_hit.get("session_id") or session_for_consent or ""),
                    consent_session_ids,
                )
                cso_again = str(consent_hit.get("cso_message") or "")
                adapter_req["_policy_cso_status"] = cso_again_status
                req["_policy_cso_status"] = cso_again_status
                adapter_req["_policy_cso_message"] = cso_again
                req["_policy_cso_message"] = cso_again
                policy_decision = (
                    "BLOCK",
                    "",
                    str(consent_hit.get("block_message") or ""),
                )
                logging.info(
                    "request_id=%s service=%s user_consent_cso_blocked status=%s session=%s",
                    request_id,
                    service_id,
                    cso_again_status,
                    (session_for_consent or "")[:64] or "-",
                )
            elif isinstance(consent_hit, dict) and str(consent_hit.get("prompt") or "").strip():
                original_prompt = str(consent_hit.get("prompt") or "")
                consent_message_id = str(consent_hit.get("message_id") or "").strip()
                if consent_message_id:
                    for sid in consent_session_ids or [session_for_consent or ""]:
                        self._user_consent_arm_output_patch(
                            adapter_service_id,
                            sid,
                            client_ip_for_consent,
                            consent_message_id,
                        )
                    adapter_req["_user_consent_output_message_id"] = consent_message_id
                    req["_user_consent_output_message_id"] = consent_message_id
                    await self._register_user_consent_input_log(
                        adapter_service_id=adapter_service_id,
                        consent_message_id=consent_message_id,
                        original_prompt=original_prompt,
                        client_ip=client_ip_for_consent,
                        request_id=request_id,
                    )
                replay_text = consent_replay_prompt_text(
                    adapter_service_id, original_prompt
                )
                # Clear leftover BLOCK UI (session + original BLOCK request_id).
                self._user_consent_mark_pass(
                    adapter_service_id,
                    session_for_consent or "",
                    consent_message_id,
                )
                # Drop held Gemini block-notice stream so it cannot consume the
                # consent output patch before the real model answer arrives.
                if consent_message_id:
                    self._drop_gemini_growing_output(
                        consent_message_id, reason="user_consent"
                    )
                if session_for_consent:
                    self._clear_adapter_response_state(adapter, session_for_consent)
                self._clear_response_override(session_for_consent or "", request_id)
                rewritten = None
                try:
                    rewritten = self._rewrite_prompt_with_adapter(
                        adapter,
                        adapter_req,
                        payload_for_text,
                        replay_text,
                        "CONSENT",
                        adapter_service_id,
                        (extracted or "").strip(),
                    )
                except Exception:
                    logging.exception(
                        "request_id=%s service=%s user_consent rewrite failed",
                        request_id,
                        service_id,
                    )
                    rewritten = None
                if rewritten is None:
                    logging.warning(
                        "request_id=%s service=%s user_consent rewrite_failed; allow without rewrite",
                        request_id,
                        service_id,
                    )
                    if consent_message_id:
                        adapter_req["_user_consent_output_message_id"] = consent_message_id
                        req["_user_consent_output_message_id"] = consent_message_id
                    return build_allow_response(
                        request_id, reason="policy_api:user_consent:pass:rewrite_failed"
                    )
                rewritten_payload, rewrite_reason = rewritten
                rewritten_payload = self._restore_consent_attachments(
                    adapter,
                    adapter_req,
                    rewritten_payload,
                    consent_hit,
                    consent_session_ids or [session_for_consent or ""],
                    client_ip_for_consent,
                    replay_text,
                    request_id,
                    service_id,
                )
                if consent_message_id:
                    adapter_req["_user_consent_output_message_id"] = consent_message_id
                    req["_user_consent_output_message_id"] = consent_message_id
                logging.info(
                    "request_id=%s service=%s user_consent_pass session=%s message_id=%s rewrite=%s in_bytes=%d out_bytes=%d",
                    request_id,
                    service_id,
                    (session_for_consent or "")[:64] or "-",
                    (consent_message_id or "")[:64] or "-",
                    rewrite_reason,
                    len(payload_for_text or b""),
                    len(rewritten_payload or b""),
                )
                return build_mask_response(
                    request_id,
                    reason=f"policy_api:user_consent:pass:{rewrite_reason}",
                    payload=rewritten_payload,
                )
            if policy_decision is None:
                try:
                    policy_decision = await self._call_policy_api(
                        adapter_req,
                        payload_for_text,
                        extracted,
                        adapter_service_id,
                        adapter,
                    )
                except PolicyApiError as exc:
                    logging.warning(
                        "request_id=%s service=%s policy_api_error=%s failure_policy=%s",
                        request_id,
                        service_id,
                        str(exc),
                        self.policy_api_failure_policy,
                    )
                    if not self.policy_api_fail_open:
                        return build_block_response(
                            request_id,
                            reason=f"policy_api_fail_close:{str(exc)}",
                            block_message="policy api unavailable",
                        )
        if policy_decision is not None:
            decision_code, masked_contents, block_message = policy_decision
            logging.info(
                "request_id=%s service=%s policy_api_decision=%s masked_len=%d block_len=%d",
                request_id,
                service_id,
                decision_code,
                len(masked_contents.encode("utf-8")) if masked_contents else 0,
                len(block_message.encode("utf-8")) if block_message else 0,
            )
            if decision_code == "PASS":
                session_id = self._extract_session_id(adapter_req, payload_for_text, adapter)
                if user_consent_enabled() and session_id:
                    self._user_consent_clear_session(adapter_service_id, session_id)
                # Duplicate eve re-eval of a BLOCKed request_id is served from cache as
                # BLOCK (not PASS). A real PASS means a new allowed turn — clear stale
                # session suppress so model responses are collected again.
                keep_block_override = self._request_was_blocked(request_id)
                if keep_block_override:
                    logging.info(
                        "request_id=%s service=%s policy_pass_keep_block_override session_id=%s",
                        request_id,
                        service_id,
                        (session_id or "")[:64] or "-",
                    )
                    return build_allow_response(request_id, reason="policy_api:pass:keep_block_override")
                self._clear_block_output_suppress(adapter_service_id, session_id or "")
                if session_id:
                    self._clear_response_override(session_id, request_id)
                    self._clear_adapter_response_state(adapter, session_id)
                else:
                    self._clear_response_override("", request_id)
                # Stale pending:{service} BLOCK override must not rewrite a later PASS turn
                # (MASKING already clears these; PASS previously left them and broke uploads UX).
                _asid = (adapter_service_id or "").lower().strip()
                if _asid:
                    self._response_override_by_session.pop(f"pending:{_asid}", None)
                    self._response_override_pending_by_service.pop(_asid, None)
                cso_status, cso_message = _policy_cso_fields(adapter_req, req)
                if cso_status == "open" and cso_message:
                    prefixed = _cso_open_upstream_text(extracted or "", cso_message)
                    if prefixed and prefixed != str(extracted or "").strip():
                        _sid = (adapter_service_id or "").lower()
                        _unit = str(req.get("unit_type") or "").lower()
                        rewrite_payload = (
                            payload
                            if _sid == "notion"
                            or (_sid == "grok" and _unit == "ws_frame")
                            else payload_for_text
                        )
                        rewritten = None
                        try:
                            rewritten = self._rewrite_prompt_with_adapter(
                                adapter,
                                adapter_req,
                                rewrite_payload,
                                prefixed,
                                "CSO_NOTICE",
                                adapter_service_id,
                                (extracted or "").strip(),
                            )
                        except Exception:
                            logging.exception(
                                "request_id=%s service=%s cso_open_notice rewrite failed",
                                request_id,
                                service_id,
                            )
                            rewritten = None
                        if rewritten is not None:
                            rewritten_payload, rewrite_reason = rewritten
                            rewritten_payload = self._restore_cso_open_attachments(
                                adapter,
                                adapter_req,
                                rewrite_payload,
                                rewritten_payload,
                                prefixed,
                                request_id,
                                service_id,
                            )
                            logging.info(
                                "request_id=%s service=%s cso_open_notice ok reason=%s in_bytes=%d out_bytes=%d",
                                request_id,
                                service_id,
                                rewrite_reason,
                                len(rewrite_payload),
                                len(rewritten_payload),
                            )
                            return build_mask_response(
                                request_id,
                                reason=f"policy_api:cso_open_notice:{rewrite_reason}",
                                payload=rewritten_payload,
                            )
                        logging.warning(
                            "request_id=%s service=%s cso_open_notice rewrite_failed; allow without notice",
                            request_id,
                            service_id,
                        )
                return build_allow_response(request_id, reason="policy_api:pass")

            if decision_code == "MASKING":
                session_id = self._extract_session_id(adapter_req, payload_for_text, adapter)
                # 응답 쪽 REWRITE·출력로그용 오버라이드 제거(프롬프트만 masked_contents로 치환).
                self._clear_response_override_for_masking(
                    adapter_service_id, session_id, adapter, adapter_req, payload_for_text
                )
                # Gemini 등: MASKING은 사용자 프롬프트만 masked_contents로 교체(BLOCK용 system instruction 없음).
                try:
                    # Notion: preprocess로 풀린 payload가 아닌 원본(raw)을 넘겨 gzip 재압축을 유지한다.
                    # Grok ws_frame: 원본 프레임 바이트가 있어야 rewrite 후 WS rewrap 가능.
                    _sid = (adapter_service_id or "").lower()
                    _unit = str(req.get("unit_type") or "").lower()
                    rewrite_payload = (
                        payload
                        if _sid == "notion" or (_sid == "grok" and _unit == "ws_frame")
                        else payload_for_text
                    )
                    rewritten = self._rewrite_prompt_with_adapter(
                        adapter,
                        adapter_req,
                        rewrite_payload,
                        masked_contents,
                        decision_code,
                        adapter_service_id,
                        (extracted or "").strip(),
                    )
                except Exception:
                    logging.exception("policy request rewrite failed service=%s", service_id)
                    rewritten = None
                if rewritten is None:
                    self._flush_chatgpt_inline_output_log(
                        adapter_req,
                        payload_for_text,
                        adapter,
                        adapter_service_id,
                        request_id,
                        service_id,
                        (masked_contents or "").strip() or "—",
                    )
                    return build_block_response(request_id, "policy_api:masking:rewrite_failed")
                rewritten_payload, rewrite_reason = rewritten
                if (adapter_service_id or "").lower() in (
                    "gemini",
                    "claude",
                    "grok",
                    "perplexity",
                    "wrks",
                    "notion",
                    "github_copilot",
                ):
                    logging.info(
                        "request_id=%s service=%s mask_rewrite ok reason=%s in_bytes=%d out_bytes=%d",
                        request_id,
                        service_id,
                        rewrite_reason,
                        len(rewrite_payload),
                        len(rewritten_payload),
                    )
                # Deep INLINE: real model output arrives via Envoy response-direction UDS.
                return build_mask_response(
                    request_id,
                    reason=f"policy_api:masking:{rewrite_reason}",
                    payload=rewritten_payload,
                )

            if decision_code == "BLOCK":
                session_id = self._extract_session_id(adapter_req, payload_for_text, adapter)
                consent_ids = self._collect_consent_session_ids(
                    adapter, adapter_req, payload_for_text, session_id or ""
                )
                # chat_id (Timely) and other collected ids are real chat keys.
                # An empty extract_session_id must not fall through to the IP slot.
                bind_session = self._consent_bind_session(session_id, consent_ids)
                cso_status, cso_message = _policy_cso_fields(adapter_req, req)
                cso_blocked = _cso_refuses_consent(cso_status)
                analysis_ms = _policy_analysis_ms(adapter_req, req)
                consent_on = (
                    user_consent_enabled()
                    and not cso_blocked
                    and not is_user_consent_phrase(extracted or "")
                )
                if (adapter_service_id or "").lower() in (
                    "gemini",
                    "gpt",
                    "chatgpt",
                    "claude",
                    "grok",
                    "perplexity",
                ):
                    detect_filter = _policy_detect_filter(adapter_req, req)
                    if cso_blocked:
                        override_text = _cso_blocked_visible_response(
                            block_message, cso_message
                        )
                    else:
                        override_text = _gpt_gemini_visible_response(
                            block_message,
                            consent_hint=consent_on,
                            analysis_ms=analysis_ms,
                            detect_filter=detect_filter,
                            cso_message=cso_message if cso_status == "open" else "",
                        )
                elif cso_blocked:
                    override_text = _cso_blocked_visible_response(
                        block_message, cso_message
                    )
                else:
                    override_text = _block_display_text(
                        masked_contents,
                        block_message,
                        consent_hint=consent_on,
                        cso_message=cso_message if cso_status == "open" else "",
                    )
                if cso_blocked and user_consent_enabled() and not is_user_consent_phrase(
                    extracted or ""
                ):
                    self._user_consent_store(
                        adapter_service_id,
                        bind_session,
                        self._resolve_client_host(adapter_req),
                        extracted or "",
                        str(
                            adapter_req.get("_policy_message_id")
                            or req.get("_policy_message_id")
                            or request_id
                            or ""
                        ),
                        cso_blocked=True,
                        cso_status=cso_status,
                        cso_message=cso_message,
                        block_message=block_message,
                    )
                    self._user_consent_alias_sessions(
                        adapter_service_id,
                        bind_session,
                        consent_ids,
                    )
                    logging.info(
                        "request_id=%s service=%s cso_block_no_consent status=%s session=%s",
                        request_id,
                        service_id,
                        cso_status or "-",
                        (session_id or "")[:64] or "-",
                    )
                elif consent_on:
                    consent_files = None
                    if adapter and hasattr(adapter, "consent_attachment_state"):
                        try:
                            consent_files = adapter.consent_attachment_state(
                                adapter_req, payload_for_text
                            )
                        except Exception:
                            logging.exception(
                                "request_id=%s service=%s consent_attachment_state failed",
                                request_id,
                                service_id,
                            )
                    self._user_consent_store(
                        adapter_service_id,
                        bind_session,
                        self._resolve_client_host(adapter_req),
                        extracted or "",
                        str(
                            adapter_req.get("_policy_message_id")
                            or req.get("_policy_message_id")
                            or request_id
                            or ""
                        ),
                        consent_files,
                    )
                    if consent_files:
                        logging.info(
                            "request_id=%s service=%s user_consent_store_attachments session=%s",
                            request_id,
                            service_id,
                            (bind_session or "")[:64] or "-",
                        )
                    self._user_consent_alias_sessions(
                        adapter_service_id,
                        bind_session,
                        consent_ids,
                    )
                self._mark_block_output_suppress(adapter_service_id, session_id, override_text)

                if (adapter_service_id or "").lower() == "claude":
                    # Claude는 지시문을 프롬프트 주입으로 거절한다. 보이는 안내문만 넣고 overlay한다.
                    if session_id:
                        self._clear_adapter_response_state(adapter, session_id)
                        self._set_response_override(session_id, request_id, override_text)
                    else:
                        logging.warning(
                            "request_id=%s service=%s response_override_set_deferred session_id_missing adapter_service=%s",
                            request_id,
                            service_id,
                            adapter_service_id,
                        )
                        self._queue_pending_response_override(adapter_service_id, override_text)
                    rewrite_text = (
                        (override_text or "").strip()
                        or _compose_block_rewrite_text(masked_contents, block_message or "")
                        or "blocked by python ai policy engine"
                    )
                    rewritten = None
                    if adapter and hasattr(adapter, "rewrite_prompt_for_block"):
                        try:
                            rewritten = adapter.rewrite_prompt_for_block(
                                adapter_req,
                                payload,
                                rewrite_text,
                                "",
                            )
                        except Exception:
                            logging.exception(
                                "claude block prompt_for_block failed service=%s", service_id,
                            )
                            rewritten = None
                    if rewritten is None:
                        try:
                            rewritten = self._rewrite_prompt_with_adapter(
                                adapter,
                                adapter_req,
                                payload,
                                rewrite_text,
                                "BLOCK",
                                adapter_service_id,
                            )
                        except Exception:
                            logging.exception(
                                "claude block prompt rewrite failed service=%s", service_id,
                            )
                            rewritten = None
                    if rewritten is not None:
                        rewritten_payload, rewrite_reason = rewritten
                        return build_mask_response(
                            request_id,
                            reason=f"policy_api:block:{rewrite_reason}",
                            payload=rewritten_payload,
                        )
                    if session_id:
                        return build_allow_response(
                            request_id, reason="policy_api:block:response_override",
                        )
                    return build_allow_response(
                        request_id, reason="policy_api:block:response_override_pending",
                    )

                if (adapter_service_id or "").lower() in ("gpt", "chatgpt"):
                    # Consent lab: return hard BLOCK to Envoy (Consent offer + consume-before-send).
                    # Default product path remains MASK/sysinstruct below.
                    # Deep INLINE: response UDS missing → flush block text as output_filter for merge.
                    self._flush_chatgpt_inline_output_log(
                        adapter_req,
                        payload_for_text,
                        adapter,
                        adapter_service_id,
                        request_id,
                        service_id,
                        (override_text or "").strip() or "blocked",
                    )
                    if str(os.environ.get("AIVAX_HARD_BLOCK_ENVOY", "")).strip().lower() in (
                        "1",
                        "true",
                        "yes",
                        "on",
                    ):
                        logging.info(
                            "request_id=%s service=%s hard_block_envoy consent_path override_len=%d",
                            request_id,
                            service_id,
                            len(override_text or ""),
                        )
                        return build_block_response(
                            request_id,
                            reason="policy_api:block:hard_envoy_consent",
                            block_message=override_text or "blocked for consent",
                        )
                    if session_id:
                        self._clear_adapter_response_state(adapter, session_id)
                        self._set_response_override(session_id, request_id, override_text)
                        logging.info(
                            "request_id=%s service=%s response_override_set session_id=%s",
                            request_id,
                            service_id,
                            session_id,
                        )
                    else:
                        logging.warning(
                            "request_id=%s service=%s response_override_set_deferred session_id_missing adapter_service=%s",
                            request_id,
                            service_id,
                            adapter_service_id,
                        )
                        self._queue_pending_response_override(adapter_service_id, override_text)

                    if self.chatgpt_block_sysinstruct:
                        if cso_blocked:
                            rewrite_text = override_text
                            block_instruction = _instruction_for(block_message, cso=True)
                        else:
                            rewrite_text = _block_model_input_prompt(
                                masked_contents,
                                block_message,
                                consent_hint=consent_on,
                                analysis_ms=analysis_ms,
                                detect_filter=detect_filter,
                                cso_message=cso_message if cso_status == "open" else "",
                            )
                            block_instruction = _instruction_for(
                                block_message, consent=consent_on
                            )
                        rewritten = None
                        if adapter and hasattr(adapter, "rewrite_prompt_for_block"):
                            try:
                                rewritten = adapter.rewrite_prompt_for_block(
                                    adapter_req,
                                    payload,
                                    rewrite_text,
                                    block_instruction,
                                )
                            except Exception:
                                logging.exception(
                                    "chatgpt block sysinstruct rewrite failed service=%s", service_id,
                                )
                                rewritten = None
                        if rewritten is None:
                            try:
                                rewritten = self._rewrite_prompt_with_adapter(
                                    adapter,
                                    adapter_req,
                                    payload,
                                    rewrite_text,
                                    "BLOCK",
                                    adapter_service_id,
                                )
                            except Exception:
                                logging.exception(
                                    "chatgpt block prompt rewrite failed service=%s", service_id,
                                )
                                rewritten = None
                        if rewritten is not None:
                            rewritten_payload, rewrite_reason = rewritten
                            original = (extracted or "").strip()
                            if original and original.encode("utf-8") in rewritten_payload:
                                logging.warning(
                                    "request_id=%s service=%s chatgpt_block_rewrite_left_original hard_block",
                                    request_id,
                                    service_id,
                                )
                                return build_block_response(
                                    request_id,
                                    reason="policy_api:block:hard_envoy:original_remain",
                                    block_message=override_text or "blocked by python ai policy engine",
                                )
                            return build_mask_response(
                                request_id,
                                reason=f"policy_api:block:{rewrite_reason}",
                                payload=rewritten_payload,
                            )
                    elif masked_contents:
                        rewrite_text = masked_contents
                        try:
                            rewritten = self._rewrite_prompt_with_adapter(
                                adapter,
                                adapter_req,
                                payload,
                                rewrite_text,
                                "BLOCK",
                                adapter_service_id,
                            )
                        except Exception:
                            logging.exception("policy block request rewrite failed service=%s", service_id)
                            rewritten = None
                        if rewritten is not None:
                            rewritten_payload, rewrite_reason = rewritten
                            original = (extracted or "").strip()
                            if original and original.encode("utf-8") in rewritten_payload:
                                logging.warning(
                                    "request_id=%s service=%s chatgpt_block_rewrite_left_original hard_block",
                                    request_id,
                                    service_id,
                                )
                                return build_block_response(
                                    request_id,
                                    reason="policy_api:block:hard_envoy:original_remain",
                                    block_message=override_text or "blocked by python ai policy engine",
                                )
                            return build_mask_response(
                                request_id,
                                reason=f"policy_api:block:{rewrite_reason}",
                                payload=rewritten_payload,
                            )

                    # Never ALLOW the original ChatGPT body after BLOCK.
                    # response_override needs a response UDS; Deep INLINE often
                    # lacks it, so ALLOW leaked 127.0.0.1 to the model.
                    logging.warning(
                        "request_id=%s service=%s chatgpt_block_no_rewrite hard_block session=%s",
                        request_id,
                        service_id,
                        (session_id or "")[:64] or "-",
                    )
                    return build_block_response(
                        request_id,
                        reason="policy_api:block:hard_envoy",
                        block_message=override_text or "blocked by python ai policy engine",
                    )

                if (adapter_service_id or "").lower() == "github_copilot":
                    if session_id:
                        self._clear_adapter_response_state(adapter, session_id)
                        self._set_response_override(session_id, request_id, override_text)
                        logging.info(
                            "request_id=%s service=%s github_copilot response_override_set session_id=%s",
                            request_id,
                            service_id,
                            session_id,
                        )
                    else:
                        logging.warning(
                            "request_id=%s service=%s github_copilot response_override_set_deferred session_id_missing",
                            request_id,
                            service_id,
                        )
                        self._queue_pending_response_override(adapter_service_id, override_text)

                    if self.chatgpt_block_sysinstruct:
                        rewrite_text = override_text
                        block_instruction = _instruction_for(
                            block_message,
                            cso=cso_blocked,
                            channel="generic",
                            consent=consent_on,
                        )
                        rewritten = None
                        if adapter and hasattr(adapter, "rewrite_prompt_for_block"):
                            try:
                                rewritten = adapter.rewrite_prompt_for_block(
                                    adapter_req,
                                    payload,
                                    rewrite_text,
                                    block_instruction,
                                )
                            except Exception:
                                logging.exception(
                                    "github_copilot block sysinstruct rewrite failed service=%s", service_id,
                                )
                                rewritten = None
                        if rewritten is None:
                            try:
                                rewritten = self._rewrite_prompt_with_adapter(
                                    adapter,
                                    adapter_req,
                                    payload,
                                    rewrite_text,
                                    "BLOCK",
                                    adapter_service_id,
                                )
                            except Exception:
                                logging.exception(
                                    "github_copilot block prompt rewrite failed service=%s", service_id,
                                )
                                rewritten = None
                        if rewritten is not None:
                            rewritten_payload, rewrite_reason = rewritten
                            return build_mask_response(
                                request_id,
                                reason=f"policy_api:block:{rewrite_reason}",
                                payload=rewritten_payload,
                            )
                    elif masked_contents:
                        rewrite_text = masked_contents
                        try:
                            rewritten = self._rewrite_prompt_with_adapter(
                                adapter,
                                adapter_req,
                                payload,
                                rewrite_text,
                                "BLOCK",
                                adapter_service_id,
                            )
                        except Exception:
                            logging.exception(
                                "github_copilot block request rewrite failed service=%s", service_id,
                            )
                            rewritten = None
                        if rewritten is not None:
                            rewritten_payload, rewrite_reason = rewritten
                            return build_mask_response(
                                request_id,
                                reason=f"policy_api:block:{rewrite_reason}",
                                payload=rewritten_payload,
                            )

                    if session_id:
                        return build_allow_response(request_id, reason="policy_api:block:response_override")
                    return build_allow_response(request_id, reason="policy_api:block:response_override_pending")

                if (adapter_service_id or "").lower() == "gemini":
                    # 어제와 같이 프롬프트에 카드와 지시문만 넣는다. 응답은 덮지 않는다.
                    if session_id:
                        self._clear_adapter_response_state(adapter, session_id)
                        self._clear_response_override(session_id, request_id)
                    self._response_override_pending_by_service.pop("gemini", None)
                    if cso_blocked:
                        rewrite_text = override_text
                        block_instruction = _instruction_for(block_message, cso=True)
                    else:
                        rewrite_text = _gemini_block_visible_notice(
                            masked_contents,
                            block_message,
                            consent_hint=consent_on,
                            analysis_ms=analysis_ms,
                            detect_filter=detect_filter,
                            cso_message=cso_message if cso_status == "open" else "",
                        )
                        block_instruction = _instruction_for(
                            block_message, consent=consent_on
                        )
                    rewritten = None
                    if adapter and hasattr(adapter, "rewrite_prompt_for_block"):
                        try:
                            rewritten = adapter.rewrite_prompt_for_block(
                                adapter_req,
                                payload_for_text,
                                rewrite_text,
                                block_instruction,
                                (extracted or "").strip(),
                            )
                        except Exception:
                            logging.exception(
                                "gemini block sysinstruct rewrite failed service=%s", service_id,
                            )
                            rewritten = None
                    if rewritten is None:
                        try:
                            rewritten = self._rewrite_prompt_with_adapter(
                                adapter,
                                adapter_req,
                                payload_for_text,
                                rewrite_text,
                                "BLOCK",
                                adapter_service_id,
                            )
                        except Exception:
                            logging.exception(
                                "gemini block prompt rewrite failed service=%s", service_id,
                            )
                            rewritten = None
                    if rewritten is not None:
                        rewritten_payload, rewrite_reason = rewritten
                        logging.info(
                            "request_id=%s service=%s gemini_block_request_rewrite ok reason=%s in_bytes=%d out_bytes=%d",
                            request_id,
                            service_id,
                            rewrite_reason,
                            len(payload_for_text),
                            len(rewritten_payload),
                        )
                        return build_mask_response(
                            request_id,
                            reason=f"policy_api:block:{rewrite_reason}",
                            payload=rewritten_payload,
                        )
                    if session_id:
                        self._set_response_override(session_id, request_id, override_text)
                        return build_allow_response(
                            request_id, reason="policy_api:block:response_override",
                        )
                    return build_allow_response(
                        request_id, reason="policy_api:block:response_override_pending",
                    )

                if (adapter_service_id or "").lower() == "perplexity":
                    # Request rewrite + instruction only. Do not overlay the response:
                    # overlay failure would leave the user with no answer.
                    if session_id:
                        self._clear_adapter_response_state(adapter, session_id)
                        self._clear_response_override(session_id, request_id)
                    if cso_blocked:
                        rewrite_text = override_text
                        block_instruction = _instruction_for(block_message, cso=True)
                    else:
                        rewrite_text = _gemini_block_visible_notice(
                            masked_contents,
                            block_message,
                            consent_hint=consent_on,
                            analysis_ms=analysis_ms,
                            detect_filter=detect_filter,
                            cso_message=cso_message if cso_status == "open" else "",
                        )
                        block_instruction = _instruction_for(
                            block_message, channel="perplexity", consent=consent_on
                        )
                    rewritten = None
                    if adapter and hasattr(adapter, "rewrite_prompt_for_block"):
                        try:
                            rewritten = adapter.rewrite_prompt_for_block(
                                adapter_req,
                                payload_for_text,
                                rewrite_text,
                                block_instruction,
                            )
                        except Exception:
                            logging.exception(
                                "perplexity block sysinstruct rewrite failed service=%s",
                                service_id,
                            )
                            rewritten = None
                    if rewritten is None:
                        try:
                            rewritten = self._rewrite_prompt_with_adapter(
                                adapter,
                                adapter_req,
                                payload_for_text,
                                rewrite_text,
                                "BLOCK",
                                adapter_service_id,
                            )
                        except Exception:
                            logging.exception(
                                "perplexity block prompt rewrite failed service=%s",
                                service_id,
                            )
                            rewritten = None
                    if rewritten is not None:
                        rewritten_payload, rewrite_reason = rewritten
                        logging.info(
                            "request_id=%s service=%s perplexity_block_request_rewrite ok reason=%s in_bytes=%d out_bytes=%d",
                            request_id,
                            service_id,
                            rewrite_reason,
                            len(payload_for_text),
                            len(rewritten_payload),
                        )
                        return build_mask_response(
                            request_id,
                            reason=f"policy_api:block:{rewrite_reason}",
                            payload=rewritten_payload,
                        )
                    if session_id:
                        self._set_response_override(session_id, request_id, override_text)
                        return build_allow_response(
                            request_id, reason="policy_api:block:response_override",
                        )
                    logging.warning(
                        "request_id=%s service=%s response_override_set_deferred session_id_missing adapter_service=%s",
                        request_id,
                        service_id,
                        adapter_service_id,
                    )
                    self._queue_pending_response_override(adapter_service_id, override_text)
                    return build_allow_response(
                        request_id, reason="policy_api:block:response_override_pending",
                    )

                if (adapter_service_id or "").lower() in (
                    "grok",
                    "notion",
                    "wrks",
                    "chat_version",
                ):
                    if session_id:
                        self._clear_adapter_response_state(adapter, session_id)
                        self._set_response_override(session_id, request_id, override_text)
                        logging.info(
                            "request_id=%s service=%s response_override_set session_id=%s",
                            request_id,
                            service_id,
                            session_id,
                        )
                    else:
                        logging.warning(
                            "request_id=%s service=%s response_override_set_deferred session_id_missing adapter_service=%s",
                            request_id,
                            service_id,
                            adapter_service_id,
                        )
                        self._queue_pending_response_override(adapter_service_id, override_text)

                    rewrite_text = (
                        (override_text or "").strip()
                        or (masked_contents or "").strip()
                        or "blocked by python ai policy engine"
                    )
                    block_instruction = (
                        ""
                        if (adapter_service_id or "").lower() == "grok"
                        else _instruction_for(
                            block_message,
                            cso=cso_blocked,
                            channel="generic",
                            consent=consent_on,
                        )
                    )
                    notion_rewrite_payload = (
                        payload
                        if (adapter_service_id or "").lower() == "notion"
                        or (
                            (adapter_service_id or "").lower() == "grok"
                            and str(req.get("unit_type") or "").lower() == "ws_frame"
                        )
                        else payload_for_text
                    )
                    rewritten = None
                    if adapter and hasattr(adapter, "rewrite_prompt_for_block"):
                        try:
                            rewritten = adapter.rewrite_prompt_for_block(
                                adapter_req,
                                notion_rewrite_payload,
                                rewrite_text,
                                block_instruction,
                            )
                        except Exception:
                            logging.exception(
                                "grok/notion/wrks block sysinstruct rewrite failed service=%s",
                                service_id,
                            )
                            rewritten = None
                    if rewritten is None:
                        try:
                            rewritten = self._rewrite_prompt_with_adapter(
                                adapter,
                                adapter_req,
                                notion_rewrite_payload,
                                rewrite_text,
                                "BLOCK",
                                adapter_service_id,
                            )
                        except Exception:
                            logging.exception(
                                "grok/notion/wrks block prompt rewrite failed service=%s",
                                service_id,
                            )
                            rewritten = None
                    if rewritten is not None:
                        rewritten_payload, rewrite_reason = rewritten
                        logging.info(
                            "request_id=%s service=%s block_request_rewrite ok reason=%s in_bytes=%d out_bytes=%d",
                            request_id,
                            service_id,
                            rewrite_reason,
                            len(notion_rewrite_payload),
                            len(rewritten_payload),
                        )
                        return build_mask_response(
                            request_id,
                            reason=f"policy_api:block:{rewrite_reason}",
                            payload=rewritten_payload,
                        )
                    logging.warning(
                        "request_id=%s service=%s block_request_rewrite_failed session_id=%s",
                        request_id,
                        service_id,
                        session_id or "-",
                    )
                    if session_id:
                        return build_allow_response(request_id, reason="policy_api:block:response_override")
                    return build_allow_response(request_id, reason="policy_api:block:response_override_pending")

                if session_id:
                    self._clear_adapter_response_state(adapter, session_id)
                    self._set_response_override(session_id, request_id, override_text)
                    logging.info(
                        "request_id=%s service=%s response_override_set session_id=%s",
                        request_id,
                        service_id,
                        session_id,
                    )
                else:
                    logging.warning(
                        "request_id=%s service=%s response_override_set_deferred session_id_missing adapter_service=%s",
                        request_id,
                        service_id,
                        adapter_service_id,
                    )
                    self._queue_pending_response_override(adapter_service_id, override_text)

                if masked_contents:
                    rewrite_text = masked_contents
                    if (adapter_service_id or "").lower() == "claude":
                        rewrite_text = (
                            (override_text or "").strip()
                            or _compose_block_rewrite_text(masked_contents, block_message)
                        )
                    try:
                        rewritten = self._rewrite_prompt_with_adapter(
                            adapter,
                            adapter_req,
                            payload,
                            rewrite_text,
                            "BLOCK",
                            adapter_service_id,
                        )
                    except Exception:
                        logging.exception("policy block request rewrite failed service=%s", service_id)
                        rewritten = None
                    if rewritten is not None:
                        rewritten_payload, rewrite_reason = rewritten
                        return build_mask_response(
                            request_id,
                            reason=f"policy_api:block:{rewrite_reason}",
                            payload=rewritten_payload,
                        )

                if session_id:
                    return build_allow_response(request_id, reason="policy_api:block:response_override")
                return build_allow_response(request_id, reason="policy_api:block:response_override_pending")

        if direction.lower() == "response":
            session_id = self._bind_pending_response_override(
                adapter_req,
                payload,
                adapter,
                adapter_service_id,
                request_id,
                service_id,
            )
            _asid = (adapter_service_id or "").lower().strip()
            pending_session_id = f"pending:{_asid}" if _asid else ""

            should_clear_override = self._is_response_stream_done(adapter_req, payload, adapter)
            if self._user_consent_skip_override(
                adapter_service_id, session_id, request_id
            ):
                override_text = ""
            else:
                override_text = self._lookup_response_override(
                    session_id, request_id, adapter_service_id
                )
            if override_text:
                resolved_session = self._lookup_response_override_session(
                    session_id, request_id
                )
                override_sid = (resolved_session or session_id or "").strip()
                cached_sid = str(adapter_req.get("_gemini_session_id") or "").strip()
                if not override_sid or override_sid.startswith("pending:"):
                    if cached_sid and cached_sid.startswith("c_"):
                        override_sid = cached_sid
                if override_sid and not override_sid.startswith("pending:"):
                    adapter_req["_gemini_override_session_id"] = override_sid

                # Gemini may deliver a still-compressed body. The visible wrb.fr
                # text is in the decompressed copy.
                response_body = payload
                if (adapter_service_id or "").lower() == "gemini" and payload_for_text:
                    response_body = payload_for_text
                skip_rewrite = False
                if adapter and hasattr(adapter, "response_already_shows_block"):
                    try:
                        skip_rewrite = bool(
                            adapter.response_already_shows_block(
                                adapter_req, response_body, override_text
                            )
                        )
                    except Exception:
                        logging.exception(
                            "adapter response_already_shows_block failed service=%s",
                            service_id,
                        )
                        skip_rewrite = False
                if skip_rewrite:
                    logging.info(
                        "request_id=%s service=%s block_response_skip_rewrite session_id=%s",
                        request_id,
                        service_id,
                        override_sid or session_id or "-",
                    )
                    self._clear_response_override(session_id, request_id)
                    if pending_session_id:
                        self._response_override_by_session.pop(pending_session_id, None)
                    return build_allow_response(
                        request_id, reason="policy_api:block:response_already_ok",
                    )

                rewritten_response = None
                try:
                    rewritten_response = self._rewrite_response_with_adapter(
                        adapter,
                        adapter_req,
                        response_body,
                        override_text,
                    )
                except Exception:
                    logging.exception("response override rewrite failed service=%s", service_id)
                    rewritten_response = None
                if rewritten_response is not None:
                    rewritten_payload, rewrite_reason = rewritten_response
                    logging.info(
                        "request_id=%s service=%s gemini_response_rewrite ok reason=%s session_id=%s in_bytes=%d out_bytes=%d",
                        request_id,
                        service_id,
                        rewrite_reason,
                        override_sid or session_id or "-",
                        len(response_body),
                        len(rewritten_payload),
                    )
                    # SSE 스트림 응답의 경우 스트림이 완전히 끝날 때까지 override를 유지해야 한다.
                    # 스트림 도중 override를 제거하면 이후 SSE 이벤트가 원본 AI 응답으로 그대로
                    # 클라이언트에 전달되어 차단 메시지와 실제 응답이 합쳐지는 문제가 발생한다.
                    if should_clear_override:
                        self._clear_response_override(session_id, request_id)
                        if pending_session_id:
                            self._response_override_by_session.pop(pending_session_id, None)
                    return build_rewrite_response(
                        request_id,
                        reason=f"policy_api:response_override:{rewrite_reason}",
                        payload=rewritten_payload,
                    )
                logging.warning(
                    "request_id=%s service=%s gemini_response_rewrite_failed session_id=%s override_len=%d payload_bytes=%d",
                    request_id,
                    service_id,
                    override_sid or session_id or "-",
                    len(override_text),
                    len(response_body),
                )
                if should_clear_override:
                    self._clear_response_override(session_id, request_id)
                    if pending_session_id:
                        self._response_override_by_session.pop(pending_session_id, None)
            if not override_text:
                prefixed_response = self._rewrite_cso_open_response_prefix(
                    adapter,
                    adapter_req,
                    payload,
                    session_id,
                    request_id,
                    adapter_service_id,
                    service_id,
                    should_clear_override,
                )
                if prefixed_response is not None:
                    return prefixed_response
            return build_allow_response(request_id, reason="response_log_only")

        lowered = extracted.lower()
        for kw in self.keywords:
            if kw and kw in lowered:
                return build_block_response(request_id, f"matched keyword: {kw}")

        if adapter and hasattr(adapter, "rewrite_always"):
            try:
                out = adapter.rewrite_always(adapter_req, payload)
                if out is not None:
                    if not (isinstance(out, tuple) and len(out) == 2 and isinstance(out[1], str)):
                        raise ValueError("adapter rewrite_always must return (bytes, str) or None")
                    forced_payload = out[0]
                    if not isinstance(forced_payload, (bytes, bytearray)):
                        raise ValueError("adapter rewrite_always payload must be bytes")
                    logging.info(
                        "request_id=%s service=%s adapter=%s forced_rewrite reason=%s in_bytes=%d out_bytes=%d",
                        request_id,
                        service_id,
                        adapter_service_id or "-",
                        out[1],
                        len(payload),
                        len(forced_payload),
                    )
                    return build_rewrite_response(
                        request_id,
                        reason=out[1],
                        payload=bytes(forced_payload),
                    )
            except Exception:
                logging.exception("adapter rewrite_always failed service=%s", service_id)

        if self.rewrite_from:
            rewritten_payload: Optional[bytes] = None
            rewrite_count = 0
            if adapter and hasattr(adapter, "rewrite"):
                try:
                    out = adapter.rewrite(adapter_req, payload, self.rewrite_from, self.rewrite_to)
                    if out is not None:
                        if isinstance(out, tuple) and len(out) == 2:
                            rewritten_payload, rewrite_count = out
                        else:
                            raise ValueError("adapter rewrite must return (bytes, int) or None")
                except Exception:
                    logging.exception("adapter rewrite failed service=%s", service_id)
                    rewritten_payload = None
                    rewrite_count = 0

            if rewritten_payload is None:
                rewritten_payload, rewrite_count = rewrite_payload(
                    adapter_req, payload, self.rewrite_from, self.rewrite_to
                )
            if rewrite_count > 0:
                logging.info(
                    "request_id=%s rewrite count=%d from=%r to=%r",
                    request_id,
                    rewrite_count,
                    self.rewrite_from,
                    self.rewrite_to,
                )
                return build_rewrite_response(
                    request_id,
                    reason=f"rewrite:{self.rewrite_from}->{self.rewrite_to}",
                    payload=rewritten_payload,
                )

        return build_allow_response(request_id)

_DEFAULT_DYN_POLICY_FILE = "/home1/aivax/data_resource/policy_signal/aivax_policy.json"
_FILE_COLLECTION_META: Dict[str, str] = {}
_SERVICE_GATE: Dict[str, str] = {}  # "gpt" -> inspect_on|inspect_off|block|redirect|expired
_BLOCK_HTML_FILE = "/home1/aivax/data_resource/block.html"
_BLOCK_HTML_BODY = ""
_REDIRECT_URL = ""
_DYN_POLICY_PATH = _DEFAULT_DYN_POLICY_FILE
_DYN_POLICY_MTIME = 0.0

# user_consent (one-shot bypass after BLOCK)
_USER_CONSENT = "off"
_USER_CONSENT_TTL_SEC = 600.0
_USER_CONSENT_ON_VALUES = frozenset({"on", "1", "true", "yes", "enable", "enabled"})
_USER_CONSENT_PHRASES = frozenset({"동의", "동의합니다"})


# policy_signal service name → engine service_id aliases
_SERVICE_GATE_ALIASES = {
    "gpt": "gpt",
    "chatgpt": "gpt",
    "gemini": "gemini",
    "claude": "claude",
    "grok": "grok",
    "perplexity": "perplexity",
    "notion": "notion ai",
    "notion ai": "notion ai",
    "wrks": "works ai",
    "works ai": "works ai",
    "works": "works ai",
    "github_copilot": "github_copilot",
    "timely": "timely",
    "timely ai": "timely",
    "genspark": "genspark",
    "unknown": "unknown",
}

_GATE_INSPECT_ON = "inspect_on"
_GATE_INSPECT_OFF = "inspect_off"
_GATE_BLOCK = "block"
_GATE_REDIRECT = "redirect"
_GATE_EXPIRED = "expired"


def _canonical_service_gate_key(service_id: str) -> str:
    raw = str(service_id or "").strip().lower()
    if not raw:
        return "unknown"
    return _SERVICE_GATE_ALIASES.get(raw, raw)


def _load_block_html(path: str) -> str:
    p = str(path or "").strip() or _BLOCK_HTML_FILE
    try:
        with open(p, encoding="utf-8") as fh:
            body = fh.read()
        if body.strip():
            return body
    except Exception:
        logging.exception("block_html_file read failed: %s", p)
    # Minimal fallback if file missing.
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"/>"
        "<title>Blocked</title></head><body><h1>Access blocked</h1>"
        "<p>This AI service is blocked by policy.</p></body></html>"
    )


def build_service_block_response(
    request_id: str,
    service_key: str = "",
    *,
    reason_prefix: str = "service_block",
) -> Dict[str, Any]:
    """BLOCK with block.html body for Envoy local reply (payload_utf8)."""
    html = _BLOCK_HTML_BODY or _load_block_html(_BLOCK_HTML_FILE)
    reason = f"{reason_prefix}:{service_key or 'unknown'}"
    return {
        "request_id": request_id,
        "action": "BLOCK",
        "reason": reason,
        "block_message": "service blocked by policy",
        "payload_utf8": html,
        "block_response": {
            "status": 403,
            "content_type": "text/html; charset=utf-8",
            "body": html,
        },
    }


def build_service_redirect_response(request_id: str, service_key: str = "") -> Dict[str, Any]:
    """Redirect gate: meta-refresh HTML when redirect_url set; else block page."""
    url = str(_REDIRECT_URL or "").strip()
    if not url:
        logging.warning(
            "service_gate=redirect but redirect_url empty; falling back to block key=%s",
            service_key or "unknown",
        )
        return build_service_block_response(
            request_id, service_key, reason_prefix="service_redirect_fallback_block"
        )
    # Escape for HTML attribute / JS string
    safe_url = (
        url.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    js_url = json.dumps(url, ensure_ascii=False)
    html = (
        "<!doctype html><html><head><meta charset=\"utf-8\"/>"
        f"<meta http-equiv=\"refresh\" content=\"0;url={safe_url}\"/>"
        f"<script>location.replace({js_url});</script>"
        f"<title>Redirect</title></head><body>"
        f"<p>Redirecting to <a href=\"{safe_url}\">{safe_url}</a></p>"
        "</body></html>"
    )
    reason = f"service_redirect:{service_key or 'unknown'}"
    return {
        "request_id": request_id,
        "action": "BLOCK",
        "reason": reason,
        "block_message": "service redirected by policy",
        "payload_utf8": html,
        "block_response": {
            "status": 403,
            "content_type": "text/html; charset=utf-8",
            "body": html,
        },
    }


def service_gate_status(service_id: str) -> str:
    """Return inspect_on|inspect_off|block|redirect|expired for engine service_id."""
    key = _canonical_service_gate_key(service_id)
    status = str(
        _SERVICE_GATE.get(key) or _SERVICE_GATE.get("unknown") or _GATE_INSPECT_ON
    ).strip().lower()
    if status in (
        _GATE_INSPECT_ON,
        _GATE_INSPECT_OFF,
        _GATE_BLOCK,
        _GATE_REDIRECT,
        _GATE_EXPIRED,
    ):
        return status
    if status:
        logging.warning(
            "unknown service_gate value=%s key=%s; treating as inspect_on", status, key
        )
    return _GATE_INSPECT_ON


def _apply_service_gate(
    request_id: str,
    gate_sid: str,
    direction: str,
) -> Optional[Dict[str, Any]]:
    """Apply service gate. Returns a response dict when short-circuiting, else None."""
    try:
        from service_gate_stats import record_service_gate
    except Exception:
        record_service_gate = None  # type: ignore

    gate = service_gate_status(gate_sid)
    gate_key = _canonical_service_gate_key(gate_sid)
    is_response = str(direction or "").lower() == "response"

    def _rec(mode: str) -> None:
        if record_service_gate is not None:
            try:
                record_service_gate(gate_key, mode)
            except Exception:
                logging.exception("record_service_gate failed")

    # inspect_off: skip analysis on request and response
    if gate == _GATE_INSPECT_OFF:
        _rec(_GATE_INSPECT_OFF)
        logging.info(
            "request_id=%s service_gate=inspect_off key=%s direction=%s",
            request_id,
            gate_key,
            direction or "-",
        )
        return build_allow_response(request_id, reason=f"service_inspect_off:{gate_key}")

    # Access-control gates apply to request ChatGate only
    if is_response:
        return None

    if gate == _GATE_BLOCK:
        _rec(_GATE_BLOCK)
        logging.info(
            "request_id=%s service_gate=block key=%s",
            request_id,
            gate_key,
        )
        return build_service_block_response(request_id, gate_key)

    if gate == _GATE_REDIRECT:
        _rec(_GATE_REDIRECT)
        logging.info(
            "request_id=%s service_gate=redirect key=%s url=%s",
            request_id,
            gate_key,
            (_REDIRECT_URL or "-")[:128],
        )
        return build_service_redirect_response(request_id, gate_key)

    if gate == _GATE_EXPIRED:
        _rec(_GATE_EXPIRED)
        logging.info(
            "request_id=%s service_gate=expired key=%s",
            request_id,
            gate_key,
        )
        return build_service_block_response(
            request_id, gate_key, reason_prefix="service_expired"
        )

    # inspect_on — continue to extract / policy API
    _rec(_GATE_INSPECT_ON)
    return None


def maybe_reload_dyn_policy(path: str = "") -> None:
    """Reload aivax_policy.json when mtime changes (policy_signal updates)."""
    global _DYN_POLICY_MTIME, _DYN_POLICY_PATH
    policy_path = str(path or _DYN_POLICY_PATH or _DEFAULT_DYN_POLICY_FILE).strip()
    if not policy_path:
        return
    try:
        mtime = float(os.path.getmtime(policy_path))
    except OSError:
        return
    if mtime == float(_DYN_POLICY_MTIME) and _SERVICE_GATE:
        return
    _DYN_POLICY_MTIME = mtime
    _DYN_POLICY_PATH = policy_path
    load_file_collection_policy(policy_path)



def normalize_user_consent_value(value) -> str:
    raw = str(value or "").strip().lower()
    if raw in _USER_CONSENT_ON_VALUES:
        return "on"
    return "off"


def normalize_user_consent_ttl_sec(value) -> int:
    raw = str(value if value is not None else "").strip()
    if not raw:
        return 600
    try:
        parsed = int(float(raw))
    except (TypeError, ValueError):
        return 600
    if parsed < 10:
        return 10
    if parsed > 3600:
        return 3600
    return parsed


def user_consent_enabled() -> bool:
    return normalize_user_consent_value(_USER_CONSENT) == "on"


def user_consent_ttl_sec() -> int:
    return int(normalize_user_consent_ttl_sec(_USER_CONSENT_TTL_SEC))


def is_user_consent_phrase(text: str) -> bool:
    # "동의합니다." is the same consent. Extra words after the phrase are not.
    value = str(text or "").strip().strip(" \t.。!！?？~…,，").strip()
    return value in _USER_CONSENT_PHRASES


def append_user_consent_hint(notice: str) -> str:
    base = str(notice or "").rstrip()
    if not base:
        base = "blocked by python ai policy engine"
    if _USER_CONSENT_HINT in base:
        return base
    return _insert_before_detect_type(base, _USER_CONSENT_HINT)


def looks_like_aivax_policy_block_notice(text: str) -> bool:
    """True for AIVAX BLOCK UI text Gemini may still stream after a blocked turn.

    That stream must never consume the consent output patch meant for the real
    model answer.
    """
    t = str(text or "")
    if not t:
        return False
    if (
        "### [AIVAX]" in t
        or "[AIVAX] " in t
        or _BLOCK_TITLE in t
        or "AIVAX 정책에 의해" in t
    ):
        return True
    return any(title and title in t for title in _visible_block_titles())


def consent_replay_prompt_text(adapter_service_id: str, original_prompt: str) -> str:
    """Consent turn sends the original prompt only. Do not prefix model instructions."""
    _ = adapter_service_id
    return str(original_prompt or "")


def _is_conversation_reload_path(adapter_service_id: str, path: str) -> bool:
    """True for chat reload/list fetches that must invalidate consent pending."""
    sid = (adapter_service_id or "").lower().strip()
    p = str(path or "").split("?", 1)[0].lower()
    if not p:
        return False
    if sid in ("gpt", "chatgpt"):
        if p.endswith("/backend-api/f/conversation") or "/backend-api/f/conversation/" in p:
            return False
        if "/backend-api/conversation/" in p or p.endswith("/backend-api/conversations"):
            return True
        if "/backend-api/conversation" == p or p.endswith("/backend-api/conversation"):
            return True
        return False
    if sid == "claude":
        if p.endswith("/completion") or "/completion" in p:
            return False
        if "/chat_conversations/" in p or "/chat_conversation/" in p:
            return True
        return False
    if sid == "gemini":
        if "streamgenerate" in p or "bidigeneratecontent" in p:
            return False
        if "listconversations" in p or "getconversation" in p:
            return True
        return False
    if sid == "grok":
        if "/rest/app-chat/conversations" in p and not p.rstrip("/").endswith("conversations"):
            return "/conversations/" in p and not p.endswith("/chat-messages")
        if p.rstrip("/").endswith("/rest/app-chat/conversations"):
            return True
        return False
    if sid == "perplexity":
        if "/rest/sse/perplexity_ask" in p or "/rest/sse/" in p:
            return False
        if "/rest/thread/" in p or p.endswith("/rest/threads"):
            return True
        return False
    if sid in ("notion", "notion ai"):
        if "/getcompletion" in p or "/runinference" in p:
            return False
        if "/loadpagechunk" in p or "/getrecentpagevisits" in p:
            return True
        return False
    if sid in ("wrks", "works ai", "works"):
        if "/chat" in p and ("message" in p or "completion" in p):
            return False
        if "/conversations" in p or "/threads" in p:
            return True
        return False
    if sid == "github_copilot":
        if "/chat/completions" in p:
            return False
        if "/conversations" in p:
            return True
        return False
    return False


def load_file_collection_policy(path: str = "") -> None:
    """Load AIDynPolicyFile: file gates + per-service gate + block.html.

    Used by both Suricata eve injection and Inline UDS evaluate.
    """
    global _FILE_COLLECTION_META, _SERVICE_GATE, _BLOCK_HTML_FILE, _BLOCK_HTML_BODY, _REDIRECT_URL
    global _DYN_POLICY_PATH, _DYN_POLICY_MTIME
    global _USER_CONSENT, _USER_CONSENT_TTL_SEC
    policy_path = str(path or _DEFAULT_DYN_POLICY_FILE).strip()
    if not policy_path:
        return
    try:
        with open(policy_path, encoding="utf-8") as fh:
            obj = json.load(fh)
        _DYN_POLICY_MTIME = float(os.path.getmtime(policy_path))
        _DYN_POLICY_PATH = policy_path
    except Exception:
        logging.exception("file collection policy load failed: %s", policy_path)
        return
    if not isinstance(obj, dict):
        return
    meta: Dict[str, str] = {}
    maxsize = obj.get("file_control_maxsize")
    if maxsize is not None and str(maxsize).strip():
        meta["file_control_maxsize"] = str(maxsize).strip()
    allow_ext = obj.get("file_allow_extension")
    if allow_ext is not None and str(allow_ext).strip():
        meta["file_allow_extension"] = str(allow_ext).strip()
    _FILE_COLLECTION_META = meta
    try:
        import file_upload_manager as _fum

        _fum.set_file_collection_policy_meta(meta)
    except Exception:
        logging.exception("file collection policy push to file_upload_manager failed")

    # Per-AI service access gate
    reserved = {
        "redirect_url",
        "block_html_file",
        "file_allow_extension",
        "file_control_maxsize",
        "summary",
        "desc",
        "ai_service",
        "user_consent",
        "user_consent_ttl_sec",
        "block_ui_mode",
    }
    gates: Dict[str, str] = {}
    for k, v in obj.items():
        key = str(k or "").strip().lower()
        if not key or key in reserved:
            continue
        status = str(v or "").strip().lower()
        if not status:
            continue
        gates[_canonical_service_gate_key(key)] = status
    _SERVICE_GATE = gates

    html_path = str(obj.get("block_html_file") or _BLOCK_HTML_FILE).strip()
    if html_path:
        _BLOCK_HTML_FILE = html_path
    _BLOCK_HTML_BODY = _load_block_html(_BLOCK_HTML_FILE)
    _REDIRECT_URL = str(obj.get("redirect_url") or "").strip()

    _USER_CONSENT = normalize_user_consent_value(obj.get("user_consent"))
    _USER_CONSENT_TTL_SEC = float(normalize_user_consent_ttl_sec(obj.get("user_consent_ttl_sec")))

    logging.info(
        "dyn policy loaded: path=%s maxsize=%s allow_ext=%s services=%s block_html=%s(%d bytes) redirect=%s user_consent=%s ttl=%s",
        policy_path,
        meta.get("file_control_maxsize", "-"),
        meta.get("file_allow_extension", "-"),
        ",".join(f"{k}={v}" for k, v in sorted(_SERVICE_GATE.items())) or "-",
        _BLOCK_HTML_FILE,
        len(_BLOCK_HTML_BODY or ""),
        _REDIRECT_URL or "-",
        _USER_CONSENT,
        int(_USER_CONSENT_TTL_SEC),
    )


# ── host → service_id 매핑 (suricata-test.rules 기준) ──
_HOST_SERVICE_MAP = [
    ("chatgpt.com",       "chatgpt"),
    ("oaiusercontent.com", "chatgpt"),
    ("gemini.google.com", "gemini"),
    ("geminiweb-pa.clients6.google.com", "gemini"),
    ("push.clients6.google.com", "gemini"),
    ("claude.ai",         "claude"),
    ("perplexity.ai",     "perplexity"),
    ("ppl-ai-file-upload", "perplexity"),
    ("grok.com",          "grok"),
    ("xai-enterprise-api-mc-assets-prod", "grok"),
    ("notion.so",         "notion"),
    ("notion.com",        "notion"),
    ("file.notion.com",   "notion"),
    ("prod-files-secure", "notion"),
    ("copilot-proxy.githubusercontent.com", "github_copilot"),
    ("origin-tracker.githubusercontent.com", "github_copilot"),
    ("githubcopilot.com", "github_copilot"),
    ("uploads.github.com", "github_copilot"),
    ("gateway-api.wrks.ai", "wrks"),
    ("wrks.ai",              "wrks"),
    ("timelyai.io",          "timely"),
    ("10.0.50.84",           "chat_version"),
]


def _is_genspark_host(host: str) -> bool:
    raw = (host or "").lower().split(":", 1)[0].strip().rstrip(".")
    return raw == "genspark.ai" or raw.endswith(".genspark.ai")


def _host_to_service_id(host: str, uri: str = "") -> str:
    h = host.lower()
    u = (uri or "").lower()
    if "accounts.google.com" in h and u.split("?", 1)[0].rstrip("/") == "/listaccounts":
        return "gemini"
    if "push.clients6.google.com" in h and "/upload" in u:
        return "gemini"
    try:
        from adapters.gemini_engine import is_google_ai_mode_request

        if is_google_ai_mode_request(h, u):
            return "gemini"
    except Exception:
        pass
    if "storage.googleapis.com" in h and "xai-enterprise-api-mc-assets-prod" in u:
        return "grok"
    if _is_genspark_host(h):
        return "genspark"
    if "api.github.com" in h and (
        "copilot" in u
        or "/copilot_" in u
        or "/user/copilot" in u
        or u.split("?", 1)[0].rstrip("/") in ("/user", "/user/emails")
    ):
        return "github_copilot"
    if "uploads.github.com" in h and "/copilot/chat/attachments" in u:
        return "github_copilot"
    if "github.com" in h and "github-copilot/chat/attachments" in u:
        return "github_copilot"
    if "user-attachments.githubusercontent.com" in h or "private-user-images.githubusercontent.com" in h:
        return "github_copilot"
    for pattern, sid in _HOST_SERVICE_MAP:
        if pattern in h:
            return sid
    return ""


async def read_eve_line(reader: asyncio.StreamReader, pending: bytes = b"") -> tuple[Optional[dict], bytes]:
    """Lua 스크립트(alert-http-filter.lua)가 UDS로 보낸 JSON 한 줄을 읽어 dict로 반환.

    Returns:
        (None, b"")  — 연결 종료(EOF)
        ({}, b"")    — 빈 줄 또는 파싱 실패 (skip)
        (dict, b"")  — 파싱된 Lua JSON
    """
    try:
        if pending:
            if b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending = rest
            else:
                line = pending + await reader.readuntil(b"\n")
                pending = b""
        else:
            line = await reader.readline()
    except (asyncio.IncompleteReadError, ValueError):
        return None, b""
    if not line:
        return None, pending

    def _loads(raw: bytes) -> dict:
        try:
            obj = json.loads(raw)
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            obj = json.loads(text)
        except json.JSONDecodeError:
            text = raw.decode("utf-8", errors="replace")
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                raise
        if not isinstance(obj, dict):
            return {}
        return obj

    try:
        return _loads(line), pending
    except json.JSONDecodeError:
        logging.warning("read_eve_line: JSON parse failed, len=%d", len(line))
        return {}, pending

async def read_frame(reader: asyncio.StreamReader) -> Optional[bytes]:
    try:
        hdr = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        return None
    (length,) = struct.unpack("!I", hdr)
    if length <= 0 or length > MAX_FRAME_SIZE:
        raise ValueError(f"invalid frame length: {length}")
    payload = await reader.readexactly(length)
    return payload


async def write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(struct.pack("!I", len(payload)))
    writer.write(payload)
    await writer.drain()


def _find_header(headers: list, name: str) -> str:
    """헤더 리스트([{"name":..., "value":...}])에서 이름으로 값 찾기.

    Plugin AIVAX_REDACT_HEADERS=1 이면 Cookie/Authorization 등이 "[REDACTED]"로
    오는데, 계정 추출(psid/JWT)에는 쓸 수 없으므로 빈 문자열로 취급한다.
    """
    target = name.lower()
    for h in headers:
        if isinstance(h, dict) and str(h.get("name", "")).lower() == target:
            value = str(h.get("value", "")).strip()
            if value.upper() == "[REDACTED]":
                return ""
            return value
    return ""


def _headers_to_dict(headers: list) -> Dict[str, str]:
    """헤더 리스트 → {lowercase_name: value} dict (어댑터 meta용)."""
    result: Dict[str, str] = {}
    for h in headers:
        if isinstance(h, dict):
            result[str(h.get("name", "")).lower()] = str(h.get("value", ""))
    return result


def _extract_named_cookie_value(cookie_str: str, name: str) -> str:
    if not cookie_str or not name:
        return ""
    m = re.search(r"(?:^|;\s*)" + re.escape(name) + r"=([^;\s]+)", cookie_str, re.IGNORECASE)
    if not m:
        return ""
    return unquote(m.group(1).strip())


def _resolve_eve_client_ip(eve: Dict[str, Any], req_headers: list) -> str:
    # Wire IP first on Mirror TAP. XFF is the Inline decryptor contract and is
    # applied in Envoy (captureClientIpAndStripXff) / PolicyEngine._resolve_client_host.
    direct = str(eve.get("client_ip") or "").strip()
    if direct:
        return direct
    xff = _find_header(req_headers, "x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    for hname in ("true-client-ip", "x-real-ip", "cf-connecting-ip"):
        value = _find_header(req_headers, hname)
        if value:
            return value.split(",")[0].strip()
    return ""


def _resolve_eve_x_userid(req_headers: list, cookie_str: str) -> str:
    for hname in ("x-userid", "x-user"):
        value = _find_header(req_headers, hname)
        if value:
            return value.strip()
    for cname in ("x-userid", "x-user"):
        value = _extract_named_cookie_value(cookie_str, cname)
        if value:
            return value
    return ""


def _resolve_eve_pplx_visitor_id(req_headers: list, cookie_str: str) -> str:
    header_val = _find_header(req_headers, "pplx-visitor-id")
    if header_val:
        return header_val.strip()
    return _extract_named_cookie_value(cookie_str, "pplx.visitor-id")


def _build_eve_meta(
    eve: Dict[str, Any],
    host: str,
    uri: str,
    method: str,
    status: str,
    req_headers: list,
    resp_headers: list,
    *,
    direction: str,
) -> Dict[str, Any]:
    """Suricata eve → SSLproxy meta 필드 정규화 (account_lookup·client_host용)."""
    cookie_str = _find_header(req_headers, "cookie")
    scheme = str(eve.get(":scheme") or eve.get("scheme") or "").strip().lower()
    scheme_source = str(eve.get("url_source") or "").strip().lower()
    if scheme in ("http", "https") and not scheme_source:
        scheme_source = "observed"
    if not scheme_source:
        scheme_source = "unknown"
    url_info = build_absolute_url(
        request_target_raw=str(eve.get("request_target_raw") or uri or ""),
        host=host,
        scheme=scheme,
        scheme_source=scheme_source,
    )
    meta: Dict[str, Any] = {
        "host": host,
        "client_ip": _resolve_eve_client_ip(eve, req_headers),
        "path": uri,
        "method": method,
        "status": status if direction == "response" else "",
        "cookie": cookie_str,
        "authorization": _find_header(req_headers, "authorization"),
        "x_userid": _resolve_eve_x_userid(req_headers, cookie_str),
        "pplx_visitor_id": _resolve_eve_pplx_visitor_id(req_headers, cookie_str),
        "request_target_raw": url_info.request_target_raw,
        "request_url_absolute": url_info.request_url_absolute,
        "url_source": url_info.url_source,
        "url_normalization_status": url_info.url_normalization_status,
        "body_state": str(eve.get("body_state") or "complete"),
        "body_reason": str(eve.get("body_reason") or ""),
        "sensor_boot_id": str(eve.get("sensor_boot_id") or ""),
        "sensor_instance_id": str(eve.get("sensor_instance_id") or ""),
        "export_sequence": eve.get("export_sequence"),
        "schema_version": eve.get("schema_version"),
        "event_time": str(eve.get("timestamp") or eve.get("event_time") or ""),
        "flow_id": eve.get("flow_id"),
        "tx_id": eve.get("tx_id"),
    }
    # response 경로에서도 CDN/signed GET 회수에 request Cookie·UA가 필요함.
    meta["request_headers"] = _headers_to_dict(req_headers)
    if direction == "response":
        meta["response_headers"] = _headers_to_dict(resp_headers)
    if _FILE_COLLECTION_META:
        meta.update(_FILE_COLLECTION_META)
    return meta


def _chatgpt_conversation_turn_path(path_lower: str) -> bool:
    """ChatGPT chat-turn path (H1/H2). Delegates to chatgpt adapter allowlist."""
    try:
        from adapters.chatgpt_engine import is_chatgpt_conversation_turn_path

        return bool(is_chatgpt_conversation_turn_path(path_lower))
    except Exception:
        # Fallback keep-alive if adapter import fails mid-reload.
        cands = (
            "/backend-api/f/conversation",
            "/backend-anon/f/conversation",
            "/backend-api/anon/conversation",
            "/backend-anon/conversation",
            "/unauth-mweb/conversation/updates",
            "/backend-api/codex/responses",
            "/v1/responses",
        )
        for pref in cands:
            i = path_lower.find(pref)
            if i < 0:
                continue
            after = path_lower[i + len(pref) : i + len(pref) + 1]
            if after == "s":
                continue
            if after and after != "?":
                continue
            return True
        return False


def _eve_response_use_full_body(host: str, uri: str, resp_ct: str) -> bool:
    """Suricata 응답에서 extract_full_body()를 쓸 스트리밍 LLM 응답인지 판별.

    계정 등록·auth session·프로필 HTML은 extract() 경로(account_lookup 등)를 유지하기 위해 False.
    """
    h = host.lower()
    path = uri.split("?", 1)[0]
    path_lower = path.lower()
    ct = resp_ct.lower()

    if "gemini.google.com" in h or "bard.google.com" in h:
        if path_lower == "/" or path_lower.startswith("/app"):
            return False
    if "grok.com" in h:
        if path_lower == "/" or path_lower.startswith("/c/"):
            return False
    if "perplexity.ai" in h:
        if path_lower.rstrip("/") == "/api/auth/session":
            return False
    if _is_genspark_host(h):
        if path_lower.rstrip("/") == "/api/user":
            return False
        if path_lower.rstrip("/") == "/api/agent/ask_proxy":
            return True
    if "gateway-api.wrks.ai" in h or "wrks.ai" in h:
        if "/my/info" in path_lower:
            return False
        if path_lower.rstrip("/").endswith("/api/auth/session"):
            return False
    if "claude.ai" in h:
        # Account/profile HTML/JSON — keep extract() for account_lookup.
        if path_lower in ("/", "/new"):
            return False
        if ("/api/" in path_lower or "/edge-api/" in path_lower) and "/completion" not in path_lower:
            return False
        if "/completion" in path_lower:
            return True

    if "event-stream" in ct:
        return True
    if "grok.com" in h and "/rest/app-chat/conversations/" in path_lower:
        return True
    if "grok.com" in h and "/ws/mgw" in path_lower:
        return True
    if ("gemini.google.com" in h or "bard.google.com" in h) and "streamgenerate" in path_lower:
        return True
    if "geminiweb-pa.clients6.google.com" in h and "processsession" in path_lower:
        return True
    try:
        from adapters.gemini_engine import (
            is_google_ai_mode_answer_path,
            is_google_search_host,
        )

        if is_google_search_host(h) and is_google_ai_mode_answer_path(path_lower):
            return True
    except Exception:
        pass
    if ("notion.so" in h or "notion.com" in h) and "runinferencetranscript" in path_lower:
        return True
    if "chatgpt.com" in h and _chatgpt_conversation_turn_path(path_lower):
        return True
    if "chatgpt.com" in h:
        try:
            from adapters.chatgpt_engine import is_chatgpt_response_collect_path

            if is_chatgpt_response_collect_path(path_lower):
                return True
        except Exception:
            pass
    if "gateway-api.wrks.ai" in h and (
        "/message/stream" in path_lower
        or "/ai/chat/stream/" in path_lower
        or "/ai/chat/agent/" in path_lower and "/stream/" in path_lower
    ):
        return True
    if "timelyai.io" in h and (
        "/timely/api/stream" in path_lower
        or "/api-ai/v3/llm-completion" in path_lower
    ):
        return True
    return False


def _decode_eve_request_payload(req_body: str, req_body_b64: str) -> bytes:
    """Prefer request_body_b64 for binary uploads; fall back to text body.

    Mirror emits multipart/octet-stream only in ``request_body_b64``. Preferring
    a possibly-empty or UTF-8-damaged ``request_body`` first used to drop file
    bytes for Grok direct upload.
    """
    b64 = (req_body_b64 or "").strip()
    if b64:
        try:
            return base64.b64decode(b64, validate=False)
        except Exception:
            logging.exception("eve request_body_b64 decode failed")
            return b""
    if req_body:
        return req_body.encode("utf-8")
    return b""


def _decode_eve_response_payload(resp_body: str, resp_body_b64: str) -> bytes:
    """Prefer response_body_b64 (plugin always sends b64); fall back to text body."""
    b64 = (resp_body_b64 or "").strip()
    if b64:
        try:
            return base64.b64decode(b64, validate=False)
        except Exception:
            logging.exception("eve response_body_b64 decode failed")
            return b""
    if resp_body:
        return resp_body.encode("utf-8")
    return b""


def _eve_request_unit_type(host: str, req_ct: str, *, from_b64: bool) -> str:
    h = (host or "").lower()
    if from_b64 or "oaiusercontent.com" in h:
        return "raw_body"
    if "storage.googleapis.com" in h:
        return "raw_body"
    ct = (req_ct or "").lower()
    if "multipart/form-data" in ct:
        return "form_body"
    if "x-www-form-urlencoded" in ct:
        return "form_body"
    if "application/octet-stream" in ct:
        return "raw_body"
    return "json_body"


# 처리 완료 카운터(인라인/풀 공통). 벤치·운영 모니터링에서 실제 drain 속도 측정용.
_EVE_PROCESSED = 0


async def _eve_stats_writer(pool: "Optional[EveConsumerPool]", path: str) -> None:
    """AIVAX_CONSUMER_STATS 로 처리 통계를 주기적으로 기록."""
    while True:
        try:
            data = {
                "ts": time.time(),
                "processed": _EVE_PROCESSED,
                "enqueued": getattr(pool, "enqueued", 0) if pool else 0,
                "drop_queue_full": getattr(pool, "dropped_queue_full", 0) if pool else 0,
                "workers": getattr(pool, "workers", 0) if pool else 0,
            }
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception:
            logging.debug("eve stats writer error", exc_info=True)
        await asyncio.sleep(0.2)


class EveConsumerPool:
    """수신/처리 분리용 샤딩 워커 풀.

    - 수신 루프(`dispatch`)는 절대 블록하지 않는다: 큐가 가득 차면 즉시 drop 카운트.
    - 샤딩 키 = request_id → 같은 트랜잭션의 request/response 가 같은 워커에서
      FIFO 로 처리되어 **순서(override 바인딩)** 가 보존된다.
    - GIL 때문에 순수 CPU 처리량은 워커 수에 비례하지 않지만, 소켓을 빠르게
      비워 버스트 drop 을 없애고, 정책 API(I/O) 대기는 워커 간 겹쳐 처리된다.
    """

    def __init__(self, engine: "PolicyEngine", workers: int, total_queue: int) -> None:
        self.engine = engine
        self.workers = max(1, workers)
        per_shard = max(8, total_queue // self.workers)
        self._queues: list[asyncio.Queue] = [
            asyncio.Queue(maxsize=per_shard) for _ in range(self.workers)
        ]
        self._tasks: list[asyncio.Task] = []
        self.enqueued = 0
        self.dropped_queue_full = 0
        self.processed = 0
        self._per_shard = per_shard

    def start(self) -> None:
        for i in range(self.workers):
            self._tasks.append(asyncio.create_task(self._worker(i)))
        logging.info(
            "eve consumer pool started workers=%d per_shard_queue=%d total=%d",
            self.workers, self._per_shard, self._per_shard * self.workers,
        )

    async def _worker(self, idx: int) -> None:
        q = self._queues[idx]
        while True:
            eve = await q.get()
            if eve is None:
                q.task_done()
                return
            try:
                await _process_eve_event(eve, self.engine)
                self.processed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.exception("eve worker %d processing error: %s", idx, exc)
            finally:
                q.task_done()

    def dispatch(self, eve: Dict[str, Any]) -> bool:
        """수신 루프에서 호출. 절대 await 하지 않음. 넣으면 True, drop 이면 False."""
        rid = eve.get("request_id") or ""
        shard = (hash(rid) & 0x7FFFFFFF) % self.workers
        try:
            self._queues[shard].put_nowait(eve)
            self.enqueued += 1
            return True
        except asyncio.QueueFull:
            self.dropped_queue_full += 1
            return False

    async def drain_and_stop(self) -> None:
        for q in self._queues:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                # 큐가 꽉 찼으면 워커를 취소로 종료
                pass
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=SHUTDOWN_GRACE_SECONDS)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        logging.info(
            "eve consumer pool stopped enqueued=%d processed=%d drop_queue_full=%d",
            self.enqueued, self.processed, self.dropped_queue_full,
        )


async def _process_eve_event(eve: Dict[str, Any], engine: PolicyEngine) -> None:
    """Lua JSON → SSLProxy read_frame 양식과 동일한 req dict로 변환 후 evaluate 2회 호출.

    Lua가 보내는 형식:
      {"host","uri","method","status","request_headers","response_headers","request_body","response_body"}
    SSLProxy가 보내는 형식 (read_frame → handle_client):
      {"request_id","service_id","direction","protocol","content_type","unit_type",
       "meta":{"host","client_ip","path","method","status"},
       "payload":{"raw_b64":"..."}}

    request_body → direction=request 로 1회, response_body → direction=response 로 1회.
    """
    host = str(eve.get("host") or "")
    uri = str(eve.get("uri") or "")
    method = str(eve.get("method") or "")
    status_code = str(eve.get("status") or "")
    req_headers = eve.get("request_headers") or []
    resp_headers = eve.get("response_headers") or []
    req_body = str(eve.get("request_body") or "")
    req_body_b64 = str(eve.get("request_body_b64") or "")
    resp_body = str(eve.get("response_body") or "")
    resp_body_b64 = str(eve.get("response_body_b64") or "")
    deferred_path = str(eve.get("deferred_path") or "").strip()
    if deferred_path and os.path.isfile(deferred_path):
        try:
            with open(deferred_path, "rb") as handle:
                nested = json.loads(handle.read().decode("utf-8", "replace"))
            if isinstance(nested, dict):
                eve = dict(nested)
                eve.setdefault("deferred_path", deferred_path)
                host = str(eve.get("host") or host)
                uri = str(eve.get("uri") or uri)
                method = str(eve.get("method") or method)
                status_code = str(eve.get("status") or status_code)
                req_headers = eve.get("request_headers") or req_headers
                resp_headers = eve.get("response_headers") or resp_headers
                req_body = str(eve.get("request_body") or "")
                req_body_b64 = str(eve.get("request_body_b64") or "")
                resp_body = str(eve.get("response_body") or "")
                resp_body_b64 = str(eve.get("response_body_b64") or "")
        except Exception:
            logging.exception("deferred_path load failed path=%s", deferred_path)
    req_payload_bytes = _decode_eve_request_payload(req_body, req_body_b64)
    req_from_b64 = bool((req_body_b64 or "").strip())
    resp_payload_bytes = _decode_eve_response_payload(resp_body, resp_body_b64)

    service_id = _host_to_service_id(host, uri)
    req_ct = _find_header(req_headers, "content-type")
    resp_ct = _find_header(resp_headers, "content-type")
    req_ce = _find_header(req_headers, "content-encoding")
    resp_ce = _find_header(resp_headers, "content-encoding")
    rid = str(eve.get("request_id") or "")

    if req_payload_bytes:
        req_payload_bytes, _ = maybe_content_decompress(req_payload_bytes, req_ce)
    if resp_payload_bytes:
        resp_payload_bytes, _ = maybe_content_decompress(resp_payload_bytes, resp_ce)

    # SSE \r\n 정규화는 adapter extract 쪽에서 필요 시 수행.
    # 여기서 전체 body를 decode+replace 하면 수 MB 응답에서 순수 낭비가 된다
    # (정규화된 문자열이 evaluate 경로에서 쓰이지 않음).

    http_version = str(eve.get("http_version") or "")
    logging.info(
        "eve event rid=%s service=%s host=%s uri=%s method=%s status=%s http_ver=%s req_body=%d resp_body=%d",
        rid, service_id, host, uri, method, status_code, http_version or "-",
        len(req_payload_bytes), len(resp_payload_bytes),
    )

    async def _eval_eve_request() -> None:
        if not req_payload_bytes:
            return
        payload_bytes = req_payload_bytes
        unit_type = _eve_request_unit_type(host, req_ct, from_b64=req_from_b64)
        # eve 경로는 payload_bytes 를 직접 넘긴다. raw_b64 재인코딩은
        # SSLproxy 프레임 호환 shape 용이며 evaluate/adapter 가 읽지 않으므로 생략.
        req = {
            "request_id": rid,
            "service_id": service_id,
            "direction": "request",
            "protocol": "https",
            "content_type": req_ct,
            "unit_type": unit_type,
            "event_time": iso_event_time(
                eve.get("timestamp") or eve.get("event_time") or eve.get("@timestamp")
            ),
            "meta": _build_eve_meta(
                eve, host, uri, method, status_code, req_headers, resp_headers, direction="request"
            ),
            "payload": {
                "raw_b64": "",
            },
        }
        try:
            result = await engine.evaluate(req, payload_bytes)
            logging.info(
                "eve request result rid=%s action=%s reason=%s",
                rid, result.get("action"), result.get("reason"),
            )
        except Exception:
            logging.exception("eve request evaluate error host=%s rid=%s", host, rid)

    async def _eval_eve_response() -> None:
        if not resp_payload_bytes:
            return
        payload_bytes = resp_payload_bytes
        use_full_body = _eve_response_use_full_body(host, uri, resp_ct)
        req = {
            "request_id": rid,
            "service_id": service_id,
            "direction": "response",
            "protocol": "https",
            "content_type": resp_ct,
            "unit_type": "sse_event" if "event-stream" in resp_ct.lower() else "json_body",
            "meta": _build_eve_meta(
                eve, host, uri, method, status_code, req_headers, resp_headers, direction="response"
            ),
            "payload": {
                "raw_b64": "",
            },
        }
        if use_full_body:
            req["_eve_full_body"] = True
        try:
            result = await engine.evaluate(req, payload_bytes)
            logging.info(
                "eve response result rid=%s action=%s reason=%s",
                rid, result.get("action"), result.get("reason"),
            )
        except Exception:
            logging.exception("eve response evaluate error host=%s rid=%s", host, rid)

    await _eval_eve_request()
    await _eval_eve_response()
    global _EVE_PROCESSED
    _EVE_PROCESSED += 1


async def _process_frame_payload(
    frame: bytes,
    writer: asyncio.StreamWriter,
    engine: PolicyEngine,
    peer: Any,
) -> bool:
    """SSLproxy 프레임 1건 처리. False면 연결 종료."""
    try:
        if len(frame) >= _SYNC_OFFLOAD_BYTES:
            req, payload = await asyncio.to_thread(_decode_policy_frame, frame)
        else:
            req, payload = _decode_policy_frame(frame)
        resp_obj = await engine.evaluate(req, payload)
    except Exception as exc:
        logging.exception("request processing error: %s", exc)
        request_id = ""
        try:
            maybe = json.loads(frame.decode("utf-8", errors="ignore"))
            if isinstance(maybe, dict):
                request_id = str(maybe.get("request_id") or "")
        except Exception:
            pass
        resp_obj = build_allow_response(request_id, reason=f"engine_error:{type(exc).__name__}")

    if len(frame) >= _SYNC_OFFLOAD_BYTES:
        resp = await asyncio.to_thread(_encode_policy_response, resp_obj)
    else:
        resp = _encode_policy_response(resp_obj)
    try:
        await write_frame(writer, resp)
    except (BrokenPipeError, ConnectionResetError) as exc:
        logging.info(
            "connection lost while sending response peer=%r: %s",
            peer,
            exc,
        )
        return False
    return True


async def _handle_frame_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    engine: PolicyEngine,
    peer: Any,
    first_byte: bytes = b"",
) -> None:
    """SSLproxy 4-byte length 프레임 프로토콜 (연결당)."""
    hdr = first_byte
    try:
        while True:
            if len(hdr) < 4:
                hdr += await reader.readexactly(4 - len(hdr))
            (length,) = struct.unpack("!I", hdr[:4])
            if length <= 0 or length > MAX_FRAME_SIZE:
                raise ValueError(f"invalid frame length: {length}")
            frame = await reader.readexactly(length)
            hdr = b""
            if not await _process_frame_payload(frame, writer, engine, peer):
                break
    except asyncio.IncompleteReadError:
        pass
    except asyncio.CancelledError:
        raise


async def _handle_eve_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    engine: PolicyEngine,
    peer: Any,
    first_byte: bytes = b"",
) -> None:
    """Suricata Lua newline JSON 프로토콜 (연결당).

    수신 루프는 라인 파싱 후 워커 풀에 dispatch 만 한다(무거운 처리는 워커).
    풀이 없으면(CONSUMER_WORKERS=0) 기존처럼 인라인 처리한다.
    """
    pool: Optional[EveConsumerPool] = getattr(engine, "_eve_consumer_pool", None)
    pending = first_byte
    while True:
        eve, pending = await read_eve_line(reader, pending)
        if eve is None:
            break
        if not eve:
            continue
        if pool is not None:
            pool.dispatch(eve)
            continue
        try:
            await _process_eve_event(eve, engine)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception("eve event processing error: %s", exc)


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    engine: PolicyEngine,
    is_eve: bool,
) -> None:
    peer = writer.get_extra_info("peername")
    logging.debug("client connected: %r", peer)
    if is_eve and UDS_RCVBUF > 0:
        # 큰 recv 버퍼로 버스트를 흡수 → 플러그인 EAGAIN drop 감소
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDS_RCVBUF)
            except OSError:
                logging.debug("SO_RCVBUF set failed", exc_info=True)
    try:
        if is_eve:
            first = await reader.read(1)
            if not first:
                return
            if first != b"{":
                await _handle_frame_client(reader, writer, engine, peer, first)
            else:
                await _handle_eve_client(reader, writer, engine, peer, first)
            return

        while True:
            frame = await read_frame(reader)
            if frame is None:
                break
            if not await _process_frame_payload(frame, writer, engine, peer):
                break
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        logging.debug("client disconnected: %r", peer)


async def run_server(args: argparse.Namespace) -> None:
    # 1. 이전 소켓 파일 정리: 기존에 남은 소켓 파일이 있으면 삭제하여 충돌을 방지합니다.
    socket_paths: list[str] = [args.socket]
    extras = getattr(args, "extra_socket", None) or []
    if isinstance(extras, str):
        extras = [extras] if extras.strip() else []
    for extra in extras:
        e = str(extra or "").strip()
        if e and e not in socket_paths:
            socket_paths.append(e)

    for sock_path in socket_paths:
        if os.path.exists(sock_path):
            os.unlink(sock_path)

    # 2. 계정 조회 캐시 초기화 (sharded LRU + TTL + confidence)
    configure_account_lookup(
        maxsize=getattr(args, "account_cache_maxsize", 10_000),
        ttl_seconds=getattr(args, "account_cache_ttl", 3600),
        shards=int(os.environ.get("AIVAX_ACCOUNT_SHARDS", "64")),
    )
    # Ensure metadata WAL directory exists (durable_metadata events)
    try:
        get_meta_wal()
    except Exception:
        logging.getLogger(__name__).exception("meta WAL init failed")

    # 3. 정책 엔진 초기화: 실제 데이터 필터링/수정 로직을 담당하는 객체를 생성합니다.
    engine = PolicyEngine(
        keywords=read_keywords(args.block_keywords),
        payload_preview_bytes=args.payload_preview_bytes,
        log_payload=args.log_payload,
        rewrite_from=args.rewrite_from,
        rewrite_to=args.rewrite_to,
        adapters_dir=args.adapters_dir,
        adapters_hot_reload=not args.adapters_no_reload,
        chatgpt_force_request_text=args.chatgpt_force_request_text,
        chatgpt_block_sysinstruct=args.chatgpt_block_sysinstruct,
        pipeline_server_url=args.server_url,
        pipeline_server_port=args.port,
        pipeline_input_api_path=args.PipelineInputAPIPath,
        pipeline_output_api_path=args.PipelineOutputAPIPath,
        policy_api_timeout_ms=args.policy_api_timeout_ms,
        policy_api_max_inflight=args.policy_api_max_inflight,
        policy_api_pool_limit=args.policy_api_pool_limit,
        policy_api_acquire_timeout_ms=args.policy_api_acquire_timeout_ms,
        policy_api_failure_policy=args.policy_api_failure_policy,
    )
    await engine.start()

    # eve 소비자 풀 (수신/처리 분리). CONSUMER_WORKERS=0 이면 인라인 처리로 폴백.
    eve_pool: Optional[EveConsumerPool] = None
    if CONSUMER_WORKERS > 0 and any(is_eve_socket(p) for p in socket_paths):
        eve_pool = EveConsumerPool(engine, CONSUMER_WORKERS, CONSUMER_QUEUE)
        eve_pool.start()
        engine._eve_consumer_pool = eve_pool  # type: ignore[attr-defined]

    stats_task: Optional[asyncio.Task[Any]] = None
    stats_path = os.environ.get("AIVAX_CONSUMER_STATS", "").strip()
    if stats_path:
        stats_task = asyncio.create_task(_eve_stats_writer(eve_pool, stats_path))

    client_tasks: set[asyncio.Task[Any]] = set()
    active_writers: set[asyncio.StreamWriter] = set()
    servers: list[asyncio.AbstractServer] = []

    def _make_on_connected(is_eve: bool):
        async def _client_task(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            active_writers.add(writer)
            try:
                await handle_client(reader, writer, engine, is_eve)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("client handler error")
            finally:
                active_writers.discard(writer)

        def _on_client_connected(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            task = asyncio.create_task(_client_task(reader, writer))
            client_tasks.add(task)
            task.add_done_callback(client_tasks.discard)

        return _on_client_connected

    # 4. UNIX 서버 시작 (primary + optional extra for sslproxy while in eve mode)
    for sock_path in socket_paths:
        is_eve = is_eve_socket(sock_path)
        server = await asyncio.start_unix_server(
            _make_on_connected(is_eve),
            path=sock_path,
            limit=MAX_FRAME_SIZE,
            start_serving=True,
        )
        os.chmod(sock_path, args.socket_mode)
        servers.append(server)
        logging.info(
            "ai policy engine listening on %s protocol_mode=%s",
            sock_path,
            "eve" if is_eve else "sslproxy-frame",
        )

    # 6. 서버 상태 로그 출력
    logging.info("adapters dir=%s hot_reload=%s", args.adapters_dir, "yes" if not args.adapters_no_reload else "no")
    logging.info("account cache maxsize=%s ttl_sec=%s", getattr(args, "account_cache_maxsize", 10_000), getattr(args, "account_cache_ttl", 3600))
    if args.server_url:
        logging.info(
            "policy api enabled server=%s port=%d input=%s output=%s timeout_ms=%d max_inflight=%d pool_limit=%d acquire_timeout_ms=%d failure_policy=%s",
            args.server_url,
            args.port,
            args.PipelineInputAPIPath,
            args.PipelineOutputAPIPath,
            args.policy_api_timeout_ms,
            args.policy_api_max_inflight,
            args.policy_api_pool_limit,
            args.policy_api_acquire_timeout_ms,
            args.policy_api_failure_policy,
        )
    
    if args.chatgpt_force_request_text:
        logging.info("chatgpt force request text enabled")
    logging.info(
        "chatgpt block sysinstruct (AIVAX_BLOCK_MODEL_INSTRUCTION)=%s",
        "on" if args.chatgpt_block_sysinstruct else "off",
    )
    if args.rewrite_from:
        logging.info("rewrite enabled: %r -> %r", args.rewrite_from, args.rewrite_to)

    # 7. 서버 중단 이벤트 설정: 사용자가 Ctrl+C 또는 프로세스 종료 시 중단 이벤트를 설정합니다.
    stop = asyncio.Event()

    def _stop_handler() -> None:
        if stop.is_set():
            logging.warning("forced shutdown (second signal)")
            os._exit(130)
        stop.set()

    # 8. 시그널 핸들러 등록: Ctrl+C(SIGINT)나 종료(SIGTERM) 신호를 받으면 _stop_handler 실행
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop_handler)
        except NotImplementedError:
            # 윈도우 환경 등에서 시그널 핸들러를 지원하지 않을 경우 무시
            pass
    
    # 9. 대기: stop.set()이 호출될 때까지 여기서 함수 실행이 멈춰있습니다.
    await stop.wait()

    # 10. 서버 종료: 활성 클라이언트 연결을 끊고 handler task를 정리한 뒤 소켓을 닫습니다.
    logging.info("shutdown requested, closing server...")
    for server in servers:
        server.close()

    for writer in list(active_writers):
        try:
            writer.close()
        except Exception:
            pass

    if client_tasks:
        _, pending = await asyncio.wait(client_tasks, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    if eve_pool is not None:
        await eve_pool.drain_and_stop()

    if stats_task is not None:
        stats_task.cancel()
        try:
            await stats_task
        except (asyncio.CancelledError, Exception):
            pass

    for server in servers:
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=SHUTDOWN_GRACE_SECONDS)
        except asyncio.TimeoutError:
            logging.warning(
                "server.wait_closed timed out after %.1fs",
                SHUTDOWN_GRACE_SECONDS,
            )

    await engine.close()
    
    # 11. 소켓 파일 삭제: 서버 종료 후 소켓 파일을 삭제합니다.
    for sock_path in socket_paths:
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass

def parse_args(argv: list[str]) -> argparse.Namespace:
    """
    CLI Arguments를 정의하고 파싱합니다.
    :param argv: 시스템으로부터 전달받은 인자 리스트 (sys.argv[1:])
    :return: 파싱된 인자값들을 담고 있는 Namespace 객체
    """
    # ArgumentParser 객체 생성: 프로그램의 설명(description)을 설정합니다.
    p = argparse.ArgumentParser(description="AIVAX Policy Engine")

    # 1. 네트워크 및 시스템 설정
    p.add_argument("--socket", default=DEFAULT_SOCKET, help="유닉스 소켓 경로")
    p.add_argument(
        "--extra-socket",
        action="append",
        default=[],
        help="추가 유닉스 소켓(반복 가능). 예: card1 eve + sslproxy",
    )
    p.add_argument(
        "--socket-mode",
        type=lambda s: int(s, 8),  # 입력받은 문자열을 8진수 정수로 변환 (예: '660' -> 0o660)
        default=int(os.environ.get("AIVAX_SOCKET_MODE", "660"), 8),
        help="8진수 형태의 소켓 권한 설정 (기본값: 660, AIVAX_SOCKET_MODE)",
    )

    # 2. AI 및 데이터 필터링 설정
    p.add_argument(
        "--block-keywords",
        default="",
        help="차단할 키워드 목록 (쉼표로 구분, 대소문자 구분 없음)",
    )
    p.add_argument(
        "--payload-preview-bytes",
        type=int,
        default=512,
        help="로그에 표시할 페이로드의 최대 길이 (바이트)",
    )
    p.add_argument(
        "--log-payload",
        action="store_true", # 인자가 있으면 True, 없으면 False (기본값)
        help="페이로드 미리보기 로그 출력 여부",
    )
    p.add_argument(
        "--server_url",
        "--server-url",
        dest="server_url",
        default=DEFAULT_POLICY_SERVER_URL,
        help="정책 API 서버 URL (예: http://10.0.80.182)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_POLICY_SERVER_PORT,
        help="정책 API 서버 포트",
    )
    p.add_argument(
        "--PipelineInputAPIPath",
        default=DEFAULT_PIPELINE_INPUT_API_PATH,
        help="Request 검사 API 경로",
    )
    p.add_argument(
        "--PipelineOutputAPIPath",
        default=DEFAULT_PIPELINE_OUTPUT_API_PATH,
        help="Response 검사 API 경로",
    )
    p.add_argument(
        "--policy-api-timeout-ms",
        type=int,
        default=DEFAULT_POLICY_API_TIMEOUT_MS,
        help="정책 판정 API 호출 타임아웃(ms)",
    )
    p.add_argument(
        "--policy-api-max-inflight",
        type=int,
        default=DEFAULT_POLICY_API_MAX_INFLIGHT,
        help="정책 API 동시 호출 상한 (Semaphore)",
    )
    p.add_argument(
        "--policy-api-pool-limit",
        type=int,
        default=DEFAULT_POLICY_API_POOL_LIMIT,
        help="정책 API HTTP 커넥션 풀 최대 연결 수",
    )
    p.add_argument(
        "--policy-api-acquire-timeout-ms",
        type=int,
        default=DEFAULT_POLICY_API_ACQUIRE_TIMEOUT_MS,
        help="정책 API 동시 호출 슬롯 획득 대기 시간(ms)",
    )
    p.add_argument(
        "--policy-api-failure-policy",
        default=DEFAULT_POLICY_API_FAILURE_POLICY,
        choices=["open", "close"],
        help="정책 API 장애 시 처리 정책: open=통과, close=차단",
    )

    # 3. 어댑터(모듈) 관리 설정
    p.add_argument(
        "--adapters-dir",
        default=ADAPTERS_DIR,
        help="어댑터 모듈이 위치한 디렉토리",
    )
    p.add_argument(
        "--adapters-no-reload",
        action="store_true",
        help="어댑터의 핫 리로드(자동 갱신) 기능 비활성화",
    )
    p.add_argument(
        "--dyn-policy-file",
        default=_DEFAULT_DYN_POLICY_FILE,
        help="Suricata 모드 file_control_maxsize/file_allow_extension 출처 (AIDynPolicyFile)",
    )
    p.add_argument(
        "--account-cache-maxsize",
        type=int,
        default=10_000,
        help="계정 조회 캐시 최대 항목 수 (LRU, 기본값: 10000)",
    )
    p.add_argument(
        "--account-cache-ttl",
        type=int,
        default=0,
        help="계정 조회 캐시 TTL 초 (0=만료 없음, 기본값: 3600)",
    )

    # 4. 텍스트 수정 및 AI 관련 설정
    p.add_argument(
        "--chatgpt-force-request-text",
        default=DEFAULT_CHATGPT_FORCE_REQUEST_TEXT,
        help="ChatGPT 요청 메시지를 강제로 대체할 텍스트",
    )
    _chatgpt_sys = p.add_mutually_exclusive_group(required=False)
    _chatgpt_sys.add_argument(
        "--chatgpt-block-sysinstruct",
        dest="chatgpt_block_sysinstruct",
        action="store_true",
        help="ChatGPT BLOCK 시 요청에 차단 안내문+AIVAX_BLOCK_MODEL_INSTRUCTION 삽입(기본: 켬)",
    )
    _chatgpt_sys.add_argument(
        "--no-chatgpt-block-sysinstruct",
        dest="chatgpt_block_sysinstruct",
        action="store_false",
        help="ChatGPT BLOCK 시 위 삽입 비활성화(기존: masked_contents만 프롬프트 치환)",
    )
    p.set_defaults(chatgpt_block_sysinstruct=DEFAULT_CHATGPT_BLOCK_SYSINSTRUCT)
    p.add_argument(
        "--rewrite-from",
        default="",
        help="페이로드에서 수정(치환)하고 싶은 대상 문자열",
    )
    p.add_argument(
        "--rewrite-to",
        default="",
        help="--rewrite-from에 입력한 문자열을 대신할 교체 문자열",
    )

    # 5. 로깅 레벨 설정
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"], # 지정된 값 중 하나만 선택 가능
        help="로그 기록 수준",
    )

    # 입력된 인자 리스트(argv)를 분석하여 반환합니다.
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    # 1. 인자 파싱: 외부에서 정의된 parse_args를 호출해 명령행 인자를 객체로 변환합니다.
    args = parse_args(argv)
    # 2. 로깅 설정: 프로그램 전체에서 사용할 로그 기록 방식을 정의합니다.
    logging.basicConfig(
        # args.log_level(예: 'INFO', 'DEBUG') 문자열을 logging 모듈의 상수 값으로 변환 (객체의 속성값을 가져오는 함수)
        level=getattr(logging, args.log_level),
        # 로그 출력 형식: [날짜/시간] [로그레벨] [메시지]
        format="%(asctime)s %(levelname)s %(message)s",
    )
    load_file_collection_policy(getattr(args, "dyn_policy_file", _DEFAULT_DYN_POLICY_FILE))
    _stop_gate_flush = None
    try:
        from service_gate_stats import start_flush_thread, stop_flush_thread

        start_flush_thread(1.0)
        _stop_gate_flush = stop_flush_thread
    except Exception:
        logging.exception("service_gate_stats flush thread start failed")
    try:
        # 3. 비동기 서버 실행: run_server 함수를 비동기 컨텍스트에서 실행합니다.
        # - 비동기 이벤트 루프 시작: run_server 코루틴을 실행합니다.
        # - asyncio.run()은 루프 생성, 실행, 종료까지 한 번에 관리합니다.
        asyncio.run(run_server(args))
    except KeyboardInterrupt:
        # 4. 사용자가 Ctrl+C를 눌러 중단했을 때, 에러 추적(Traceback) 없이 조용히 넘깁니다.
        pass
    finally:
        if _stop_gate_flush is not None:
            try:
                _stop_gate_flush()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    # sys.argv[1:]를 통해 파일명을 제외한 인자들만 main 함수에 전달하고,
    # main의 반환값(0)을 받아 프로세스를 안전하게 종료(SystemExit)합니다.
    raise SystemExit(main(sys.argv[1:]))
