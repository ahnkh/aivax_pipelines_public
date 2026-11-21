"""
ChatGPT Adapter
Handles request JSON and response SSE event payloads.
"""

from __future__ import annotations

import base64
import hashlib
import html as html_module
import json
import logging
import os
import re
import threading
import time
from collections import deque
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import parse_qsl, unquote, urlencode

try:
    import account_lookup as _account_lookup
except ImportError:
    _account_lookup = None

from ai_service_codes import AI_SERVICE_GPT
from adapters.chatgpt_upload import (
    POLICY_ATTACHMENTS_FINAL,
    build_policy_attachments,
    codex_tool_followup_has_files,
    normalize_upload,
    strip_request_attachments,
)
# 확장자/크기 engine 선차단 비활성 후 미사용 (재활성 시 collection_denied_reason용).
# import file_upload_manager as _fum

logger = logging.getLogger(__name__)

_SSE_RESPONSE_BUFFER: Dict[str, str] = {}
_SSE_RESPONSE_META: Dict[str, Dict[str, str]] = {}
_SSE_RESPONSE_OVERRIDE_EMITTED: Dict[str, bool] = {}
_SSE_RESPONSE_PREFIX_EMITTED: Dict[str, bool] = {}
_ROOM_BY_CONVERSATION: Dict[str, str] = {}
_ROOM_BY_MESSAGE: Dict[str, str] = {}
_ACTIVE_TURN_BY_ROOM: Dict[str, str] = {}
_TURN_TO_ROOM: Dict[str, str] = {}
# Keys of streams that ended (new turn started in same room); [DONE] should drain these first.
_PENDING_DONE_KEYS: deque = deque()
# Order of stream keys as they first appeared; [DONE] matches oldest open stream when PENDING is empty.
_STREAM_KEY_ORDER: List[str] = []
_SSE_FALLBACK_KEY = "__fallback__"

_USER_REQUEST_TAG_RE = re.compile(
    r"(<(?:userRequest|user_request|user_query)\s*>\s*)(.*?)(\s*</(?:userRequest|user_request|user_query)\s*>)",
    re.DOTALL | re.IGNORECASE,
)
_MY_REQUEST_HEADING_RE = re.compile(
    r"(^|\n)((?:#{1,3}\s*)?My request\s*:?\s*\n+)(.*)\Z",
    re.DOTALL | re.IGNORECASE,
)
# Codex desktop appends this block as a user message. It is session metadata
# (date, workspace, subagents), not something the person typed.
_ENVIRONMENT_CONTEXT_RE = re.compile(
    r"<environment_context\b[^>]*>.*?</environment_context>",
    re.DOTALL | re.IGNORECASE,
)
def _is_codex_media_label(text: str) -> bool:
    """One-line wrapper Codex puts beside an input_image, not the user's request."""
    stripped = (text or "").strip()
    if not stripped or "\n" in stripped or len(stripped) > 500:
        return False
    if not (stripped.startswith("<") and stripped.endswith(">")):
        return False
    return "path=" in stripped or stripped.lower() in {
        "<image>",
        "</image>",
        "<audio>",
        "</audio>",
        "<file>",
        "</file>",
    }
_CODEX_TOOL_ITEM_TYPES = frozenset(
    {
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "computer_call",
        "computer_call_output",
        "file_search_call",
        "web_search_call",
        "mcp_call",
        "mcp_list_tools",
        "reasoning",
    }
)
_RESPONSES_SSE_DELTA_TYPES = frozenset(
    {"response.output_text.delta", "response.text.delta"}
)
_RESPONSES_SSE_DONE_TYPES = frozenset(
    {
        "response.output_text.done",
        "response.text.done",
        "response.completed",
        "response.done",
    }
)


def _safe_utf8(data: Union[bytes, bytearray, str]) -> str:
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _extract_bearer_token(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    raw = ""
    for key in ("authorization", "Authorization"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            break
    if not raw:
        headers = meta.get("request_headers")
        if isinstance(headers, dict):
            for key in ("authorization", "Authorization"):
                value = headers.get(key)
                if isinstance(value, str) and value.strip():
                    raw = value.strip()
                    break
    if not raw or raw.upper() == "[REDACTED]":
        return ""
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        obj = json.loads(_safe_utf8(decoded))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _split_sse_data_lines(text: str) -> List[str]:
    """Split one payload that may contain multiple SSE 'data: ...' events into a list of data payloads."""
    out: List[str] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.split("\n"):
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    out.append(payload)
                break
    return out if len(out) > 1 else []


def _coerce_sse_json_blobs(text: str) -> Tuple[List[str], bool]:
    """Split one proxy chunk into JSON payloads. Second value: True if ``data:`` framing was used (restore on output)."""
    n = text.replace("\r\n", "\n")
    multi = _split_sse_data_lines(n)
    if len(multi) > 1:
        return multi, True
    s = n.strip()
    if s.startswith("data:"):
        inner = s[5:].strip()
        return ([inner] if inner else []), True
    return ([s] if s else []), False


def _describe_image_assets(value: Any) -> str:
    """Readable stand-in for ChatGPT image-generation pointers (no image bytes)."""
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("content_type") == "image_asset_pointer":
                pointer = str(node.get("asset_pointer") or "")
                file_id = pointer.rsplit("/", 1)[-1] if pointer else ""
                mime = str(node.get("mime_type") or "image")
                width = node.get("width")
                height = node.get("height")
                size = ""
                if isinstance(width, int) and isinstance(height, int):
                    size = f" {width}x{height}"
                label = file_id or pointer or "image"
                found.append(f"[이미지 생성] {label} {mime}{size}".strip())
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return "\n".join(found)


# The signed estuary/download URL only works while the browser session is
# fetching the file. Bytes are stored as they pass; nothing is fetched later.
_GENERATED_IMAGE_DIR = os.environ.get(
    "AIVAX_GENERATED_IMAGE_DIR",
    "/data/backup/attach_file_backup/generated",
)
_GENERATED_IMAGE_LOCK = threading.Lock()
_GENERATED_IMAGES: Dict[str, Dict[str, Any]] = {}
_GENERATED_IMAGE_LIMIT = 64
_GENERATED_ARTIFACTS: Dict[str, Dict[str, Any]] = {}
_GENERATED_ARTIFACT_LINKS: Dict[str, Dict[str, Any]] = {}
_PENDING_ARTIFACT_DOWNLOADS: deque = deque()
_GENERATED_ARTIFACT_LIMIT = 128
_FILE_ID_RE = re.compile(r"^file_[A-Za-z0-9]+$")


def clear_generated_images_for_tests() -> None:
    with _GENERATED_IMAGE_LOCK:
        _GENERATED_IMAGES.clear()
        _GENERATED_ARTIFACTS.clear()
        _GENERATED_ARTIFACT_LINKS.clear()
        _PENDING_ARTIFACT_DOWNLOADS.clear()


def is_chatgpt_generated_image_path(path: str) -> bool:
    """Full-file image fetch. Thumbnails are the gallery, not this turn."""
    raw = unquote(path or "")
    lowered = raw.lower()
    if "thumbnail" in lowered:
        return False
    base = lowered.split("?", 1)[0]
    return (
        "/backend-api/estuary/content" in base
        or "/backend-api/files/download/" in base
    )


def chatgpt_generated_image_file_id(path: str) -> str:
    if not is_chatgpt_generated_image_path(path):
        return ""
    raw = unquote(path or "")
    base, _, query = raw.partition("?")
    file_id = ""
    if "/backend-api/estuary/content" in base.lower():
        for key, value in parse_qsl(query, keep_blank_values=False):
            if key == "id" and value:
                file_id = value
                break
    else:
        file_id = base.rstrip("/").rsplit("/", 1)[-1]
    file_id = file_id.split("#", 1)[0].strip()
    if not _FILE_ID_RE.fullmatch(file_id):
        return ""
    return file_id


def _image_kind(body: bytes) -> Tuple[str, str]:
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if body.startswith(b"GIF87a") or body.startswith(b"GIF89a"):
        return "image/gif", "gif"
    if len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp", "webp"
    return "", ""


def capture_chatgpt_generated_image(
    path: str,
    body: bytes,
    backup_dir: str = "",
) -> bool:
    """Save one in-flight generated image. Returns False for thumbnails and non-images."""
    file_id = chatgpt_generated_image_file_id(path)
    if not file_id or not body:
        return False
    mime, ext = _image_kind(body)
    if not mime:
        return False
    directory = backup_dir or _GENERATED_IMAGE_DIR
    try:
        os.makedirs(directory, exist_ok=True)
        filename = f"{file_id}.{ext}"
        destination = os.path.join(directory, filename)
        temporary = destination + ".part"
        with open(temporary, "wb") as handle:
            handle.write(body)
        os.replace(temporary, destination)
        os.chmod(destination, 0o644)
    except Exception:
        logger.exception("chatgpt generated image save failed file_id=%s", file_id)
        return False
    record = {
        "file_name": filename,
        "file_backup_path": destination,
        "mime_type": mime,
        "size": len(body),
        "saved_at": time.time(),
    }
    with _GENERATED_IMAGE_LOCK:
        _GENERATED_IMAGES[file_id] = record
        while len(_GENERATED_IMAGES) > _GENERATED_IMAGE_LIMIT:
            _GENERATED_IMAGES.pop(next(iter(_GENERATED_IMAGES)))
    logger.info(
        "chatgpt_generated_image_saved file_id=%s bytes=%d mime=%s",
        file_id,
        len(body),
        mime,
    )
    return True


def generated_image_file_ids(text: str) -> List[str]:
    found: List[str] = []
    for line in (text or "").splitlines():
        if not line.startswith("[이미지 생성]"):
            continue
        parts = line.split()
        if len(parts) >= 3 and _FILE_ID_RE.fullmatch(parts[2]):
            found.append(parts[2])
    return found


def generated_image_attachments(text: str) -> List[Dict[str, Any]]:
    attachments: List[Dict[str, Any]] = []
    with _GENERATED_IMAGE_LOCK:
        for file_id in generated_image_file_ids(text):
            record = _GENERATED_IMAGES.get(file_id)
            if not record:
                continue
            attachments.append(
                {
                    "file_name": record["file_name"],
                    "file_backup_path": record["file_backup_path"],
                    "mime_type": record["mime_type"],
                    "size": record["size"],
                }
            )
    return attachments


async def wait_generated_image_files(text: str, timeout: float = 8.0) -> List[Dict[str, Any]]:
    """Poll until the in-flight download of this turn's file has been saved."""
    import asyncio

    ids = generated_image_file_ids(text)
    if not ids:
        return []
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        found = generated_image_attachments(text)
        if len(found) >= len(ids) or time.monotonic() >= deadline:
            return found
        await asyncio.sleep(0.15)


def _sandbox_paths(text: str) -> List[str]:
    paths: List[str] = []
    for match in re.finditer(r"sandbox:(/[^)\s]+)", text or "", re.IGNORECASE):
        path = unquote(match.group(1)).strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def register_generated_artifact_links(text: str, message_id: str) -> List[Dict[str, Any]]:
    """Associate sandbox links in a model answer with the original prompt row."""
    mid = str(message_id or "").strip()
    paths = _sandbox_paths(text)
    if not mid or not paths:
        return []
    with _GENERATED_IMAGE_LOCK:
        for path in paths:
            _GENERATED_ARTIFACT_LINKS[path] = {
                "message_id": mid,
                "saved_at": time.time(),
            }
        while len(_GENERATED_ARTIFACT_LINKS) > _GENERATED_ARTIFACT_LIMIT:
            _GENERATED_ARTIFACT_LINKS.pop(next(iter(_GENERATED_ARTIFACT_LINKS)))
        return [
            {
                key: value
                for key, value in _GENERATED_ARTIFACTS[path].items()
                if key not in ("saved_at", "message_id")
            }
            for path in paths
            if path in _GENERATED_ARTIFACTS
        ]


def is_chatgpt_generated_artifact_path(path: str) -> bool:
    base = unquote(path or "").split("?", 1)[0].lower()
    return (
        "/backend-api/conversation/" in base
        and base.endswith("/interpreter/download")
    )


def is_chatgpt_generated_file_raw_path(path: str) -> bool:
    """Blob fetched after interpreter/download redirects to the real PPT."""
    raw = unquote(path or "")
    lowered = raw.lower()
    if "thumbnail" in lowered:
        return False
    base = lowered.split("?", 1)[0]
    return "/files/" in base and base.endswith("/raw")


def note_chatgpt_artifact_download(path: str) -> None:
    """Remember the clicked sandbox file until its redirected body arrives."""
    sandbox_path, _name = _artifact_download_info(path)
    if not sandbox_path:
        return
    now = time.time()
    with _GENERATED_IMAGE_LOCK:
        _PENDING_ARTIFACT_DOWNLOADS.append((now, sandbox_path))
        while _PENDING_ARTIFACT_DOWNLOADS and (
            now - _PENDING_ARTIFACT_DOWNLOADS[0][0] > 60
            or len(_PENDING_ARTIFACT_DOWNLOADS) > 32
        ):
            _PENDING_ARTIFACT_DOWNLOADS.popleft()


def _artifact_download_info(path: str) -> Tuple[str, str]:
    if not is_chatgpt_generated_artifact_path(path):
        return "", ""
    _, _, query = (path or "").partition("?")
    sandbox_path = ""
    for key, value in parse_qsl(query, keep_blank_values=False):
        if key == "sandbox_path" and value:
            sandbox_path = unquote(value).strip()
            break
    if not sandbox_path:
        return "", ""
    name = os.path.basename(sandbox_path)
    if not name or name in (".", ".."):
        return "", ""
    return sandbox_path, name


def _peek_pending_artifact_path() -> str:
    now = time.time()
    with _GENERATED_IMAGE_LOCK:
        while _PENDING_ARTIFACT_DOWNLOADS and now - _PENDING_ARTIFACT_DOWNLOADS[0][0] > 60:
            _PENDING_ARTIFACT_DOWNLOADS.popleft()
        if not _PENDING_ARTIFACT_DOWNLOADS:
            return ""
        return _PENDING_ARTIFACT_DOWNLOADS[0][1]


def _pop_pending_artifact_path() -> None:
    with _GENERATED_IMAGE_LOCK:
        if _PENDING_ARTIFACT_DOWNLOADS:
            _PENDING_ARTIFACT_DOWNLOADS.popleft()


def capture_chatgpt_generated_artifact(
    path: str,
    body: bytes,
    backup_dir: str = "",
) -> Optional[Dict[str, Any]]:
    """Save a session-only interpreter PPT/PPTX download."""
    sandbox_path, original_name = _artifact_download_info(path)
    from_redirect = False
    if not sandbox_path and is_chatgpt_generated_file_raw_path(path):
        sandbox_path = _peek_pending_artifact_path()
        original_name = os.path.basename(sandbox_path) if sandbox_path else ""
        from_redirect = True
    if not sandbox_path or not original_name or not body:
        return None
    extension = os.path.splitext(original_name)[1].lower()
    if extension == ".pptx":
        valid = body.startswith(b"PK\x03\x04")
        mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    elif extension == ".ppt":
        valid = body.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        mime = "application/vnd.ms-powerpoint"
    else:
        return None
    if not valid:
        return None
    if from_redirect:
        _pop_pending_artifact_path()

    directory = backup_dir or _GENERATED_IMAGE_DIR
    digest = hashlib.sha256(sandbox_path.encode("utf-8", errors="ignore")).hexdigest()[:12]
    stored_name = f"{digest}_{original_name}"
    destination = os.path.join(directory, stored_name)
    temporary = destination + ".part"
    try:
        os.makedirs(directory, exist_ok=True)
        with open(temporary, "wb") as handle:
            handle.write(body)
        os.replace(temporary, destination)
        os.chmod(destination, 0o644)
    except Exception:
        logger.exception("chatgpt generated artifact save failed name=%s", original_name)
        return None

    record: Dict[str, Any] = {
        "file_name": original_name,
        "file_backup_path": destination,
        "mime_type": mime,
        "size": len(body),
        "saved_at": time.time(),
        "message_id": "",
    }
    with _GENERATED_IMAGE_LOCK:
        link = _GENERATED_ARTIFACT_LINKS.get(sandbox_path)
        if link:
            record["message_id"] = str(link.get("message_id") or "")
        _GENERATED_ARTIFACTS[sandbox_path] = record
        while len(_GENERATED_ARTIFACTS) > _GENERATED_ARTIFACT_LIMIT:
            _GENERATED_ARTIFACTS.pop(next(iter(_GENERATED_ARTIFACTS)))
    logger.info(
        "chatgpt_generated_artifact_saved name=%s bytes=%d mime=%s linked=%s",
        original_name,
        len(body),
        mime,
        bool(record["message_id"]),
    )
    return {
        key: value
        for key, value in record.items()
        if key != "saved_at"
    }


def _extract_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return _safe_utf8(value)
    if isinstance(value, list):
        parts = [_extract_content(v) for v in value]
        parts = [p for p in parts if p]
        return "\n".join(parts)
    if isinstance(value, dict):
        if "parts" in value and isinstance(value["parts"], list):
            return _extract_content(value["parts"])
        if "content" in value:
            return _extract_content(value["content"])
        if "text" in value and isinstance(value["text"], str):
            return value["text"]
        if "message" in value:
            return _extract_content(value["message"])
        if "delta" in value:
            return _extract_content(value["delta"])
        if "choices" in value:
            return _extract_content(value["choices"])
        if "data" in value:
            return _extract_content(value["data"])
    return ""


def _extract_sse_patch_text(value: Any) -> str:
    """Extract assistant text from conduit patch array.
    Format: [{"p": "/message/content/parts/0", "o": "append", "v": "chunk"}, ...]
    """
    if not isinstance(value, list):
        return ""
    chunks = []
    for item in value:
        if not isinstance(item, dict):
            continue
        path = item.get("p")
        op = item.get("o")
        val = item.get("v")
        if not isinstance(path, str) or not isinstance(op, str):
            continue
        if not path.startswith("/message/content/parts/"):
            continue
        if op.lower() not in ("append", "replace", "add"):
            continue
        if isinstance(val, str):
            chunks.append(val)
        elif isinstance(val, (int, float, bool)) or val is None:
            pass
        else:
            chunks.append(_extract_content(val))
    return "".join(chunks)


def _extract_root_parts_delta_text(obj: Dict[str, Any]) -> str:
    """Single-frame conduit deltas (no nested list): JSON-RPC style patch at root.

    ChatGPT streaming often emits many frames as::
        {"p": "/message/content/parts/0", "o": "append", "v": "token_chunk"}

    Previously only {"v": [ {...}, ... ]} arrays were parsed, so these chunks were dropped
    and merged assistant text lost prefixes (e.g. **결** before 론).
    """
    path = obj.get("p")
    op = obj.get("o")
    val = obj.get("v")
    if not isinstance(path, str) or not isinstance(op, str):
        return ""
    if not path.startswith("/message/content/parts/"):
        return ""
    if op.lower() not in ("append", "replace", "add"):
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, (int, float, bool)) or val is None:
        return ""
    return _extract_content(val)


# Thinking / conduit: assistant envelopes that are not the user-visible answer (policy + merge).
_CHATGPT_SKIP_ASSISTANT_CONTENT_TYPES = frozenset({"reasoning_recap", "thoughts"})

# Harmony channels: only ``final`` is the answer. ``commentary`` carries the
# "…정리해드릴게요 / 확인해서 설명할게요" planning note and tool calls; ``analysis`` is CoT.
_CHATGPT_ANSWER_CHANNEL = "final"
_CHATGPT_ANSWER_CONTENT_TYPES = frozenset({"text", "multimodal_text"})

# Conversation path SSE that carries no assistant text (UI resume / handoff / chrome).
# Live ~973B bodies are typically resume_conversation_token + stream_handoff only.
_CHATGPT_SSE_CONTROL_TYPES = frozenset(
    {
        "resume_conversation_token",
        "stream_handoff",
        "title_generation",
        "conversation_detail_metadata",
        "message_marker",
        "server_ste_metadata",
    }
)

# Frames that name the follow-up stream carrying the real answer.
_CHATGPT_SSE_HANDOFF_TYPES = frozenset({"stream_handoff", "resume_conversation_token"})

# High-reasoning: answer arrives on a *separate* stream after control SSE
# (stream_handoff): historically POST /conversation/resume, increasingly Celsius WS
# (/ws/user). UI merges Input/Output by message_id==request_id of the original
# /f/conversation turn.
# Map handoff topic/conversation keys → parent request_id (TTL seconds).
# Pro/high-reasoning turns routinely think for 10+ minutes before the answer
# stream opens, so the binding has to outlive the reasoning window.
#
# Mirror SHM uses many processes: the map MUST be cross-process (Valkey via
# resume_parent_store). Process-local dict alone caused parent=- on the WS
# worker that did not see the HTTP handoff registration.
_RESUME_PARENT_TTL_SEC = 3600.0  # kept for tests / docs; store reads env/TTL


def _resume_parent_put(key: str, parent_request_id: str) -> None:
    try:
        from resume_parent_store import put as _store_put
    except ImportError:  # pragma: no cover
        try:
            from core.resume_parent_store import put as _store_put
        except ImportError:
            logger.debug("resume_parent_store missing; skip put")
            return
    try:
        _store_put(key, parent_request_id)
    except Exception:
        logger.debug("resume_parent_put failed key=%s", str(key or "")[:80], exc_info=True)


def _bind_parent_key(key: str, parent_request_id: str, *, replace: bool) -> bool:
    """Store a handoff key. Turn keys keep the first request; conversation keys move only for a new turn."""
    existing = _resume_parent_get(key)
    rid = (parent_request_id or "").strip()
    if not key or not rid:
        return False
    if existing and existing != rid and not replace:
        return False
    if existing == rid:
        return False
    _resume_parent_put(key, rid)
    return not existing


def _resume_parent_get(key: str) -> str:
    try:
        from resume_parent_store import get as _store_get
    except ImportError:  # pragma: no cover
        try:
            from core.resume_parent_store import get as _store_get
        except ImportError:
            return ""
    try:
        return _store_get(key)
    except Exception:
        logger.debug("resume_parent_get failed key=%s", str(key or "")[:80], exc_info=True)
        return ""


def _iter_sse_json_objs(payload: bytes):
    text = _safe_utf8(payload).replace("\r\n", "\n").replace("\r", "\n")
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if isinstance(obj, dict):
            yield obj


def register_stream_handoff_parent(payload: bytes, parent_request_id: str) -> List[str]:
    """Bind stream_handoff / resume token keys to the chat-turn request_id.

    A later resume or websocket body repeats the same turn id. That repeat must
    not replace the prompt request, or the answer is logged under a request that
    has no prompt. A new turn id does take over the conversation id.
    """
    rid = (parent_request_id or "").strip()
    if not rid:
        return []
    keys: List[str] = []
    conv_keys: List[str] = []
    new_turn = False

    def remember_turn(key: str) -> None:
        nonlocal new_turn
        if _bind_parent_key(key, rid, replace=False):
            new_turn = True
        keys.append(key)

    def remember_conv(key: str) -> None:
        conv_keys.append(key)
        keys.append(key)

    for obj in _iter_sse_json_objs(payload or b""):
        cid = obj.get("conversation_id")
        if isinstance(cid, str) and cid.strip():
            remember_conv(f"conv:{cid.strip()}")
        tex = obj.get("turn_exchange_id")
        if isinstance(tex, str) and tex.strip():
            remember_turn(f"turn:{tex.strip()}")
            remember_turn(f"topic:conversation-turn-{tex.strip()}")
        opts = obj.get("options")
        if isinstance(opts, list):
            for opt in opts:
                if not isinstance(opt, dict):
                    continue
                tid = opt.get("topic_id")
                if isinstance(tid, str) and tid.strip():
                    remember_turn(f"topic:{tid.strip()}")
        # stream_handoff handle / topic (Celsius WS join key)
        sh = obj.get("stream_handoff")
        if isinstance(sh, dict):
            for hk, prefix in (
                ("handle", "topic:"),
                ("topic_id", "topic:"),
                ("topic", "topic:"),
                ("conversation_id", "conv:"),
                ("turn_exchange_id", "turn:"),
            ):
                hv = sh.get(hk)
                if isinstance(hv, str) and hv.strip():
                    key = f"{prefix}{hv.strip()}"
                    if prefix == "conv:":
                        remember_conv(key)
                    else:
                        remember_turn(key)
                    if hk in ("handle", "topic_id", "topic") and not hv.startswith(
                        "conversation-turn-"
                    ):
                        remember_turn(f"topic:conversation-turn-{hv.strip()}")
        # JWT turn_topic_id (resume_conversation_token)
        if obj.get("type") == "resume_conversation_token":
            token = obj.get("token")
            if isinstance(token, str) and token.count(".") >= 2:
                try:
                    claims = _decode_jwt_payload(token)
                except Exception:
                    claims = None
                if isinstance(claims, dict):
                    ttid = claims.get("turn_topic_id")
                    if isinstance(ttid, str) and ttid.strip():
                        remember_turn(f"topic:{ttid.strip()}")
        message = obj.get("input_message")
        if isinstance(message, dict):
            mid = message.get("id")
            if isinstance(mid, str) and mid.strip() and _message_role(message) in ("", "user"):
                remember_turn(f"umsg:{mid.strip()}")
    if new_turn:
        for key in conv_keys:
            _resume_parent_put(key, rid)
    else:
        for key in conv_keys:
            _bind_parent_key(key, rid, replace=False)
    return keys


def _bind_prompt_request(req: Dict[str, Any], obj: Dict[str, Any]) -> None:
    """Point a repeated post of the same user message at the original prompt request."""
    rid = str(req.get("request_id") or "").strip()
    if not rid or not isinstance(obj, dict):
        return
    owner = rid
    umsg = _extract_request_message_id(obj)
    if umsg:
        key = f"umsg:{umsg}"
        existing = _resume_parent_get(key)
        if existing and existing != rid:
            req["_chatgpt_ui_message_id"] = existing
            owner = existing
        else:
            _bind_parent_key(key, rid, replace=False)
    cid = _extract_request_conversation_id(obj)
    if cid:
        _bind_parent_key(f"conv:{cid}", owner, replace=False)


def _user_message_parent(payload: bytes) -> str:
    for obj in _iter_sse_json_objs(payload or b""):
        message = obj.get("input_message")
        if not isinstance(message, dict):
            continue
        mid = message.get("id")
        if isinstance(mid, str) and mid.strip():
            hit = _resume_parent_get(f"umsg:{mid.strip()}")
            if hit:
                return hit
    return ""


def _conversation_ids_in_payload(payload: bytes) -> List[str]:
    found: List[str] = []
    for obj in _iter_sse_json_objs(payload or b""):
        cid = obj.get("conversation_id")
        if isinstance(cid, str) and cid.strip() and cid.strip() not in found:
            found.append(cid.strip())
    for match in re.finditer(
        r'"conversation_id"\s*:\s*"([^"]+)"', _safe_utf8(payload or b"")
    ):
        cid = (match.group(1) or "").strip()
        if cid and not cid.startswith("pending:") and cid not in found:
            found.append(cid)
    return found


def _stash_pending_answer(conversation_id: str, text: str) -> None:
    cid = (conversation_id or "").strip()
    body = (text or "").strip()
    if not cid or not body:
        return
    key = f"pending_answer:{cid}"
    prev = _resume_parent_get(key)
    if prev and len(prev) >= len(body):
        return
    _resume_parent_put(key, body)


def _pending_answer_for_payload(payload: bytes) -> str:
    best = ""
    for cid in _conversation_ids_in_payload(payload):
        text = _resume_parent_get(f"pending_answer:{cid}")
        if text and len(text) > len(best):
            best = text
    return best


# Turns whose /f/conversation stream ended without answer text. The answer
# for these arrives later on /ws/user or a resume stream; when
# AIVAX_GPT_ANSWER_TRACE_DIR is set, those later bodies are saved so the
# binding can be checked against what ChatGPT actually sent.
_ANSWER_TRACE_WATCH: Dict[str, Tuple[str, float]] = {}
_ANSWER_TRACE_TTL_SEC = 3600.0
_ANSWER_TRACE_MAX_WATCH = 64
_ANSWER_TRACE_MAX_FILES = 4000
_ANSWER_TRACE_JWT = re.compile(rb"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_ANSWER_TRACE_VERIFY = re.compile(rb"verify=[^\s\"'&]+")


def _answer_trace_dir() -> str:
    return os.environ.get("AIVAX_GPT_ANSWER_TRACE_DIR", "").strip()


def _answer_trace_watch(request_id: str, payload: bytes, keys: List[str]) -> None:
    if not _answer_trace_dir():
        return
    rid = (request_id or "").strip()
    if not rid:
        return
    now = time.time()
    marks: Set[str] = set(_conversation_ids_in_payload(payload or b""))
    for key in keys or ():
        if key.startswith(("topic:", "turn:")):
            marks.add(key.split(":", 1)[1])
        elif key.startswith("conv:"):
            marks.add(key[5:])
    for mark in marks:
        if mark:
            _ANSWER_TRACE_WATCH[mark] = (rid, now)
    if len(_ANSWER_TRACE_WATCH) > _ANSWER_TRACE_MAX_WATCH:
        oldest = sorted(_ANSWER_TRACE_WATCH.items(), key=lambda kv: kv[1][1])
        for mark, _ in oldest[: len(_ANSWER_TRACE_WATCH) - _ANSWER_TRACE_MAX_WATCH]:
            _ANSWER_TRACE_WATCH.pop(mark, None)
    logger.info(
        "chatgpt answer_trace watch request_id=%s marks=%d",
        rid[:36],
        len(marks),
    )


def _answer_trace_owner(payload: bytes) -> str:
    if not _ANSWER_TRACE_WATCH:
        return ""
    text = _safe_utf8(payload or b"")
    now = time.time()
    for mark, (rid, ts) in list(_ANSWER_TRACE_WATCH.items()):
        if now - ts > _ANSWER_TRACE_TTL_SEC:
            _ANSWER_TRACE_WATCH.pop(mark, None)
            continue
        if mark in text:
            return rid
    return ""


def _answer_trace_dump(req: Dict[str, Any], payload: bytes, kind: str, **info: Any) -> None:
    dump_dir = _answer_trace_dir()
    if not dump_dir:
        return
    owner = _answer_trace_owner(payload)
    if not owner:
        return
    rid = str(req.get("request_id") or "unknown")[:36]
    detail = " ".join(f"{k}={v}" for k, v in info.items())
    logger.info(
        "chatgpt answer_trace %s owner=%s request_id=%s payload_len=%d %s",
        kind,
        owner[:36],
        rid,
        len(payload or b""),
        detail,
    )
    try:
        os.makedirs(dump_dir, exist_ok=True)
        if len(os.listdir(dump_dir)) >= _ANSWER_TRACE_MAX_FILES:
            return
        body = _ANSWER_TRACE_JWT.sub(b"<jwt>", (payload or b"")[: 2 * 1024 * 1024])
        body = _ANSWER_TRACE_VERIFY.sub(b"verify=<redacted>", body)
        name = f"{time.time_ns()}_{owner[:36]}_{kind}_{rid}_{len(payload or b'')}.bin"
        with open(os.path.join(dump_dir, name), "wb") as fh:
            fh.write(body)
    except Exception:
        logger.debug("chatgpt answer_trace dump failed", exc_info=True)


def resolve_resume_parent_request_id(payload: bytes) -> str:
    """Find parent /f/conversation request_id for resume SSE *or* Celsius WS bodies."""
    # 1) Classic resume SSE (data: … frames)
    for obj in _iter_sse_json_objs(payload or b""):
        hit = _parent_from_obj(obj)
        if hit:
            return hit
    # 2) WS / already-unwrapped JSON (and nested SSE-in-string envelopes)
    for obj in _iter_ws_json_objects(payload or b""):
        hit = _parent_from_obj(obj)
        if hit:
            return hit
    # 3) Fallback: scan raw text for topic ids we registered at handoff
    text = _safe_utf8(payload or b"")
    for m in re.finditer(r"conversation-turn-[0-9a-fA-F-]{20,}", text):
        hit = _resume_parent_get(f"topic:{m.group(0)}")
        if hit:
            return hit
    return ""


def _parent_from_obj(obj: Dict[str, Any]) -> str:
    if not isinstance(obj, dict):
        return ""
    if obj.get("type") == "resume_conversation_token":
        token = obj.get("token")
        if isinstance(token, str) and token.count(".") >= 2:
            try:
                claims = _decode_jwt_payload(token)
            except Exception:
                claims = None
            if isinstance(claims, dict):
                ttid = claims.get("turn_topic_id")
                if isinstance(ttid, str) and ttid.strip():
                    hit = _resume_parent_get(f"topic:{ttid.strip()}")
                    if hit:
                        return hit
    # Inline stream_handoff.parent_id (rare on answer frames; useful when present)
    sh = obj.get("stream_handoff")
    if isinstance(sh, dict):
        for pk in ("parent_id", "parent_request_id", "parent"):
            pv = sh.get(pk)
            if isinstance(pv, str) and pv.strip():
                return pv.strip()
        for hk, prefix in (
            ("handle", "topic:"),
            ("topic_id", "topic:"),
            ("topic", "topic:"),
            ("conversation_id", "conv:"),
            ("turn_exchange_id", "turn:"),
        ):
            hv = sh.get(hk)
            if isinstance(hv, str) and hv.strip():
                hit = _resume_parent_get(f"{prefix}{hv.strip()}")
                if hit:
                    return hit
    for key_name, prefix in (
        ("turn_exchange_id", "turn:"),
        ("turn_id", "turn:"),
        ("conversation_id", "conv:"),
        ("topic_id", "topic:"),
        ("topic", "topic:"),
    ):
        val = obj.get(key_name)
        if isinstance(val, str) and val.strip():
            hit = _resume_parent_get(f"{prefix}{val.strip()}")
            if hit:
                return hit
            if key_name in ("turn_exchange_id", "turn_id"):
                hit = _resume_parent_get(f"topic:conversation-turn-{val.strip()}")
                if hit:
                    return hit
            if key_name in ("topic_id", "topic") and not val.startswith("conversation-turn-"):
                hit = _resume_parent_get(f"topic:conversation-turn-{val.strip()}")
                if hit:
                    return hit
    opts = obj.get("options")
    if isinstance(opts, list):
        for opt in opts:
            if isinstance(opt, dict):
                tid = opt.get("topic_id")
                if isinstance(tid, str) and tid.strip():
                    hit = _resume_parent_get(f"topic:{tid.strip()}")
                    if hit:
                        return hit
    for nest_key in ("v", "message", "conversation", "payload", "data", "body"):
        nest = obj.get(nest_key)
        if isinstance(nest, dict):
            hit = _parent_from_obj(nest)
            if hit:
                return hit
        elif isinstance(nest, str) and "conversation-turn-" in nest:
            for m in re.finditer(r"conversation-turn-[0-9a-fA-F-]{20,}", nest):
                hit = _resume_parent_get(f"topic:{m.group(0)}")
                if hit:
                    return hit
    return ""


def _is_answer_message(msg: Dict[str, Any]) -> bool:
    """True when this message is the user-visible assistant answer (not preamble/CoT/tool)."""
    if _message_role(msg) != "assistant":
        return False
    if _skip_chatgpt_assistant_envelope(msg):
        return False
    channel = msg.get("channel")
    if isinstance(channel, str) and channel and channel != _CHATGPT_ANSWER_CHANNEL:
        return False
    content = msg.get("content")
    if isinstance(content, dict):
        ct = content.get("content_type")
        if isinstance(ct, str) and ct and ct not in _CHATGPT_ANSWER_CONTENT_TYPES:
            return False
    return True


def _message_content_type(msg: Dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, dict):
        ct = content.get("content_type")
        if isinstance(ct, str):
            return ct
    return ""


def _delta_message_from_obj(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Message envelope that opens (or snapshots) a message: ``{"v":{"message":…}}`` / ``{"message":…}``."""
    v = obj.get("v")
    if isinstance(v, dict) and isinstance(v.get("message"), dict):
        return v["message"]
    if isinstance(obj.get("message"), dict) and not obj.get("type"):
        return obj["message"]
    return None


class _DeltaMessageAssembler:
    """Replay conduit ``delta_encoding: v1`` into per-message text.

    A message event opens message ``c``; every following patch — including bare
    ``{"v":"…"}`` continuations that reuse the previous ``p``/``o`` — belongs to that
    message until the next message event. Channel/metadata patches are applied so
    a message that turns into ``commentary`` mid-stream is classified correctly.
    """

    def __init__(self) -> None:
        self.order: List[str] = []
        self.msgs: Dict[str, Dict[str, Any]] = {}
        self.texts: Dict[str, str] = {}
        self.cur: str = ""
        self.last_p: str = ""
        self.last_o: str = ""
        self._anon = 0
        self.by_c: Dict[int, str] = {}

    def feed(self, obj: Dict[str, Any]) -> bool:
        """Apply one decoded SSE data object. Returns True when it touched a message."""
        c = obj.get("c")
        msg = _delta_message_from_obj(obj)
        if msg is not None:
            mid = str(msg.get("id") or "")
            if not mid:
                self._anon += 1
                mid = f"_anon{self._anon}"
            if mid not in self.msgs:
                self.order.append(mid)
            self.msgs[mid] = dict(msg)
            self.texts[mid] = _extract_content(msg.get("content")) or _describe_image_assets(
                msg.get("content")
            )
            if isinstance(c, int):
                self.by_c[c] = mid
            self.cur = mid
            self.last_p = ""
            self.last_o = ""
            return True
        if obj.get("type"):
            return False
        if isinstance(c, int) and c in self.by_c and self.by_c[c] != self.cur:
            self.cur = self.by_c[c]
            self.last_p = ""
            self.last_o = ""
        v = obj.get("v")
        ops: Optional[List[Any]] = None
        if obj.get("o") == "patch" and isinstance(v, list):
            ops = v
        elif isinstance(v, list) and "p" not in obj and "o" not in obj:
            ops = v
        if ops is not None:
            target = self._thoughts_patch_target(ops)
            if target:
                saved = (self.cur, self.last_p, self.last_o)
                self.cur = target
            for op in ops:
                if isinstance(op, dict):
                    self._apply(op.get("p"), op.get("o"), op.get("v"))
            if target:
                self.cur, self.last_p, self.last_o = saved
            return True
        if "p" in obj or "o" in obj or isinstance(v, str):
            self._apply(obj.get("p"), obj.get("o"), v)
            return True
        return False

    def _thoughts_patch_target(self, ops: List[Any]) -> str:
        """Thinking-summary patches arrive without ``c`` right after the answer
        message opens; applying them to the answer marks it visually hidden."""
        touches_thoughts = any(
            isinstance(op, dict)
            and isinstance(op.get("p"), str)
            and op["p"].startswith("/message/content/thoughts")
            for op in ops
        )
        if not touches_thoughts or not self.cur:
            return ""
        if _message_content_type(self.msgs.get(self.cur) or {}) == "thoughts":
            return ""
        for mid in reversed(self.order):
            if _message_content_type(self.msgs.get(mid) or {}) == "thoughts":
                return mid
        return ""

    def _apply(self, p: Any, o: Any, v: Any) -> None:
        path = p if isinstance(p, str) and p else self.last_p
        op = (o if isinstance(o, str) and o else self.last_o or "append").lower()
        self.last_p, self.last_o = path, op
        if not path:
            return
        if not self.cur:
            # Resume/continuation stream: the message was opened on an earlier stream.
            self._anon += 1
            self.cur = f"_anon{self._anon}"
            self.order.append(self.cur)
            self.msgs[self.cur] = {"author": {"role": "assistant"}}
            self.texts[self.cur] = ""
        msg = self.msgs[self.cur]
        if path.startswith("/message/content/parts/") or path == "/message/content/text":
            if isinstance(v, str):
                chunk = v
            elif isinstance(v, (dict, list)):
                chunk = _extract_content(v)
            else:
                return
            if op == "append":
                self.texts[self.cur] = self.texts.get(self.cur, "") + chunk
            elif op in ("replace", "add"):
                self.texts[self.cur] = chunk
        elif path == "/message/channel" and isinstance(v, str):
            msg["channel"] = v
        elif path == "/message/status" and isinstance(v, str):
            msg["status"] = v
        elif path == "/message/metadata" and isinstance(v, dict):
            meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
            msg["metadata"] = {**meta, **v}
        elif path.startswith("/message/metadata/"):
            meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
            msg["metadata"] = {**meta, path[len("/message/metadata/"):]: v}
        elif path == "/message/content/content_type" and isinstance(v, str):
            content = msg.get("content") if isinstance(msg.get("content"), dict) else {}
            msg["content"] = {**content, "content_type": v}

    def current_is_answer(self) -> bool:
        return bool(self.cur) and _is_answer_message(self.msgs[self.cur])

    def answer_text(self) -> str:
        parts = []
        for mid in self.order:
            text = self.texts.get(mid, "").strip()
            if not text:
                continue
            # Image generation arrives as a tool message, not assistant text.
            if _is_answer_message(self.msgs[mid]) or text.startswith("[이미지 생성]"):
                parts.append(text)
        return "\n\n".join(parts)

    def has_messages(self) -> bool:
        return bool(self.order)


# Streaming extract(): is the message currently open on this stream key the answer?
_SSE_ANSWER_VISIBLE: Dict[str, bool] = {}
_SSE_ANSWER_VISIBLE_MAX = 4096


def _track_answer_visibility(key: str, obj: Dict[str, Any]) -> None:
    if not key:
        return
    msg = _delta_message_from_obj(obj)
    if msg is not None:
        _SSE_ANSWER_VISIBLE[key] = _is_answer_message(msg)
    elif key in _SSE_ANSWER_VISIBLE and _SSE_ANSWER_VISIBLE[key] is False:
        return
    else:
        ops = obj.get("v") if isinstance(obj.get("v"), list) else [obj]
        for op in ops:
            if isinstance(op, dict) and op.get("p") == "/message/channel":
                ch = op.get("v")
                if isinstance(ch, str) and ch and ch != _CHATGPT_ANSWER_CHANNEL:
                    _SSE_ANSWER_VISIBLE[key] = False
    if len(_SSE_ANSWER_VISIBLE) > _SSE_ANSWER_VISIBLE_MAX:
        for old in list(_SSE_ANSWER_VISIBLE)[: _SSE_ANSWER_VISIBLE_MAX // 4]:
            _SSE_ANSWER_VISIBLE.pop(old, None)


def _skip_chatgpt_assistant_envelope(msg: Dict[str, Any]) -> bool:
    """Skip internal envelopes (thinking recap, CoT placeholders) — same role/channel as real text."""
    meta = msg.get("metadata")
    if isinstance(meta, dict):
        if meta.get("reasoning_status") == "is_reasoning":
            return True
        if meta.get("is_visually_hidden_from_conversation"):
            return True
        if meta.get("is_thinking_preamble_message") is True:
            return True
    content = msg.get("content")
    if isinstance(content, dict):
        ct = content.get("content_type")
        if isinstance(ct, str) and ct in _CHATGPT_SKIP_ASSISTANT_CONTENT_TYPES:
            return True
    return False


def _extract_sse_assistant_text(obj: Dict[str, Any]) -> str:
    """Extract assistant text from SSE/conduit events.
    Supports:
    - Patch: {"o":"patch","v":[{"p":"/message/content/parts/0","o":"append","v":"..."}]}
    - Patch (no top-level o): {"v":[{"p":"/message/content/parts/0","o":"append","v":"..."}]}
    - Delta with message: {"v":{"message":{"author":{"role":"assistant"},"content":{"parts":["..."]}}}}
    - Root single patch: {"p":"/message/content/parts/0","o":"append","v":"chunk"} (streaming frames)
    - Bare continuation: {"v":"fragment"} when ``p`` is absent (delta_encoding follow-ups)
    - Codex / Responses API: {"type":"response.output_text.delta","delta":"..."}
    """
    etype = str(obj.get("type") or "")
    if etype in _RESPONSES_SSE_DELTA_TYPES:
        delta = obj.get("delta")
        if isinstance(delta, str) and delta:
            return delta
        return ""
    if etype in ("response.output_text.done", "response.text.done"):
        # Full snapshot is handled in extract_full_body; avoid double-append in extract().
        return ""
    v = obj.get("v")
    # v as array = patch list (conduit streaming)
    if isinstance(v, list):
        text = _extract_sse_patch_text(v)
        if text:
            return text
    # v as dict = delta with message
    if isinstance(v, dict):
        msg = v.get("message")
        if isinstance(msg, dict) and _message_role(msg) == "assistant":
            if not _skip_chatgpt_assistant_envelope(msg):
                content_text = _extract_content(msg.get("content"))
                if content_text:
                    return content_text

    # Single message envelope
    msg = obj.get("message")
    if isinstance(msg, dict) and _message_role(msg) == "assistant":
        if not _skip_chatgpt_assistant_envelope(msg):
            content_text = _extract_content(msg.get("content"))
            if content_text:
                return content_text

    root = _extract_root_parts_delta_text(obj)
    if root:
        return root

    # Continuation frames: {"v": " next_tokens"} with no JSON Pointer ``p`` (see user SSE dumps).
    if isinstance(v, str) and v:
        p = obj.get("p")
        if p is None or p == "":
            return v

    return ""


def _is_stream_finished(obj: Dict[str, Any]) -> bool:
    """Detect finished_successfully in a conduit delta, signalling the response stream is complete.
    Works for both patch-list format and full-message delta format.
    """
    etype = str(obj.get("type") or "")
    if etype in _RESPONSES_SSE_DONE_TYPES:
        return True
    v = obj.get("v")
    if isinstance(v, list):
        for item in v:
            if not isinstance(item, dict):
                continue
            if (item.get("p") == "/message/status"
                    and item.get("v") == "finished_successfully"):
                return True
    elif isinstance(v, dict):
        msg = v.get("message")
        if isinstance(msg, dict):
            if (msg.get("status") == "finished_successfully"
                    and _message_role(msg) == "assistant"):
                return not _skip_chatgpt_assistant_envelope(msg)
    return False


def _extract_conversation_id(obj: Dict[str, Any]) -> str:
    conversation_id = obj.get("conversation_id")
    if isinstance(conversation_id, str) and conversation_id:
        return conversation_id
    response_id = obj.get("response_id")
    if isinstance(response_id, str) and response_id:
        return response_id
    resp = obj.get("response")
    if isinstance(resp, dict):
        rid = resp.get("id") or resp.get("response_id")
        if isinstance(rid, str) and rid:
            return rid
    v = obj.get("v")
    if isinstance(v, dict):
        conversation_id = v.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            return conversation_id
    return ""


def _extract_sse_message_id(obj: Dict[str, Any]) -> str:
    # message marker envelope
    message_id = obj.get("message_id")
    if isinstance(message_id, str) and message_id:
        return message_id

    # input message envelope
    input_message = obj.get("input_message")
    if isinstance(input_message, dict):
        message_id = input_message.get("id")
        if isinstance(message_id, str) and message_id:
            return message_id

    # delta envelope
    v = obj.get("v")
    if isinstance(v, dict):
        msg = v.get("message")
        if isinstance(msg, dict):
            message_id = msg.get("id")
            if isinstance(message_id, str) and message_id:
                return message_id

    return ""


def _extract_parent_message_id(obj: Dict[str, Any]) -> str:
    for key in ("parent_message_id", "parent_id", "parentMessageId"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    input_message = obj.get("input_message")
    if isinstance(input_message, dict):
        for key in ("parent_message_id", "parent_id", "parentMessageId"):
            value = input_message.get(key)
            if isinstance(value, str) and value:
                return value
    v = obj.get("v")
    if isinstance(v, dict):
        for key in ("parent_message_id", "parent_id", "parentMessageId"):
            value = v.get(key)
            if isinstance(value, str) and value:
                return value
        msg = v.get("message")
        if isinstance(msg, dict):
            for key in ("parent_message_id", "parent_id", "parentMessageId"):
                value = msg.get(key)
                if isinstance(value, str) and value:
                    return value
    msg = obj.get("message")
    if isinstance(msg, dict):
        for key in ("parent_message_id", "parent_id", "parentMessageId"):
            value = msg.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _extract_request_conversation_id(obj: Dict[str, Any]) -> str:
    for key in ("conversation_id", "session_id", "previous_response_id", "response_id"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _extract_request_message_id(obj: Dict[str, Any]) -> str:
    value = obj.get("message_id")
    if isinstance(value, str) and value:
        return value
    messages = obj.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            value = msg.get("id")
            if isinstance(value, str) and value:
                return value
    return ""


def _extract_request_parent_message_id(obj: Dict[str, Any]) -> str:
    for key in ("parent_message_id", "parent_id", "parentMessageId"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    input_message = obj.get("input_message")
    if isinstance(input_message, dict):
        for key in ("parent_message_id", "parent_id", "parentMessageId"):
            value = input_message.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _resolve_room_key(
    conversation_id: str,
    parent_message_id: str,
    message_id: str,
    req: Optional[Dict[str, Any]] = None,
) -> str:
    if conversation_id:
        room_key = _ROOM_BY_CONVERSATION.get(conversation_id)
        if not room_key:
            for token in (parent_message_id, message_id):
                if token and token in _ROOM_BY_MESSAGE:
                    room_key = _ROOM_BY_MESSAGE[token]
                    break
        if not room_key:
            room_key = f"conv:{conversation_id}"
            _ROOM_BY_CONVERSATION[conversation_id] = room_key
        else:
            _ROOM_BY_CONVERSATION[conversation_id] = room_key
        return room_key

    for token in (parent_message_id, message_id):
        if token and token in _ROOM_BY_MESSAGE:
            return _ROOM_BY_MESSAGE[token]

    if isinstance(req, dict):
        room_key = str(req.get("_chatgpt_room_key") or "")
        if room_key:
            return room_key

    if len(_ACTIVE_TURN_BY_ROOM) == 1:
        return next(iter(_ACTIVE_TURN_BY_ROOM))
    return _SSE_FALLBACK_KEY


def _bind_room_hints(
    room_key: str,
    conversation_id: str,
    parent_message_id: str,
    message_id: str,
) -> None:
    if not room_key:
        return
    if conversation_id:
        _ROOM_BY_CONVERSATION[conversation_id] = room_key
    if message_id:
        _ROOM_BY_MESSAGE[message_id] = room_key
    # 가장 정석적인 방어 코드
    if parent_message_id and parent_message_id not in {"client-created-root", "null", "root", "undefined", "none"}:
        # 진짜 고유한 ID일 때만 장부에 기록
        if parent_message_id not in _ROOM_BY_MESSAGE:
            _ROOM_BY_MESSAGE[parent_message_id] = room_key


def _resolve_turn_key(room_key: str, message_id: str) -> str:
    if message_id:
        return f"msg:{message_id}"

    existing = _ACTIVE_TURN_BY_ROOM.get(room_key)
    if existing:
        return existing
    if len(_SSE_RESPONSE_BUFFER) == 1:
        return next(iter(_SSE_RESPONSE_BUFFER))
    if len(_SSE_RESPONSE_META) == 1:
        return next(iter(_SSE_RESPONSE_META))
    if room_key and room_key != _SSE_FALLBACK_KEY:
        return f"pending:{room_key}"
    return _SSE_FALLBACK_KEY


def _migrate_turn_state(old_key: str, new_key: str) -> None:
    if not old_key or not new_key or old_key == new_key:
        return
    if old_key in _SSE_RESPONSE_BUFFER and new_key not in _SSE_RESPONSE_BUFFER:
        _SSE_RESPONSE_BUFFER[new_key] = _SSE_RESPONSE_BUFFER.pop(old_key)
    if old_key in _SSE_RESPONSE_META and new_key not in _SSE_RESPONSE_META:
        _SSE_RESPONSE_META[new_key] = _SSE_RESPONSE_META.pop(old_key)
    if old_key in _SSE_RESPONSE_OVERRIDE_EMITTED and new_key not in _SSE_RESPONSE_OVERRIDE_EMITTED:
        _SSE_RESPONSE_OVERRIDE_EMITTED[new_key] = _SSE_RESPONSE_OVERRIDE_EMITTED.pop(old_key)
    if old_key in _TURN_TO_ROOM and new_key not in _TURN_TO_ROOM:
        _TURN_TO_ROOM[new_key] = _TURN_TO_ROOM.pop(old_key)


def _update_sse_meta(req: Dict[str, Any], obj: Dict[str, Any]) -> str:
    conversation_id = _extract_conversation_id(obj)
    message_id = _extract_sse_message_id(obj)
    parent_message_id = _extract_parent_message_id(obj)

    room_key = _resolve_room_key(conversation_id, parent_message_id, message_id, req)
    _bind_room_hints(room_key, conversation_id, parent_message_id, message_id)

    key = _resolve_turn_key(room_key, message_id)
    prev_key = _ACTIVE_TURN_BY_ROOM.get(room_key)
    if prev_key and prev_key != key:
        prev_room = _TURN_TO_ROOM.get(prev_key, "")
        # Same stream: server sent conversation_id or message_id late → migrate so one key has buffer/session.
        same_stream = (
            room_key.startswith("conv:") and (prev_room == _SSE_FALLBACK_KEY or prev_room.startswith("pending:"))
        ) or (prev_key.startswith("pending:") and key.startswith("msg:"))
        if same_stream:
            _migrate_turn_state(prev_key, key)
        else:
            # New turn in same room (or different conversation): do not migrate; [DONE] will pop prev_key first.
            _PENDING_DONE_KEYS.append(prev_key)
    _ACTIVE_TURN_BY_ROOM[room_key] = key
    _TURN_TO_ROOM[key] = room_key
    if key not in _STREAM_KEY_ORDER:
        _STREAM_KEY_ORDER.append(key)

    prev = _SSE_RESPONSE_META.get(key, {})
    session_id = conversation_id
    if not session_id and room_key.startswith("conv:"):
        session_id = room_key[5:]
    if not session_id:
        session_id = str(prev.get("session_id") or "")

    meta = {
        "message_id": str(prev.get("message_id") or ""),
        "session_id": session_id,
    }
    if message_id:
        meta["message_id"] = message_id
    _SSE_RESPONSE_META[key] = meta

    req["_chatgpt_stream_key"] = key
    req["_chatgpt_room_key"] = room_key
    req["_chatgpt_session_id"] = session_id
    return key


def _resolve_done_stream_key(req: Dict[str, Any]) -> str:
    stream_key = str(req.get("_chatgpt_stream_key") or "")
    if stream_key and (stream_key in _SSE_RESPONSE_BUFFER or stream_key in _SSE_RESPONSE_META):
        return stream_key
    if len(_ACTIVE_TURN_BY_ROOM) == 1:
        return next(iter(_ACTIVE_TURN_BY_ROOM.values()))
    if len(_SSE_RESPONSE_BUFFER) == 1:
        return next(iter(_SSE_RESPONSE_BUFFER))
    if len(_SSE_RESPONSE_META) == 1:
        return next(iter(_SSE_RESPONSE_META))
    return ""


def _peek_done_key(req: Dict[str, Any]) -> str:
    """Key that would be used for the next [DONE] (same as extract() logic, no mutation)."""
    for k in _PENDING_DONE_KEYS:
        if k in _SSE_RESPONSE_BUFFER:
            return k
    for k in _STREAM_KEY_ORDER:
        if k in _SSE_RESPONSE_BUFFER:
            return k
    return _resolve_done_stream_key(req)


def _set_output_ctx(req: Dict[str, Any], key: str) -> None:
    if key in _STREAM_KEY_ORDER:
        _STREAM_KEY_ORDER.remove(key)
    meta = _SSE_RESPONSE_META.pop(key, {})
    room_key = _TURN_TO_ROOM.pop(key, "")
    if room_key and _ACTIVE_TURN_BY_ROOM.get(room_key) == key:
        _ACTIVE_TURN_BY_ROOM.pop(room_key, None)

    session_id = str(meta.get("session_id") or "")
    if not session_id and room_key.startswith("conv:"):
        session_id = room_key[5:]
    if not session_id and room_key and room_key != _SSE_FALLBACK_KEY:
        session_id = room_key
    message_id = str(meta.get("message_id") or "")
    if not message_id and key.startswith("msg:"):
        message_id = key[4:]

    req["_chatgpt_output_session_id"] = session_id
    req["_chatgpt_output_message_id"] = message_id
    req["_chatgpt_session_id"] = session_id


def _set_extract_final(req: Dict[str, Any], is_final: bool) -> None:
    req["_chatgpt_extract_final"] = bool(is_final)


def _message_role(msg: Dict[str, Any]) -> str:
    role = msg.get("role")
    if isinstance(role, str) and role:
        return role.lower()
    author = msg.get("author")
    if isinstance(author, dict):
        author_role = author.get("role")
        if isinstance(author_role, str) and author_role:
            return author_role.lower()
    return ""


def _mutate_message_content(msg: Dict[str, Any], forced_text: str) -> int:
    changed = 0
    content = msg.get("content")

    if isinstance(content, str):
        if content != forced_text:
            msg["content"] = forced_text
            changed += 1
        return changed

    if isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            new_parts = []
            for part in parts:
                if isinstance(part, str):
                    new_parts.append(forced_text)
                    if part != forced_text:
                        changed += 1
                elif isinstance(part, dict):
                    p = dict(part)
                    if isinstance(p.get("text"), str):
                        if p["text"] != forced_text:
                            changed += 1
                        p["text"] = forced_text
                    if isinstance(p.get("content"), str):
                        if p["content"] != forced_text:
                            changed += 1
                        p["content"] = forced_text
                    new_parts.append(p)
                else:
                    new_parts.append(part)
            content["parts"] = new_parts
        if isinstance(content.get("text"), str):
            if content["text"] != forced_text:
                changed += 1
            content["text"] = forced_text
        if isinstance(content.get("content"), str):
            if content["content"] != forced_text:
                changed += 1
            content["content"] = forced_text
        return changed

    if isinstance(content, list):
        new_list = []
        for item in content:
            if isinstance(item, str):
                new_list.append(forced_text)
                if item != forced_text:
                    changed += 1
            elif isinstance(item, dict):
                d = dict(item)
                if isinstance(d.get("text"), str):
                    if d["text"] != forced_text:
                        changed += 1
                    d["text"] = forced_text
                if isinstance(d.get("content"), str):
                    if d["content"] != forced_text:
                        changed += 1
                    d["content"] = forced_text
                new_list.append(d)
            else:
                new_list.append(item)
        msg["content"] = new_list

    return changed


def _rewrite_request_payload(
    payload: bytes,
    forced_text: str,
    allow_empty: bool = False,
) -> Optional[Tuple[bytes, int]]:
    if not allow_empty and not forced_text:
        return None
    try:
        text = _safe_utf8(payload)
        obj = json.loads(text)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None

    had_attachments = False
    top = obj.get("attachments")
    if isinstance(top, list) and len(top) > 0:
        had_attachments = True
    elif isinstance(obj.get("messages"), list):
        for msg in obj["messages"]:
            if not isinstance(msg, dict):
                continue
            meta = msg.get("metadata")
            if isinstance(meta, dict):
                att = meta.get("attachments")
                if isinstance(att, list) and att:
                    had_attachments = True
                    break
            content = msg.get("content")
            if isinstance(content, dict):
                parts = content.get("parts")
                if isinstance(parts, list) and any(
                    isinstance(p, dict)
                    and any(p.get(k) for k in ("file_id", "id", "asset_pointer", "image_url", "mime_type"))
                    for p in parts
                ):
                    had_attachments = True
                    break

    changed = 0
    if isinstance(obj.get("input"), list):
        changed += _mutate_last_codex_user_content(obj, forced_text)
        strip_cnt = strip_request_attachments(obj)
        if changed <= 0 and strip_cnt <= 0:
            return None
        if changed <= 0:
            changed = 1
        encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return encoded, changed

    messages = obj.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = _message_role(msg)
            # Rewrite user prompt messages while leaving assistant/system as-is.
            if role and role != "user":
                continue
            changed += _mutate_message_content(msg, forced_text)

    if changed == 0:
        for key in ("prompt", "input", "query", "text"):
            if isinstance(obj.get(key), str):
                if obj[key] != forced_text:
                    changed += 1
                obj[key] = forced_text

    # prepare-dispatch / anon turns keep the real prompt here; extract() reads it
    # but older rewrite left it intact so BLOCK still reached the model.
    partial = obj.get("partial_query")
    if isinstance(partial, dict):
        changed += _mutate_message_content(partial, forced_text)

    # Image/file-only user turn: parts have no text — replace whole content.
    if changed == 0 and isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict) or _message_role(msg) != "user":
                continue
            msg["content"] = {"content_type": "text", "parts": [forced_text]}
            changed = 1
            break

    strip_cnt = strip_request_attachments(obj)
    if changed <= 0 and strip_cnt <= 0 and not had_attachments:
        return None
    if changed <= 0 and (strip_cnt > 0 or had_attachments):
        changed = 1

    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, changed


# Gemini/Claude와 동일: 안내문·지시 사이 긴 줄바꿈(UI 휴리스틱, 보장 아님).
_BLOCK_SYSINST_VERTICAL_GAP = "\n" * 1


try:
    from block_copy import POLICY_INSTRUCTION_HEADER as _LEAKED_BLOCK_HEADER
except ImportError:
    from core.block_copy import POLICY_INSTRUCTION_HEADER as _LEAKED_BLOCK_HEADER


def _chatgpt_block_prompt(user_prompt: str, system_instruction: str) -> str:
    """Notice first (what the user sees in the chat bubble), instruction last.

    The ``[AIVAX 정책 응답 처리]`` header line is never sent: as the trailing
    text the model copied it as the last line of the answer.
    """
    sys_t = "\n".join(
        line
        for line in (system_instruction or "").splitlines()
        if line.strip() != _LEAKED_BLOCK_HEADER
    ).strip()
    user_t = (user_prompt or "").strip()
    if sys_t and user_t:
        return f"{user_t}\n{_BLOCK_SYSINST_VERTICAL_GAP}{sys_t}"
    return user_t or sys_t


def rewrite_prompt_for_block(
    req: Dict[str, Any],
    payload: bytes,
    user_prompt: str,
    system_instruction: str,
) -> Optional[Tuple[bytes, str]]:
    """BLOCK 전용: 지시문을 안내문 앞에 두고 마지막 user 메시지(또는 prompt)에 넣는다.

    파일만 올린 턴(첨부-only)도 strip_request_attachments 로 업스트림 전달을 막는다.
    """
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "request":
        return None

    combined = _chatgpt_block_prompt(user_prompt, system_instruction)
    if not combined:
        return None

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    if _is_unauth_mweb_updates_path(path):
        encoded = _rewrite_unauth_mweb_form_prompt(payload, combined)
        if encoded is None:
            return None
        return encoded, "adapter:chatgpt_policy_block_sysinstruct"

    # Codex IDE sends the turn as one masked WebSocket text frame. JSON-only
    # rewrite returns None, and the filter then forwards the original prompt.
    if _is_codex_responses_path(path) and unit_type == "ws_frame":
        rewritten_frames = bytearray()
        changed = 0
        saw_text = False
        for opcode, data, original in _iter_ws_frames(payload or b""):
            if opcode not in (0x1, 0x2):
                rewritten_frames += original
                continue
            saw_text = True
            out = _rewrite_request_payload(data, combined, allow_empty=False)
            if out is None:
                rewritten_frames += _encode_ws_text_frame(data, masked=True)
                continue
            encoded, n = out
            changed += n
            rewritten_frames += _encode_ws_text_frame(encoded, masked=True)
        if not saw_text or changed <= 0:
            return None
        return bytes(rewritten_frames), "adapter:chatgpt_policy_block_sysinstruct"

    if not (unit_type == "json_body" or "application/json" in content_type):
        return None

    out = _rewrite_request_payload(payload, combined, allow_empty=False)
    if out is None:
        return None
    encoded, _ = out
    return encoded, "adapter:chatgpt_policy_block_sysinstruct"


def _parse_request_json(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "request":
        return None
    if not (unit_type == "json_body" or "application/json" in content_type):
        return None
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _register_request_context(req: Dict[str, Any], obj: Dict[str, Any]) -> None:
    conversation_id = _extract_request_conversation_id(obj)
    message_id = _extract_request_message_id(obj)
    parent_message_id = _extract_request_parent_message_id(obj)

    room_key = _resolve_room_key(conversation_id, parent_message_id, message_id, req)
    if room_key == _SSE_FALLBACK_KEY:
        seed = conversation_id or message_id or parent_message_id or str(req.get("request_id") or "")
        if seed:
            room_key = f"pending:{seed}"
    _bind_room_hints(room_key, conversation_id, parent_message_id, message_id)

    req["_chatgpt_room_key"] = room_key
    if conversation_id:
        req["_chatgpt_session_id"] = conversation_id


def _extract_partial_query_prompt(obj: Dict[str, Any]) -> str:
    """Prompt from the client_prepare dispatch, which carries no messages[].

    Anonymous turns and prepare-dispatch turns put the submitted text in
    partial_query instead, and those requests do receive a full answer.
    """
    partial = obj.get("partial_query")
    if not isinstance(partial, dict):
        return ""
    if _message_role(partial) not in ("", "user"):
        return ""
    return _extract_content(partial.get("content")) or ""


def _strip_codex_environment_context(text: str) -> str:
    """Drop Codex session metadata and keep the line the person submitted."""
    raw = text or ""
    if "<environment_context" not in raw.lower():
        return raw.strip()
    return _ENVIRONMENT_CONTEXT_RE.sub("", raw).strip()


def _extract_user_visible_prompt(text: str) -> str:
    """Codex desktop wraps the submitted line in My request / userRequest tags."""
    raw = (text or "").strip()
    if not raw:
        return ""
    m = _USER_REQUEST_TAG_RE.search(raw)
    if m and m.group(2).strip():
        return _strip_codex_environment_context(m.group(2))
    m = _MY_REQUEST_HEADING_RE.search(raw)
    if m and m.group(3).strip():
        return _strip_codex_environment_context(m.group(3))
    return _strip_codex_environment_context(raw)


def _replace_user_visible_prompt(text: str, replacement: str) -> Tuple[str, bool]:
    raw = text or ""
    m = _USER_REQUEST_TAG_RE.search(raw)
    if m:
        return f"{m.group(1)}{replacement}{m.group(3)}", True
    m = _MY_REQUEST_HEADING_RE.search(raw)
    if m:
        return f"{m.group(1)}{m.group(2)}{replacement}", True
    if raw != replacement:
        return replacement, True
    return raw, False


def _last_codex_user_message(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    inp = obj.get("input")
    if not isinstance(inp, list):
        return None
    for msg in reversed(inp):
        if not isinstance(msg, dict):
            continue
        mtype = str(msg.get("type") or "message").lower()
        if mtype in ("message", "") and _message_role(msg) == "user":
            return msg
    return None


def _codex_user_text(msg: Dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return _extract_content(content)
    parts: List[str] = []
    for part in content:
        if isinstance(part, str) and part.strip():
            parts.append(part.strip())
            continue
        if not isinstance(part, dict):
            continue
        ptype = str(part.get("type") or "").lower()
        if ptype in ("input_image", "input_file", "file", "image"):
            continue
        for key in ("text", "content", "value"):
            val = part.get(key)
            if isinstance(val, str) and val.strip():
                if _is_codex_media_label(val):
                    break
                parts.append(val.strip())
                break
    return "\n".join(parts).strip()


# Codex labels who started the turn in x-codex-turn-metadata. These are the
# sources the app itself classifies as not a user-authored root turn. The
# ``thread_*``/``*_message`` names are Codex "ephemeral generation" features:
# the client opens a throwaway thread, sends the feature's own instruction
# block as the user message, and parses a JSON answer back.
_CODEX_APP_THREAD_SOURCES = frozenset(
    {
        "system",
        "title",
        "subagent",
        "guardian_review",
        "memory_consolidation",
        "thread_title",
        "thread_description",
        "commit_message",
        "pull_request_message",
        "commit_pull_request_message",
    }
)
_CODEX_NON_TURN_REQUEST_KINDS = frozenset({"compaction", "memory"})
# ``text.format.type`` values that pin the answer to a fixed JSON schema.
_CODEX_SCHEMA_FORMAT_TYPES = frozenset({"json_schema", "internal_json_schema"})


def _json_object(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _codex_turn_metadata_documents(
    obj: Optional[Dict[str, Any]], req: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Turn identity Codex attaches to the request, header first then body.

    HTTP carries it in ``x-codex-turn-metadata``. WebSocket ``response.create``
    carries the same JSON at ``client_metadata["x-codex-turn-metadata"]``, with
    a flat ``client_metadata`` projection beside it. The nested blob is the
    source of truth; flat keys are only a fallback.
    """
    docs: List[Dict[str, Any]] = []
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    header = _json_object(meta.get("x-codex-turn-metadata"))
    if header:
        docs.append(header)
    if not isinstance(obj, dict):
        return docs
    client = obj.get("client_metadata")
    if isinstance(client, dict):
        nested = _json_object(client.get("x-codex-turn-metadata"))
        if nested:
            docs.append(nested)
        docs.append(client)
    top = _json_object(obj.get("x-codex-turn-metadata"))
    if top:
        docs.append(top)
    return docs


def _codex_meta_value(docs: List[Dict[str, Any]], key: str) -> str:
    for doc in docs:
        value = doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _codex_thread_source_is_app(source: str) -> bool:
    source = (source or "").strip().lower()
    return source in _CODEX_APP_THREAD_SOURCES or source.startswith("ambient")


def _codex_output_format_type(obj: Optional[Dict[str, Any]]) -> str:
    """``text.format.type`` the client pinned for this turn, if any.

    Responses carries it at ``text.format``; a few builds still send the chat
    spelling ``response_format``. ``response.create`` keeps these at the top
    level, but accept a nested ``response`` object as well.
    """
    if not isinstance(obj, dict):
        return ""
    for container in (obj, obj.get("response")):
        if not isinstance(container, dict):
            continue
        text = container.get("text")
        fmt = text.get("format") if isinstance(text, dict) else None
        if not isinstance(fmt, dict):
            fmt = container.get("response_format")
        if not isinstance(fmt, dict):
            continue
        ftype = str(fmt.get("type") or "").strip().lower()
        if ftype:
            return ftype
    return ""


def _codex_turn_pins_output_schema(obj: Optional[Dict[str, Any]]) -> bool:
    """True when the turn demands a JSON answer matching a fixed schema.

    Codex asks for a schema only on its own generation calls (thread title and
    description, git commit / PR message). Those send the feature's prompt
    template as the user message, so the text in ``input`` is the client's, not
    the user's. A line somebody typed is always answered as free text, so this
    stays correct for generation features Codex has not shipped yet.
    """
    return _codex_output_format_type(obj) in _CODEX_SCHEMA_FORMAT_TYPES


def _codex_turn_is_app_initiated(obj: Optional[Dict[str, Any]], req: Dict[str, Any]) -> bool:
    """True when Codex started this turn. A missing label stays a user turn."""
    if _codex_turn_pins_output_schema(obj):
        return True
    docs = _codex_turn_metadata_documents(obj, req)
    if _codex_meta_value(docs, "request_kind") in _CODEX_NON_TURN_REQUEST_KINDS:
        return True
    # ``turn_trigger`` repeats the source on generation turns and is the only
    # label present when the ephemeral thread was forked from the user's.
    return any(
        _codex_thread_source_is_app(_codex_meta_value(docs, key))
        for key in ("thread_source", "turn_trigger")
    )


def _extract_codex_user_prompt(obj: Dict[str, Any]) -> str:
    msg = _last_codex_user_message(obj)
    if not isinstance(msg, dict):
        return ""
    return _extract_user_visible_prompt(_codex_user_text(msg))


def _codex_last_user_has_inline_file(obj: Dict[str, Any]) -> bool:
    msg = _last_codex_user_message(obj)
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    for part in content:
        if not isinstance(part, dict):
            continue
        if str(part.get("type") or "").lower() in ("input_image", "input_file", "file", "image"):
            return True
    return False


def _codex_is_tool_followup(obj: Dict[str, Any]) -> bool:
    inp = obj.get("input")
    if not isinstance(inp, list) or not inp:
        return False
    last = inp[-1]
    if not isinstance(last, dict):
        return False
    return str(last.get("type") or "").lower() in _CODEX_TOOL_ITEM_TYPES


def _mutate_last_codex_user_content(obj: Dict[str, Any], replacement: str) -> int:
    msg = _last_codex_user_message(obj)
    if not isinstance(msg, dict):
        return 0
    content = msg.get("content")
    if isinstance(content, str):
        new_c, ok = _replace_user_visible_prompt(content, replacement)
        if ok:
            msg["content"] = new_c
            return 1
        return 0
    if not isinstance(content, list):
        msg["content"] = [{"type": "input_text", "text": replacement}]
        return 1
    changed = 0
    for idx, part in enumerate(content):
        if not isinstance(part, dict):
            continue
        ptype = str(part.get("type") or "").lower()
        if ptype not in ("input_text", "text", ""):
            continue
        raw = str(part.get("text") or part.get("content") or "")
        if "text" not in part and "content" not in part:
            continue
        new_t, ok = _replace_user_visible_prompt(raw, replacement)
        if not ok:
            continue
        key = "text" if "text" in part else "content"
        part[key] = new_t
        content[idx] = part
        changed += 1
    if changed:
        msg["content"] = content
        return changed
    if _codex_last_user_has_inline_file(obj):
        msg["content"] = [{"type": "input_text", "text": replacement}]
        return 1
    return 0


def _responses_event_is_tool_call(event_type: str, obj: Dict[str, Any]) -> bool:
    """A Responses event that invokes a tool instead of speaking to the user."""
    etype = (event_type or "").lower()
    if etype.startswith(
        (
            "response.custom_tool_call",
            "response.function_call",
            "response.local_shell_call",
            "response.tool_search_call",
            "response.web_search_call",
            "response.file_search_call",
            "response.computer_call",
        )
    ):
        return True
    if etype not in ("response.output_item.added", "response.output_item.done"):
        return False
    item = obj.get("item")
    if not isinstance(item, dict):
        return False
    return str(item.get("type") or "").lower() in (
        "custom_tool_call",
        "function_call",
        "local_shell_call",
        "tool_search_call",
        "web_search_call",
        "file_search_call",
        "computer_call",
    ) or _responses_item_waits_on_client(item)


def _responses_item_waits_on_client(item: Dict[str, Any]) -> bool:
    """True for a client tool call. Server-side tools have no ``call_id``.

    A returned tool result (``function_call_output``) also carries ``call_id``,
    and that item means the result is already in hand, not that the client
    still has to run something.
    """
    kind = str(item.get("type") or "").lower()
    if kind.endswith("_output"):
        return False
    call_id = item.get("call_id")
    return isinstance(call_id, str) and bool(call_id.strip())


def _responses_output_waits_on_client(obj: Dict[str, Any]) -> bool:
    """True when this response is paused until the client returns a tool result.

    ``response.completed`` is the whole model step: its ``output`` lists every
    item, including a tool call that was streamed in an earlier frame. A
    ``call_id`` is the protocol marker for "the client must answer this". The
    status sentence that sits beside that item is not the answer the user
    reads; that answer is the next response, after the tool result. A
    server-side tool (web search, image generation) has no ``call_id``, and
    the message in the same response is the answer.
    """
    resp = obj.get("response") if isinstance(obj.get("response"), dict) else None
    if not isinstance(resp, dict):
        return False
    output = resp.get("output")
    if not isinstance(output, list):
        return False
    return any(
        isinstance(row, dict) and _responses_item_waits_on_client(row) for row in output
    )


def _extract_responses_snapshot_text(obj: Dict[str, Any]) -> str:
    etype = str(obj.get("type") or "")
    if etype in ("response.output_text.done", "response.text.done"):
        text = obj.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    if etype not in ("response.completed", "response.done"):
        return ""
    resp = obj.get("response") if isinstance(obj.get("response"), dict) else None
    if not isinstance(resp, dict):
        return ""
    output = resp.get("output")
    if not isinstance(output, list):
        return ""
    parts: List[str] = []
    for row in output:
        if not isinstance(row, dict):
            continue
        content = row.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"]:
                    parts.append(c["text"])
        elif isinstance(row.get("text"), str) and row["text"]:
            parts.append(row["text"])
    return "".join(parts).strip()


def _extract_latest_user_prompt(obj: Dict[str, Any]) -> str:
    messages = obj.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            if _message_role(msg) != "user":
                continue
            text = _extract_content(msg.get("content"))
            if text:
                return text
    codex_text = _extract_codex_user_prompt(obj)
    if codex_text:
        return codex_text
    partial_text = _extract_partial_query_prompt(obj)
    if partial_text:
        return partial_text
    for key in ("prompt", "input", "query", "text"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_message_id(obj: Dict[str, Any]) -> str:
    for key in ("message_id", "parent_message_id"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    messages = obj.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            value = msg.get("id")
            if isinstance(value, str) and value:
                return value
    return ""


def is_chatgpt_conversation_turn_path(path: str) -> bool:
    """True for real chat-turn POSTs only (login + anon).

    Allow:
      /backend-api/f/conversation
      /backend-anon/f/conversation
      /backend-api/anon/conversation
      /backend-anon/conversation
      /unauth-mweb/conversation/updates   # lightweight anon (form `prompt`)
      /backend-api/codex/responses       # ChatGPT login, Codex desktop and IDE plugin
      /v1/responses                      # api.openai.com (API key or workspace routing)
    Deny:
      /conversations, /prepare, /init, /resume, /runtime, /{id}/..., ces/sentinel/telemetry, …
    """
    path_lower = (path or "").split("?", 1)[0].lower()
    if _is_codex_responses_path(path_lower):
        return True
    cands = (
        "/backend-api/f/conversation",
        "/backend-anon/f/conversation",
        "/backend-api/anon/conversation",
        "/backend-anon/conversation",
        "/unauth-mweb/conversation/updates",
    )
    for pref in cands:
        i = path_lower.find(pref)
        if i < 0:
            continue
        after = path_lower[i + len(pref) : i + len(pref) + 1]
        if after == "s":  # /conversations
            continue
        if after and after != "?":  # /prepare, /init, /resume, /{id}/...
            continue
        return True
    return False


def _is_codex_responses_path(path: str) -> bool:
    """User turn on the Responses API, HTTP or WebSocket.

    Codex in VS Code and Cursor uses ``wss://chatgpt.com/backend-api/codex/responses``
    with a ChatGPT login, and ``wss://api.openai.com/v1/responses`` when the
    account is routed there or an API key is configured. A suffix such as
    ``/v1/responses/{id}`` is not a turn.
    """
    path_lower = (path or "").split("?", 1)[0].lower()
    for pref in ("/backend-api/codex/responses", "/v1/responses"):
        i = path_lower.find(pref)
        if i < 0:
            continue
        after = path_lower[i + len(pref) : i + len(pref) + 1]
        if (not after) or after == "?":
            return True
    return False


# Codex Desktop's cloud app-server. The socket is GET / on this host, so the
# responses-path allowlist never matches and the turn was not logged.
_CODEX_DESKTOP_HOST = "codex-cloud-backend."
_CODEX_DESKTOP_TURN_METHODS = frozenset(
    {
        "turn/start",
        "turn/steer",
        "thread/start",
        "thread/prompt",
        "conversation/turn",
    }
)


def _is_codex_desktop_ws(req: Dict[str, Any]) -> bool:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").split(":", 1)[0].lower()
    if _CODEX_DESKTOP_HOST not in host:
        return False
    path = str(meta.get("path") or "").split("?", 1)[0].rstrip("/") or "/"
    return path == "/"


def _codex_desktop_collect_text(value: Any, texts: List[str]) -> None:
    """User-authored strings only. Developer and assistant text stays out."""
    if isinstance(value, list):
        for item in value:
            _codex_desktop_collect_text(item, texts)
        return
    if not isinstance(value, dict):
        return
    role = str(value.get("role") or "").strip().lower()
    if role in ("assistant", "system", "developer"):
        return
    kind = str(value.get("type") or "").strip().lower()
    if role == "user" or kind in ("text", "input_text", "message", ""):
        for key in ("text", "content", "prompt"):
            child = value.get(key)
            if isinstance(child, str) and child.strip() and key != "content":
                texts.append(child.strip())
                return
            if isinstance(child, (dict, list)):
                _codex_desktop_collect_text(child, texts)
                return
    for key in ("input", "items", "content"):
        child = value.get(key)
        if isinstance(child, (dict, list)):
            _codex_desktop_collect_text(child, texts)


def _codex_desktop_user_text(obj: Dict[str, Any]) -> str:
    method = str(obj.get("method") or "").strip()
    if method and method not in _CODEX_DESKTOP_TURN_METHODS:
        return ""
    params = obj.get("params") if isinstance(obj.get("params"), dict) else obj
    if not isinstance(params, dict):
        return ""
    if not method:
        # Bare JSON without a turn method is telemetry, not a prompt.
        return ""
    texts: List[str] = []
    _codex_desktop_collect_text(params, texts)
    # De-dupe while keeping order. Nested message+text can be visited twice.
    seen = set()
    unique: List[str] = []
    for text in texts:
        if text not in seen:
            seen.add(text)
            unique.append(text)
    return "\n".join(unique).strip()


def _codex_desktop_frame_label(payload: bytes) -> str:
    """Opcode and event name only. Never the prompt text."""
    labels: List[str] = []
    for opcode, data, _original in _iter_ws_frames(payload or b""):
        if opcode != 1:
            labels.append(f"op{opcode}:{len(data)}")
            continue
        piece = _safe_utf8(data).strip()
        if not piece or piece[0] not in "{[":
            labels.append(f"text:{len(data)}")
            continue
        try:
            obj = json.loads(piece)
        except Exception:
            labels.append("badjson")
            continue
        if isinstance(obj, dict):
            ev = obj.get("event") or obj.get("method") or obj.get("type")
            labels.append(str(ev)[:80] if isinstance(ev, str) and ev else "dict")
        else:
            labels.append(type(obj).__name__)
        if len(labels) >= 4:
            break
    return ",".join(labels)


def _extract_codex_desktop_prompt(payload: bytes) -> str:
    found: List[str] = []
    for opcode, data, _original in _iter_ws_frames(payload or b""):
        if opcode != 1:
            continue
        piece = _safe_utf8(data).strip()
        if not piece or piece[0] not in "{[":
            continue
        try:
            obj = json.loads(piece)
        except Exception:
            continue
        if isinstance(obj, dict):
            text = _codex_desktop_user_text(obj)
            if text:
                found.append(text)
    return "\n".join(found).strip()


def _codex_json_objects(payload: bytes) -> List[Dict[str, Any]]:
    """JSON objects from a Responses body or from WebSocket text frames."""
    raw = payload or b""
    text = _safe_utf8(raw).lstrip()
    if text.startswith("{") or text.startswith("["):
        blobs: List[bytes] = [raw]
    else:
        frames = _parse_ws_payloads(raw)
        blobs = frames if frames else []
    out: List[Dict[str, Any]] = []
    for blob in blobs:
        piece = _safe_utf8(blob).strip()
        if not piece or piece[0] not in "{[":
            continue
        try:
            obj = json.loads(piece)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
        elif isinstance(obj, list):
            out.extend(item for item in obj if isinstance(item, dict))
    return out


def _codex_turn_object(payload: bytes) -> Optional[Dict[str, Any]]:
    """The response.create (or HTTP input) object inside a Codex turn."""
    fallback: Optional[Dict[str, Any]] = None
    for obj in _codex_json_objects(payload):
        kind = str(obj.get("type") or "")
        if kind == "response.create" or isinstance(obj.get("input"), list):
            return obj
        if fallback is None:
            fallback = obj
    return fallback


_CODEX_WS_TEXT: Dict[str, str] = {}
_CODEX_WS_ORDER: List[str] = []
# response_id values whose step already asked the client to run a tool.
# The completed frame often arrives on its own, after the call frame, so the
# verdict has to outlive the frame that first showed the call_id.
_CODEX_TOOL_SEEN: Dict[str, bool] = {}
_CODEX_TOOL_SEEN_ORDER: List[str] = []
_RESPONSES_RESPONSE_END_TYPES = frozenset({"response.completed", "response.done"})
# Long-lived Codex socket: the upgrade id is shared by every turn. Remember the
# prompt frame id so the later answer merges with that turn, not the previous one.
_CODEX_TURN_MESSAGE: Dict[str, str] = {}
_CODEX_TURN_PROMPT: Dict[str, str] = {}
_CODEX_TURN_ORDER: List[str] = []
# Whether the turn in flight on that socket was started by Codex itself. A
# WebSocket answer frame carries no turn metadata of its own, so the verdict
# taken on the request frame has to be remembered for the answer.
_CODEX_APP_TURN: Dict[str, bool] = {}
_CODEX_APP_TURN_ORDER: List[str] = []
# Assistant message that arrived in the same step as a client tool call.
# The pipeline keeps one output per prompt, so this stays unpublished until the
# turn settles. A later tool-free answer replaces it. If none arrives, the
# engine publishes this text so the prompt is not left blank.
_CODEX_HELD_ANSWER: Dict[str, str] = {}
_CODEX_HELD_ANSWER_ORDER: List[str] = []


def _codex_response_stream_key(obj: Dict[str, Any], req: Dict[str, Any]) -> str:
    rid = obj.get("response_id")
    if isinstance(rid, str) and rid.strip():
        return rid.strip()
    resp = obj.get("response")
    if isinstance(resp, dict):
        inner = resp.get("id")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    client = str(meta.get("client_ip") or "").strip()
    return f"codex:{client}" if client else "codex"


def _remember_codex_ws_delta(key: str, chunk: str) -> None:
    if not key or not chunk:
        return
    if key not in _CODEX_WS_TEXT:
        _CODEX_WS_ORDER.append(key)
        while len(_CODEX_WS_ORDER) > 256:
            _CODEX_WS_TEXT.pop(_CODEX_WS_ORDER.pop(0), None)
    _CODEX_WS_TEXT[key] = _CODEX_WS_TEXT.get(key, "") + chunk


def _drop_codex_ws_delta(key: str) -> str:
    text = _CODEX_WS_TEXT.pop(key, "")
    if key in _CODEX_WS_ORDER:
        _CODEX_WS_ORDER.remove(key)
    return text


def _replace_codex_ws_text(key: str, text: str) -> None:
    """Store a full text snapshot. Later deltas of the same response append."""
    if not key or not text or key.startswith("codex:"):
        return
    if key not in _CODEX_WS_TEXT:
        _CODEX_WS_ORDER.append(key)
        while len(_CODEX_WS_ORDER) > 256:
            _CODEX_WS_TEXT.pop(_CODEX_WS_ORDER.pop(0), None)
    _CODEX_WS_TEXT[key] = text


def _remember_codex_tool_response(key: str) -> None:
    """This response_id has asked the client to run a tool."""
    if not key or key.startswith("codex:"):
        return
    if key in _CODEX_TOOL_SEEN:
        return
    _CODEX_TOOL_SEEN[key] = True
    _CODEX_TOOL_SEEN_ORDER.append(key)
    while len(_CODEX_TOOL_SEEN_ORDER) > 256:
        _CODEX_TOOL_SEEN.pop(_CODEX_TOOL_SEEN_ORDER.pop(0), None)


def _take_codex_tool_response(key: str) -> bool:
    if not key or key not in _CODEX_TOOL_SEEN:
        return False
    _CODEX_TOOL_SEEN.pop(key, None)
    if key in _CODEX_TOOL_SEEN_ORDER:
        _CODEX_TOOL_SEEN_ORDER.remove(key)
    return True


def _remember_codex_prompt_message(req: Dict[str, Any], prompt: str = "") -> None:
    """Bind this prompt's own frame id. Do not collapse it to the socket id."""
    rid = str(req.get("request_id") or "").strip()
    if "-ws-" not in rid:
        return
    conn = rid.rsplit("-ws-", 1)[0].strip()
    if not conn:
        return
    if conn not in _CODEX_TURN_MESSAGE:
        _CODEX_TURN_ORDER.append(conn)
        while len(_CODEX_TURN_ORDER) > 256:
            dropped = _CODEX_TURN_ORDER.pop(0)
            _CODEX_TURN_MESSAGE.pop(dropped, None)
            _CODEX_TURN_PROMPT.pop(dropped, None)
    _CODEX_TURN_MESSAGE[conn] = rid
    text = (prompt or "").strip()
    if text:
        _CODEX_TURN_PROMPT[conn] = text
    req["_chatgpt_ui_message_id"] = rid


def _codex_connection_id(request_id: str) -> str:
    rid = str(request_id or "").strip()
    if "-ws-" in rid:
        return rid.rsplit("-ws-", 1)[0].strip()
    return rid


def _codex_prompt_message_for_connection(request_id: str) -> str:
    rid = str(request_id or "").strip()
    if not rid:
        return ""
    if "-ws-" in rid:
        return rid
    return str(_CODEX_TURN_MESSAGE.get(rid) or "")


def _codex_saved_prompt(request_id: str) -> str:
    return str(_CODEX_TURN_PROMPT.get(_codex_connection_id(request_id)) or "")


def _codex_saved_message_id(request_id: str) -> str:
    return str(_CODEX_TURN_MESSAGE.get(_codex_connection_id(request_id)) or "")


def _remember_codex_app_turn(req: Dict[str, Any], app_turn: bool) -> None:
    """Record the verdict for the turn now in flight on this socket."""
    conn = _codex_connection_id(str(req.get("request_id") or ""))
    if not conn:
        return
    if conn not in _CODEX_APP_TURN:
        _CODEX_APP_TURN_ORDER.append(conn)
        while len(_CODEX_APP_TURN_ORDER) > 256:
            _CODEX_APP_TURN.pop(_CODEX_APP_TURN_ORDER.pop(0), None)
    _CODEX_APP_TURN[conn] = app_turn


def _codex_connection_in_app_turn(req: Dict[str, Any]) -> bool:
    conn = _codex_connection_id(str(req.get("request_id") or ""))
    return bool(conn) and _CODEX_APP_TURN.get(conn, False)


def _codex_answer_message_id(req: Dict[str, Any]) -> str:
    """Prompt row this answer must join. Falls back to the socket id."""
    rid = str(req.get("request_id") or "").strip()
    bound = _codex_prompt_message_for_connection(rid)
    if bound:
        return bound
    return _codex_connection_id(rid) or rid


def _hold_codex_answer(message_id: str, text: str) -> None:
    mid = (message_id or "").strip()
    visible = (text or "").strip()
    if not mid or not visible:
        return
    if mid not in _CODEX_HELD_ANSWER:
        _CODEX_HELD_ANSWER_ORDER.append(mid)
        while len(_CODEX_HELD_ANSWER_ORDER) > 256:
            _CODEX_HELD_ANSWER.pop(_CODEX_HELD_ANSWER_ORDER.pop(0), None)
    _CODEX_HELD_ANSWER[mid] = visible


def clear_codex_held_answer(message_id: str) -> None:
    mid = (message_id or "").strip()
    if not mid:
        return
    _CODEX_HELD_ANSWER.pop(mid, None)
    if mid in _CODEX_HELD_ANSWER_ORDER:
        _CODEX_HELD_ANSWER_ORDER.remove(mid)


def take_codex_held_answer(message_id: str, expected: str = "") -> str:
    """Return the held answer if it is still the one the caller scheduled."""
    mid = (message_id or "").strip()
    if not mid:
        return ""
    current = _CODEX_HELD_ANSWER.get(mid, "")
    if not current:
        return ""
    if expected and current != expected:
        return ""
    clear_codex_held_answer(mid)
    return current


def _extract_codex_request_prompt(req: Dict[str, Any], payload: bytes) -> str:
    """User text from an HTTP body or a WebSocket response.create frame."""
    obj = _codex_turn_object(payload)
    # Ping/pong frames, prewarm (generate=false), app turns and tool results
    # carry no user text by design; report them as control, not extract_fail.
    app_turn = isinstance(obj, dict) and _codex_turn_is_app_initiated(obj, req)
    if isinstance(obj, dict) and (obj.get("input") is not None or app_turn):
        _remember_codex_app_turn(req, app_turn)
    if (
        not isinstance(obj, dict)
        or obj.get("generate") is False
        or app_turn
        or _codex_is_tool_followup(obj)
    ):
        req["_chatgpt_sse_control_only"] = True
        return ""
    model = obj.get("model")
    if isinstance(model, str) and model.strip():
        req["_llm_model"] = model.strip()
    _register_request_context(req, obj)
    prompt = _extract_codex_user_prompt(obj)
    if prompt:
        _bind_prompt_request(req, obj)
        _remember_codex_prompt_message(req, prompt)
        return prompt
    raw_user = ""
    last = _last_codex_user_message(obj)
    if isinstance(last, dict):
        raw_user = _codex_user_text(last)
    if "<environment_context" in raw_user.lower():
        req["_chatgpt_sse_control_only"] = True
    return ""


def _extract_codex_ws_response(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    """Assistant text from Codex Responses WebSocket frames.

    Each frame is its own policy call. Deltas and ``output_text.done`` are kept
    until ``response.completed``, the event that carries the whole step. A step
    that contains a client tool call (``call_id``) is not published yet: the
    pipeline pairs one input with the first output, so a status line would
    discard the response that follows the tool result. The assistant message
    in that step is held. A later tool-free answer replaces it. If the turn
    ends on the tool step, the engine publishes the held message.
    """
    if _codex_turn_is_app_initiated(None, req) or _codex_connection_in_app_turn(req):
        req["_chatgpt_sse_control_only"] = True
        return None
    objs = _codex_json_objects(payload)
    type_counts: Dict[str, int] = {}
    saw_tool = False
    final = ""
    done = False
    done_key = ""
    for obj in objs:
        etype = str(obj.get("type") or "")
        if etype:
            type_counts[etype] = type_counts.get(etype, 0) + 1
        key = _codex_response_stream_key(obj, req)
        if key and not key.startswith("codex:"):
            req["_chatgpt_session_id"] = key
        if _responses_event_is_tool_call(etype, obj) or _responses_output_waits_on_client(obj):
            saw_tool = True
            _remember_codex_tool_response(key)
        if etype in _RESPONSES_SSE_DELTA_TYPES:
            delta = obj.get("delta")
            if isinstance(delta, str) and delta:
                _remember_codex_ws_delta(key, delta)
        if etype in ("response.output_text.done", "response.text.done"):
            text = obj.get("text")
            if isinstance(text, str) and text.strip():
                _replace_codex_ws_text(key, text.strip())
                final = text.strip()
        if etype in _RESPONSES_RESPONSE_END_TYPES:
            done = True
            done_key = key
            snap = _extract_responses_snapshot_text(obj)
            if snap:
                final = snap
    # Decide only after every event in the frame. The tool call and the
    # completed snapshot travel in separate frames; both belong to one step.
    if done and (saw_tool or _take_codex_tool_response(done_key)):
        _drop_codex_ws_delta(done_key)
        req["_chatgpt_sse_control_only"] = True
        visible = (final or "").strip()
        message_id = _codex_answer_message_id(req)
        if visible and message_id:
            _hold_codex_answer(message_id, visible)
            req["_codex_held_answer"] = visible
            req["_codex_held_message_id"] = message_id
            logger.info(
                "chatgpt codex answer held message_id=%s len=%d types=%s",
                message_id,
                len(visible),
                type_counts,
            )
        return None
    if done:
        buffered = _drop_codex_ws_delta(done_key)
        if not final:
            final = buffered
    if done and (not final) and type_counts:
        req["_chatgpt_sse_control_only"] = True
        return None
    if done and final:
        req["_extract_final"] = True
        turn_message = _codex_answer_message_id(req)
        if turn_message:
            req["_chatgpt_ui_message_id"] = turn_message
            clear_codex_held_answer(turn_message)
        logger.info(
            "chatgpt codex ws answer ok payload_len=%d result_len=%d types=%s",
            len(payload or b""),
            len(final),
            type_counts,
        )
        return final
    req["_extract_final"] = False
    return None


def _is_unauth_mweb_updates_path(path: str) -> bool:
    path_lower = (path or "").split("?", 1)[0].lower()
    pref = "/unauth-mweb/conversation/updates"
    i = path_lower.find(pref)
    if i < 0:
        return False
    after = path_lower[i + len(pref) : i + len(pref) + 1]
    return (not after) or after == "?"


def _extract_unauth_mweb_form_prompt(payload: bytes) -> str:
    """User prompt only from unauth-mweb updates form-urlencoded body.

    Never treat tokens / conversationState / telemetry as prompt text.
    """
    if not payload:
        return ""
    try:
        pairs = parse_qsl(_safe_utf8(payload), keep_blank_values=True)
    except Exception:
        return ""
    for key, value in pairs:
        if key == "prompt" and isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# Anonymous mweb has no JSON turn body, so uploads ride along as JSON strings
# inside form fields. Field names have already changed once, so match on the
# OpenAI file id rather than on a fixed schema.
_UNAUTH_ATTACHMENT_FIELDS = (
    "imageAttachments",
    "pendingImageUploads",
    "attachments",
    "fileAttachments",
    "pendingFileUploads",
)
_UNAUTH_FILE_ID_RE = re.compile(r"^file[-_][A-Za-z0-9]{6,}$")
_UNAUTH_FILE_ID_HINT_RE = re.compile(r'"file[-_][A-Za-z0-9]{6,}"')
_UNAUTH_ATTACHMENT_ID_KEYS = ("id", "file_id", "fileId", "upload_id", "uploadId")
_UNAUTH_ATTACHMENT_NAME_KEYS = ("name", "file_name", "fileName", "filename")
_UNAUTH_ATTACHMENT_MIME_KEYS = ("mime_type", "mimeType", "mimetype", "content_type")
_UNAUTH_ATTACHMENT_SIZE_KEYS = ("size", "file_size", "fileSize", "fileSizeBytes", "bytes")


def _unauth_first_str(node: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _unauth_mweb_attachment_items(form: Dict[str, str]) -> List[Dict[str, Any]]:
    """Collect uploaded file ids referenced by an anonymous mweb turn."""
    items: List[Dict[str, Any]] = []
    seen: set = set()

    def visit(node: Any) -> None:
        if isinstance(node, str):
            candidate = node.strip()
            if _UNAUTH_FILE_ID_RE.match(candidate) and candidate not in seen:
                seen.add(candidate)
                items.append({"id": candidate, "size": 0, "name": "", "mime_type": ""})
            return
        if isinstance(node, list):
            for entry in node:
                visit(entry)
            return
        if not isinstance(node, dict):
            return
        file_id = ""
        for key in _UNAUTH_ATTACHMENT_ID_KEYS:
            value = node.get(key)
            if isinstance(value, str) and _UNAUTH_FILE_ID_RE.match(value.strip()):
                file_id = value.strip()
                break
        if file_id and file_id not in seen:
            seen.add(file_id)
            try:
                size = int(
                    next(
                        (
                            node[key]
                            for key in _UNAUTH_ATTACHMENT_SIZE_KEYS
                            if isinstance(node.get(key), (int, float, str)) and node.get(key)
                        ),
                        0,
                    )
                )
            except (TypeError, ValueError):
                size = 0
            items.append(
                {
                    "id": file_id,
                    "size": size,
                    "name": _unauth_first_str(node, _UNAUTH_ATTACHMENT_NAME_KEYS),
                    "mime_type": _unauth_first_str(node, _UNAUTH_ATTACHMENT_MIME_KEYS),
                }
            )
        for value in node.values():
            visit(value)

    for field in _UNAUTH_ATTACHMENT_FIELDS:
        raw = str(form.get(field) or "").strip()
        if not raw or raw in ("[]", "{}"):
            continue
        try:
            visit(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            visit(raw)
    return items


def _rewrite_unauth_mweb_form_prompt(payload: bytes, forced_text: str) -> Optional[bytes]:
    """Replace only the form `prompt` field; leave tokens/state untouched."""
    if payload is None:
        return None
    try:
        pairs = parse_qsl(_safe_utf8(payload), keep_blank_values=True)
    except Exception:
        return None
    if not any(k == "prompt" for k, _ in pairs):
        return None
    new_pairs = [(k, forced_text if k == "prompt" else v) for k, v in pairs]
    return urlencode(new_pairs).encode("utf-8")


_UNAUTH_CONVERSATION_RE = re.compile(
    r"""data-conversation\s*=\s*(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE,
)
_UNAUTH_ASSISTANT_OPEN_RE = re.compile(
    r"""<([A-Za-z][\w:-]*)\b[^>]*\bdata-message-(?:author-)?role\s*=\s*(?:"assistant"|'assistant'|assistant)[^>]*>""",
    re.IGNORECASE,
)
_UNAUTH_TEMPLATE_OPEN_RE = re.compile(r"<template ([^>]*)>", re.IGNORECASE)
_UNAUTH_APPLY_RE = re.compile(r'data-web-mobile-dpu-apply="(\w+)"')
_UNAUTH_FOR_RE = re.compile(r'for="([^"]*)"')
_UNAUTH_START_RE = re.compile(r'<\?start name="([^"]*)">')
_UNAUTH_BLOCK_NAME_RE = re.compile(r"-committed-block-\d+$")
_UNAUTH_PI_RE = re.compile(r"<\?[^>]*>")
# Source chips (<button data-assistant-sources-payload="[{…url…}]">) are UI
# affordances, not assistant prose — they must never reach the UI log.
_UNAUTH_DROP_MARKERS = (
    ("span", "data-assistant-content-reference"),
    ("div", "data-assistant-sources-dialog"),
    ("button", "data-assistant-sources-trigger"),
)


def _unauth_element_end(text: str, start: int, tag: str) -> Optional[int]:
    """End offset of the element opened at `start`, honouring same-tag nesting."""
    pattern = re.compile(r"<(/?)" + re.escape(tag) + r"\b[^>]*?(/?)>", re.IGNORECASE)
    depth = 0
    for match in pattern.finditer(text, start):
        if match.group(1) == "/":
            depth -= 1
            if depth <= 0:
                return match.end()
        elif match.group(2) == "/":
            if match.start() == start:
                return match.end()
        else:
            depth += 1
    return None


def _unauth_drop_elements(fragment: str) -> str:
    for tag, marker in _UNAUTH_DROP_MARKERS:
        pattern = re.compile(
            r"<" + re.escape(tag) + r"\b[^>]*" + re.escape(marker) + r"[^>]*>",
            re.IGNORECASE,
        )
        while True:
            match = pattern.search(fragment)
            if not match:
                break
            end = _unauth_element_end(fragment, match.start(), tag)
            fragment = fragment[: match.start()] + (
                fragment[end:] if end is not None else ""
            )
    return fragment


class _UnauthMarkdownParser(HTMLParser):
    """Rendered answer HTML → markdown, so anon turns match the SSE adapters.

    The lightweight anon app ships server-rendered `web-mobile-partial+html`
    instead of the markdown-carrying SSE deltas, so headings/lists/tables would
    otherwise collapse into one flat line in the UI log.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._list_stack: List[Optional[int]] = []
        self._table_rows: List[List[str]] = []
        self._cell: Optional[List[str]] = None
        self._table_has_header = False

    def _emit(self, text: str) -> None:
        if self._cell is not None:
            self._cell.append(text)
        else:
            self.parts.append(text)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in ("p", "div"):
            self._emit("\n\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("ul", "ol"):
            self._list_stack.append(1 if tag == "ol" else None)
        elif tag == "li":
            depth = max(0, len(self._list_stack) - 1)
            counter = self._list_stack[-1] if self._list_stack else None
            if counter is None:
                bullet = "- "
            else:
                bullet = f"{counter}. "
                self._list_stack[-1] = counter + 1
            self._emit("\n" + "  " * depth + bullet)
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code":
            self._emit("`")
        elif tag == "pre":
            self._emit("\n\n```\n")
        elif tag == "blockquote":
            self._emit("\n\n> ")
        elif tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._emit("\n\n---\n")
        elif tag == "table":
            self._table_rows = []
            self._table_has_header = False
        elif tag == "tr":
            self._table_rows.append([])
        elif tag in ("td", "th"):
            self._cell = []
            if tag == "th":
                self._table_has_header = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code":
            self._emit("`")
        elif tag == "pre":
            self._emit("\n```\n")
        elif tag in ("td", "th"):
            cell = "".join(self._cell or []).strip()
            self._cell = None
            if self._table_rows:
                self._table_rows[-1].append(cell)
        elif tag == "table":
            self.parts.append("\n\n" + self._render_table())
            self._table_rows = []

    def handle_data(self, data: str) -> None:
        if not data:
            return
        # Source newlines between tags are layout, not answer structure.
        self._emit(data if data.strip() else " ")

    def _render_table(self) -> str:
        rows = [r for r in self._table_rows if r]
        if not rows:
            return ""
        lines = ["| " + " | ".join(r) + " |" for r in rows]
        if self._table_has_header and len(lines) >= 1:
            lines.insert(1, "| " + " | ".join("---" for _ in rows[0]) + " |")
        return "\n".join(lines)

    def markdown(self) -> str:
        out: List[str] = []
        for raw in "".join(self.parts).split("\n"):
            line = raw.rstrip()
            if not line.strip():
                if out and out[-1]:
                    out.append("")
                continue
            out.append(line)
        return "\n".join(out).strip()


def _unauth_html_to_text(fragment: str) -> str:
    fragment = _unauth_drop_elements(fragment)
    fragment = _UNAUTH_PI_RE.sub("", fragment)
    parser = _UnauthMarkdownParser()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception:
        logger.debug("unauth markdown parse failed", exc_info=True)
        return ""
    return parser.markdown()


def _iter_unauth_json_objects(text: str):
    decoder = json.JSONDecoder()
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return
        try:
            obj, end = decoder.raw_decode(text, start)
        except Exception:
            index = start + 1
            continue
        index = end
        if isinstance(obj, dict):
            yield obj


def _iter_unauth_conversation_attrs(text: str):
    """Yield decoded `data-conversation` values, last frame last.

    Older turns embed the attribute in HTML. The ndjson body stores that HTML
    in a JSON string, so the quotes are escaped until the object is decoded.
    """
    seen = set()

    def emit(fragment: str) -> None:
        for match in _UNAUTH_CONVERSATION_RE.finditer(fragment):
            value = html_module.unescape(match.group(1) or match.group(2) or "")
            if value not in seen:
                seen.add(value)
                yield value

    # HTML partials carry the attribute directly.
    yield from emit(text)
    if '"html"' not in text:
        return
    for obj in _iter_unauth_json_objects(text):
        html = obj.get("html")
        if isinstance(html, str):
            yield from emit(html)


def _assistant_from_conversation_obj(obj: Any) -> str:
    messages = obj.get("messages") if isinstance(obj, dict) else None
    if not isinstance(messages, list):
        return ""
    best = ""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            best = content.strip()
    return best


def _extract_unauth_mweb_final_markdown(text: str) -> str:
    """Assistant markdown from the terminal `data-conversation` snapshot.

    The last frame of a completed answer carries the whole conversation as JSON,
    so the original markdown is available verbatim instead of reconstructed from
    the render patches. When several snapshots parse, the last one is the
    terminal frame.
    """
    found = ""
    for raw in _iter_unauth_conversation_attrs(text):
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        content = _assistant_from_conversation_obj(obj)
        if content:
            found = content
    return found


# DIL fallbackMarkdown backslash-escapes punctuation (`\*\*`, `\-`, `\:`).
# The screen shows the character, so revision identity and the stored answer
# use that visible form. A conversation-source delta is not passed through here.
_UNAUTH_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|:>~])")
# The caret is an unfinished token at the write head, not new prose.
# Live anonymous revisions disagree only in that tail: an open `**` (2),
# a `#` caret, or an empty table cell such as ` |  |` (7).
_UNAUTH_WRITE_HEAD = 8
_UNAUTH_STABLE_PREFIX = 8


def _unauth_visible_markdown(text: str) -> str:
    """Markdown with renderer punctuation escapes removed."""
    return _UNAUTH_MD_ESCAPE_RE.sub(r"\1", text)


def _unauth_common_prefix(left: str, right: str) -> int:
    count = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        count += 1
    return count


def _unauth_write_head_extends(previous: str, chunk: str) -> bool:
    """True when `chunk` is `previous` plus a new tail, allowing a short caret rewrite.

    Compared texts are already in visible form. A leading `# ` is the caret
    parked in front of the first frame. A trailing `#` or empty table cell is
    the same caret after the last committed character.
    """
    heads = [previous]
    if previous.startswith("#"):
        caret = 1
        while caret < len(previous) and previous[caret].isspace():
            caret += 1
        if caret > 1:
            heads.append(previous[caret:])
    for head in heads:
        stable = re.sub(r"(?:\|\s*)?#\s*$", "", head)
        matched = _unauth_common_prefix(stable, chunk)
        dirty = len(stable) - matched
        if matched >= _UNAUTH_STABLE_PREFIX and dirty <= _UNAUTH_WRITE_HEAD:
            return True
    return False


def _unauth_accept_snapshot(previous: str, snapshot: str) -> str:
    """Keep the current DIL document. A shorter replay of the same text is stale."""
    visible = _unauth_visible_markdown(snapshot)
    if not visible.strip():
        return previous
    if not previous:
        return visible
    previous_visible = _unauth_visible_markdown(previous)
    if _unauth_write_head_extends(previous_visible, visible) or visible.startswith(
        previous_visible
    ):
        return visible
    if previous_visible.startswith(visible) or _unauth_write_head_extends(
        visible, previous_visible
    ):
        return previous
    if len(visible) >= len(previous_visible):
        return visible
    return previous


def _unauth_merge_markdown_append(previous: str, chunk: str) -> str:
    """Join one `markdown` append.

    conversation-source sends a tail delta and the bytes are concatenated.
    The anonymous renderer instead re-sends the whole answer, sometimes with
    different escapes or a caret, and that copy replaces the previous one.
    """
    if not chunk:
        return previous
    if not previous:
        return chunk
    if previous.endswith(chunk):
        return previous
    if chunk.startswith(previous):
        return _unauth_visible_markdown(chunk)
    visible = _unauth_visible_markdown(chunk)
    previous_visible = _unauth_visible_markdown(previous)
    if visible.startswith(previous_visible) or _unauth_write_head_extends(
        previous_visible, visible
    ):
        return visible
    # A later frame that only repeats the opening is a stale snapshot, not a
    # tail delta. Appending it would print the answer twice.
    if previous_visible.startswith(visible) or previous_visible.endswith(visible):
        return previous
    if _unauth_write_head_extends(visible, previous_visible):
        return previous
    return previous + chunk


def _unauth_content_event(obj: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Return `(text, kind)`.

    `fallbackMarkdown` is the cumulative document. A non-empty `markdown`
    fragment in the same object must not hide it. `markdown` alone stays a
    delta or a replace, and the append merger decides which.
    """
    markdown = obj.get("markdown")
    markdown = markdown if isinstance(markdown, str) else ""
    dil = obj.get("dil")
    fallback = ""
    if isinstance(dil, dict):
        raw_fallback = dil.get("fallbackMarkdown")
        if isinstance(raw_fallback, str):
            fallback = raw_fallback
    if fallback.strip():
        return fallback, "snapshot"
    if markdown.strip():
        return markdown, "markdown"
    return None


def _replay_unauth_content_events(text: str) -> str:
    """Assistant text from `application/vnd.openai.conversation-source+ndjson`."""
    if '"markdown"' not in text and "fallbackMarkdown" not in text:
        return ""
    by_id: Dict[str, str] = {}
    order: List[str] = []
    for obj in _iter_unauth_json_objects(text):
        if obj.get("type") != "content":
            continue
        event = _unauth_content_event(obj)
        if event is None:
            continue
        chunk, kind = event
        message_id = str(obj.get("messageId") or "")
        if message_id not in by_id:
            by_id[message_id] = ""
            order.append(message_id)
        if kind == "snapshot" or obj.get("mode") != "append":
            by_id[message_id] = _unauth_accept_snapshot(by_id[message_id], chunk)
        else:
            by_id[message_id] = _unauth_merge_markdown_append(by_id[message_id], chunk)
    for message_id in reversed(order):
        text_out = by_id.get(message_id, "").strip()
        if text_out:
            return text_out
    return ""


def _replay_unauth_mweb_patches(text: str) -> str:
    """Assistant text rebuilt by replaying the DOM patch stream.

    Only appends onto the `…-committed-tail` anchor build the final document;
    `apply="replace"` rewrites a block in place and may rename its slot, and
    appends onto `…-pending-tail` are streaming previews that would duplicate
    committed text. Used when the terminal snapshot is missing (aborted or
    truncated capture).
    """
    blocks: List[str] = []
    slot_pos: Dict[str, int] = {}
    for match in _UNAUTH_TEMPLATE_OPEN_RE.finditer(text):
        attrs = match.group(1)
        apply = _UNAUTH_APPLY_RE.search(attrs)
        anchor = _UNAUTH_FOR_RE.search(attrs)
        if not apply or not anchor:
            continue
        start = _UNAUTH_START_RE.search(text, match.end())
        if not start or start.start() - match.end() > 200:
            continue
        end = text.find("<?end>", start.end())
        if end < 0:
            continue
        region = text[start.end() : end]
        if "data-assistant-stream-block" not in region:
            continue
        name = start.group(1)
        target = anchor.group(1)
        chunk = _unauth_html_to_text(region)
        if apply.group(1) == "append":
            if not target.endswith("-committed-tail"):
                continue
            blocks.append(chunk)
            if _UNAUTH_BLOCK_NAME_RE.search(name):
                slot_pos[name] = len(blocks) - 1
        else:
            pos = slot_pos.get(target)
            if pos is None or not _UNAUTH_BLOCK_NAME_RE.search(target):
                continue
            blocks[pos] = chunk
            if name != target:
                slot_pos.pop(target, None)
                if _UNAUTH_BLOCK_NAME_RE.search(name):
                    slot_pos[name] = pos
    return "\n".join(block for block in blocks if block)


def _extract_unauth_rendered_assistant(text: str) -> str:
    """Last rendered assistant node when snapshot and ndjson are absent.

    Logged-out mweb completes the turn as HTML carrying
    data-message-role="assistant" rather than only a data-conversation JSON
    snapshot or committed-tail patches.
    """
    found = ""
    for match in _UNAUTH_ASSISTANT_OPEN_RE.finditer(text):
        end = _unauth_element_end(text, match.start(), match.group(1))
        if end is None:
            continue
        chunk = _unauth_html_to_text(text[match.start() : end])
        if chunk:
            found = chunk
    return found


def _extract_unauth_mweb_assistant_text(payload: bytes) -> str:
    """Assistant text from a lightweight anon updates response.

    The body is either declarative HTML or concatenated
    `conversation-source+ndjson` objects. Resume-token JWTs, safety islands,
    and client telemetry must never become 응답 text.
    """
    if not payload:
        return ""
    text = _safe_utf8(payload)
    if not text:
        return ""
    return (
        _extract_unauth_mweb_final_markdown(text)
        or _replay_unauth_content_events(text)
        or _replay_unauth_mweb_patches(text)
        or _extract_unauth_rendered_assistant(text)
    )


def is_chatgpt_ws_user_path(path: str) -> bool:
    """Celsius `/ws/user` (and /p24/ws/user/…) — assistant text after stream_handoff."""
    path_lower = (path or "").split("?", 1)[0].lower()
    return "/ws/user" in path_lower


def is_chatgpt_response_collect_path(path: str) -> bool:
    """True for response-direction SSE/WS we must tee/extract (turn + handoff + Celsius WS)."""
    if is_chatgpt_conversation_turn_path(path):
        return True
    if is_chatgpt_ws_user_path(path):
        return True
    path_lower = (path or "").split("?", 1)[0].lower()
    cands = (
        "/backend-api/f/conversation/resume",
        "/backend-anon/f/conversation/resume",
        "/backend-api/anon/conversation/resume",
        "/backend-anon/conversation/resume",
    )
    for pref in cands:
        i = path_lower.find(pref)
        if i < 0:
            continue
        after = path_lower[i + len(pref) : i + len(pref) + 1]
        if not after or after in ("?", "/"):
            return True
    return False


def chatgpt_should_skip_policy(req: Dict[str, Any], payload: bytes = b"") -> bool:
    """Deny-by-default: only evaluate allowlisted conversation turns.

    Deep INLINE tags every chatgpt.com host as service=gpt. Without this gate,
    ces/telemetry bodies become 사용자 프롬프트 garbage in pipeline UI logs.
    """
    if (req.get("direction") or "").lower() != "request":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    if _is_codex_desktop_ws(req):
        prompt = _extract_codex_desktop_prompt(payload)
        client = str(meta.get("client_ip") or "")
        if prompt:
            logger.info(
                "chatgpt codex_desktop turn client=%s chars=%d",
                client or "-",
                len(prompt),
            )
            return False
        logger.info(
            "chatgpt codex_desktop frame skipped client=%s bytes=%d label=%s",
            client or "-",
            len(payload or b""),
            _codex_desktop_frame_label(payload) or "-",
        )
        return True
    path = str(meta.get("path") or "")
    if not is_chatgpt_conversation_turn_path(path):
        return True
    if _is_codex_responses_path(path):
        obj = _codex_turn_object(payload)
        if not isinstance(obj, dict):
            return True
        # Ambient suggestions, title generation, compaction, and memory turns
        # are started by the app. Their user-role text is the app's instruction.
        if _codex_turn_is_app_initiated(obj, req):
            docs = _codex_turn_metadata_documents(obj, req)
            logger.info(
                "chatgpt codex app turn skipped thread_source=%s turn_trigger=%s "
                "request_kind=%s output_format=%s",
                _codex_meta_value(docs, "thread_source") or "-",
                _codex_meta_value(docs, "turn_trigger") or "-",
                _codex_meta_value(docs, "request_kind") or "-",
                _codex_output_format_type(obj) or "-",
            )
            return True
        if _codex_is_tool_followup(obj):
            # The model read a local file and is sending the bytes back.
            # Inspect that body on the original prompt instead of dropping it.
            return not codex_tool_followup_has_files(obj)
        if _extract_codex_user_prompt(obj) or _codex_last_user_has_inline_file(obj):
            return False
        return True
    # unauth-mweb updates: tokens/state alone are not a turn. A file with no
    # prompt still has to reach file_block_filter.
    if _is_unauth_mweb_updates_path(path):
        if _extract_unauth_mweb_form_prompt(payload):
            return False
        try:
            form = {k: v for k, v in parse_qsl(_safe_utf8(payload), keep_blank_values=True)}
        except Exception:
            form = {}
        return not bool(_unauth_mweb_attachment_items(form))
    return False


def _cookie_value(cookie_str: str, name: str) -> str:
    if not cookie_str or not name:
        return ""
    m = re.search(
        r"(?:^|;\s*)" + re.escape(name) + r"=([^;]+)",
        cookie_str,
        re.IGNORECASE,
    )
    if not m:
        return ""
    return m.group(1).strip()


def _chatgpt_account_response_path(path: str) -> bool:
    """Logged-in profile JSON. NextAuth session and /backend-api/me carry the display name."""
    base = (path or "").split("?", 1)[0].rstrip("/").lower()
    return base in ("/api/auth/session", "/backend-api/me")


def _chatgpt_person_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 80:
        return ""
    lowered = text.lower()
    if "@" in text or "/" in text or "\\" in text or lowered.startswith("http"):
        return ""
    if lowered.startswith("user-") or lowered.startswith("did:"):
        return ""
    return text


def _chatgpt_profile_fields(obj: Dict[str, Any]) -> Tuple[str, str, str]:
    user = obj.get("user") if isinstance(obj.get("user"), dict) else obj
    if not isinstance(user, dict):
        return "", "", ""
    user_id = str(user.get("id") or "").strip()
    email = str(user.get("email") or "").strip()
    if "@" not in email:
        email = ""
    name = _chatgpt_person_name(user.get("name"))
    if not name:
        name = _chatgpt_person_name(user.get("displayName") or user.get("display_name"))
    return user_id, email, name


def _chatgpt_request_cookie(meta: Dict[str, Any]) -> str:
    cookie = str(meta.get("cookie") or "")
    if cookie:
        return cookie
    headers = meta.get("request_headers")
    if isinstance(headers, dict):
        return str(headers.get("cookie") or headers.get("Cookie") or "")
    return ""


def _remember_chatgpt_name(req: Dict[str, Any], user_id: str, email: str, name: str) -> None:
    """Keep the display name on the same account keys the email already uses."""
    cleaned = _chatgpt_person_name(name)
    if cleaned:
        req["_chatgpt_name"] = cleaned
    if not _account_lookup or not (user_id or email or cleaned):
        return
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    client_ip = _client_ip_from_meta(meta)
    did = _cookie_value(_chatgpt_request_cookie(meta), "oai-did")
    keys = []
    if user_id:
        keys.append(user_id)
    if did:
        keys.append(f"did:{did}")
    if not keys and client_ip:
        keys.append(f"ip:{client_ip}")
    for key in keys:
        _account_lookup.set_account(
            "chatgpt",
            key,
            user_id,
            email,
            client_ip=client_ip,
            display_name=cleaned,
        )


def _cached_chatgpt_name(req: Dict[str, Any], user_id: str) -> str:
    current = _chatgpt_person_name(req.get("_chatgpt_name"))
    if current or not _account_lookup or not hasattr(_account_lookup, "get_result"):
        return current
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    keys = []
    if user_id:
        keys.append(user_id)
    did = _cookie_value(_chatgpt_request_cookie(meta), "oai-did")
    if did:
        keys.append(f"did:{did}")
    client_ip = _client_ip_from_meta(meta)
    if client_ip:
        keys.append(f"ip:{client_ip}")
    for key in keys:
        try:
            info = _account_lookup.get_result("chatgpt", key)
        except Exception:
            info = None
        name = _chatgpt_person_name(getattr(info, "display_name", "") if info else "")
        if name:
            req["_chatgpt_name"] = name
            return name
    return ""


def _note_chatgpt_account_response(req: Dict[str, Any], payload: bytes) -> None:
    try:
        obj = json.loads(_safe_utf8(payload or b""))
    except Exception:
        return
    if not isinstance(obj, dict):
        return
    user_id, email, name = _chatgpt_profile_fields(obj)
    if not (user_id or email or name):
        return
    _remember_chatgpt_name(req, user_id, email, name)
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    logger.info(
        "[Account Info] chatgpt: user_id=%s email=%s name=%s path=%s",
        user_id or "-",
        email or "-",
        name or "-",
        str(meta.get("path") or ""),
    )


def _extract_identity_from_authorization(req: Dict[str, Any]) -> Tuple[str, str]:
    token = _extract_bearer_token(req)
    user_id = ""
    email = ""
    display_name = ""
    if token:
        claims = _decode_jwt_payload(token)
        if claims:
            auth_claim = claims.get("https://api.openai.com/auth")
            if isinstance(auth_claim, dict):
                for key in ("chatgpt_account_user_id", "user_id", "chatgpt_user_id"):
                    value = auth_claim.get(key)
                    if isinstance(value, str) and value:
                        user_id = value
                        break
            profile_claim = claims.get("https://api.openai.com/profile")
            if isinstance(profile_claim, dict):
                value = profile_claim.get("email")
                if isinstance(value, str) and value:
                    email = value
                display_name = _chatgpt_person_name(profile_claim.get("name"))
            if not user_id:
                for key in ("sub", "user_id", "uid"):
                    value = claims.get(key)
                    if isinstance(value, str) and value:
                        user_id = value
                        break
            if not email:
                value = claims.get("email")
                if isinstance(value, str) and value:
                    email = value
            if not display_name:
                display_name = _chatgpt_person_name(claims.get("name"))

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    cookie = _chatgpt_request_cookie(meta)
    did = _cookie_value(cookie, "oai-did")
    client_ip = _client_ip_from_meta(meta)
    # oai-did is a rotating device cookie.  Use it only to look up / store
    # the JWT profile identity, never as the account itself.
    if (user_id or email) and did and _account_lookup:
        _account_lookup.set_account(
            "chatgpt",
            f"did:{did}",
            user_id,
            email,
            client_ip=client_ip,
            display_name=display_name,
        )
    if display_name:
        req["_chatgpt_name"] = display_name
    elif did and _account_lookup and hasattr(_account_lookup, "get_result"):
        try:
            cached = _account_lookup.get_result("chatgpt", f"did:{did}")
        except Exception:
            cached = None
        cached_name = _chatgpt_person_name(getattr(cached, "display_name", "") if cached else "")
        if cached_name:
            req["_chatgpt_name"] = cached_name
    if not (user_id or email) and did and _account_lookup:
        info = _account_lookup.get("chatgpt", f"did:{did}")
        if info:
            user_id, email = info[0], info[1]

    return user_id, email


def _client_ip_from_meta(meta: Dict[str, Any]) -> str:
    raw = str(meta.get("client_ip") or meta.get("clientIp") or "").strip()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        if end > 1:
            return raw[1:end]
    if raw.count(":") == 1:
        host, _, port = raw.partition(":")
        if port.isdigit():
            return host
    return raw


def build_policy_api_payload(req: Dict[str, Any], payload: bytes, extracted_text: str) -> Optional[Dict[str, Any]]:
    direction = (req.get("direction") or "").lower()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    client_host = _client_ip_from_meta(meta)

    if direction == "response":
        session_id = str(req.get("_chatgpt_output_session_id") or "")
        message_id = str(req.get("_chatgpt_output_message_id") or "")
        if not session_id or not message_id:
            try:
                obj = json.loads(_safe_utf8(payload))
            except Exception:
                obj = None
            if isinstance(obj, dict):
                if not message_id:
                    message_id = _extract_sse_message_id(obj)
                key = str(req.get("_chatgpt_stream_key") or "")
                if not key and message_id:
                    key = f"msg:{message_id}"
                if not key and len(_SSE_RESPONSE_META) == 1:
                    key = next(iter(_SSE_RESPONSE_META))
                cached = _SSE_RESPONSE_META.get(key, {}) if key else {}
                if not session_id:
                    session_id = str(req.get("_chatgpt_session_id") or "")
                if not session_id:
                    session_id = _extract_conversation_id(obj)
                if not session_id:
                    session_id = str(cached.get("session_id") or "")
                if not message_id:
                    message_id = str(cached.get("message_id") or "")

        # JWT profile identity (and a later oai-did cache hit) belongs to the
        # original request and is available on response EVE metadata as well.
        response_user_id, response_email = _extract_identity_from_authorization(req)
        if not (response_user_id or response_email) and _account_lookup:
            cookie = str(meta.get("cookie") or "")
            did = _cookie_value(cookie, "oai-did")
            if did:
                info = _account_lookup.wait_for(
                    "chatgpt",
                    [f"did:{did}"],
                    env_name="AIVAX_CHATGPT_ACCOUNT_WAIT_MS",
                )
                if info:
                    response_user_id, response_email = info[0], info[1]
        response_payload = {
            "llm_output": extracted_text,
            "user_id": response_user_id,
            "email": response_email,
            "ai_service": AI_SERVICE_GPT,
            "client_host": client_host,
            "session_id": session_id,
            "message_id": message_id,
        }
        response_name = _cached_chatgpt_name(req, response_user_id)
        if response_name:
            response_payload["user_name"] = response_name
        return response_payload

    # Lightweight anon: form-urlencoded body (not JSON). Build policy row from
    # extracted `prompt` only — never paste tokens into UI fields.
    if _is_unauth_mweb_updates_path(str(meta.get("path") or "")):
        form: Dict[str, str] = {}
        try:
            form = {k: v for k, v in parse_qsl(_safe_utf8(payload), keep_blank_values=True)}
        except Exception:
            form = {}
        prompt = (extracted_text or "").strip() or _extract_unauth_mweb_form_prompt(payload)
        attachment_items = _unauth_mweb_attachment_items(form)
        attachments = (
            build_policy_attachments({"attachments": attachment_items})
            if attachment_items
            else []
        )
        has_attachments = any(
            isinstance(a, dict) and str(a.get("id") or "").strip() for a in attachments
        )
        if attachment_items:
            logger.info(
                "chatgpt unauth_updates attachments refs=%d resolved=%d ids=%s",
                len(attachment_items),
                sum(1 for a in attachments if isinstance(a, dict) and a.get("id")),
                [str(a.get("id") or "")[-48:] for a in attachments[:4]],
            )
        else:
            # Name the field that held the id so a renamed upload field surfaces
            # here instead of silently dropping the file.
            missed = [
                key
                for key, value in form.items()
                if key not in _UNAUTH_ATTACHMENT_FIELDS
                and _UNAUTH_FILE_ID_HINT_RE.search(str(value or ""))
            ]
            if missed:
                logger.warning(
                    "chatgpt unauth_updates upload ref in unexpected fields=%s",
                    missed[:6],
                )
        # Image-only anonymous turns still need file_block_filter (auth parity).
        if not prompt and not has_attachments:
            return None
        session_id = str(req.get("_chatgpt_session_id") or "")
        if not session_id:
            session_id = str(form.get("oai-session-id") or "").strip()
        message_id = str(form.get("userMessageId") or req.get("_chatgpt_room_key") or "").strip()
        auth_user_id, auth_email = _extract_identity_from_authorization(req)
        auth_name = _cached_chatgpt_name(req, auth_user_id)
        if auth_user_id or auth_email or auth_name:
            logger.info(
                "[Account Info] chatgpt: user_id=%s email=%s name=%s path=%s",
                auth_user_id or "-",
                auth_email or "-",
                auth_name or "-",
                str(meta.get("path") or ""),
            )
        unauth_payload = {
            "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
            "prompt": prompt,
            "encoding": False,
            "user_id": auth_user_id or "",
            "email": auth_email or "",
            "ai_service": AI_SERVICE_GPT,
            "client_host": client_host,
            "session_id": session_id,
            "attachments": attachments,
            "message_id": message_id,
        }
        if auth_name:
            unauth_payload["user_name"] = auth_name
        return unauth_payload

    # Codex IDE turns arrive as a masked WebSocket text frame, not json_body.
    # extract_full_body already pulled the visible prompt; re-parsing the frame
    # as HTTP JSON returns None and the engine then drops the policy call.
    if _is_codex_responses_path(str(meta.get("path") or "")) and (
        (req.get("unit_type") or "").lower() == "ws_frame"
    ):
        turn = _codex_turn_object(payload) or {}
        prompt = (extracted_text or "").strip() or _extract_codex_user_prompt(turn)
        attachments = build_policy_attachments(turn) if turn else []
        if turn and _codex_is_tool_followup(turn):
            if not prompt:
                prompt = _codex_saved_prompt(str(req.get("request_id") or ""))
            parent = _codex_saved_message_id(str(req.get("request_id") or ""))
            if parent:
                req["_chatgpt_ui_message_id"] = parent
        if not (prompt or "").strip() and not any(
            isinstance(item, dict) and str(item.get("id") or "").strip() for item in attachments
        ):
            return None
        auth_user_id, auth_email = _extract_identity_from_authorization(req)
        auth_name = _cached_chatgpt_name(req, auth_user_id)
        session_id = str(
            turn.get("conversation_id")
            or turn.get("previous_response_id")
            or req.get("_chatgpt_session_id")
            or ""
        )
        ws_payload = {
            "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
            "prompt": prompt,
            "encoding": False,
            "user_id": auth_user_id or "",
            "email": auth_email or "",
            "ai_service": AI_SERVICE_GPT,
            "client_host": client_host,
            "session_id": session_id,
            "attachments": attachments,
            "message_id": _extract_message_id(turn) if turn else "",
        }
        if auth_name:
            ws_payload["user_name"] = auth_name
        return ws_payload

    obj = _parse_request_json(req, payload)
    if obj is None:
        return None

    prompt = _extract_latest_user_prompt(obj)
    attachments = build_policy_attachments(obj)
    has_attachments = any(
        isinstance(a, dict) and str(a.get("id") or "").strip()
        for a in (attachments or [])
    )
    # Attachment-only chat turns still need file_block_filter (Gemini parity).
    # Non-chat JSON without prompt/attachments: do not invent a policy payload.
    if not (prompt or "").strip() and not has_attachments:
        return None

    # 확장자/크기 차단은 pipeline file_block_filter가 담당 (engine 선차단 비활성).
    # Collection gate (file_allow_extension / file_control_maxsize) denied earlier:
    # force BLOCK on the chat turn even if async PUT already reached OpenAI.
    # for att in attachments or []:
    #     if not isinstance(att, dict):
    #         continue
    #     path = str(att.get("id") or "").strip()
    #     name = str(att.get("name") or "").strip()
    #     for candidate in (os.path.basename(path), path.rsplit("/", 1)[-1] if path else "", name):
    #         why = _fum.collection_denied_reason(candidate)
    #         if why:
    #             req["_chatgpt_file_collection_denied"] = why
    #             logger.info(
    #                 "chatgpt chat-turn collection deny uid=%s name=%s reason=%s",
    #                 candidate[:64],
    #                 name[:80],
    #                 why,
    #             )
    #             break
    #     if req.get("_chatgpt_file_collection_denied"):
    #         break

    auth_user_id, auth_email = _extract_identity_from_authorization(req)
    auth_name = _cached_chatgpt_name(req, auth_user_id)
    if auth_user_id or auth_email or auth_name:
        logger.info(
            "[Account Info] chatgpt: user_id=%s email=%s name=%s path=%s",
            auth_user_id or "-",
            auth_email or "-",
            auth_name or "-",
            str(meta.get("path") or ""),
        )

    chat_payload = {
        "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
        "prompt": prompt or "",
        "encoding": False,
        "user_id": auth_user_id or str(obj.get("user_id") or ""),
        "email": auth_email or str(obj.get("email") or ""),
        "ai_service": AI_SERVICE_GPT,
        "client_host": client_host,
        "session_id": str(
            obj.get("conversation_id")
            or obj.get("previous_response_id")
            or obj.get("session_id")
            or ""
        ),
        "attachments": attachments,
        "message_id": _extract_message_id(obj),
    }
    if auth_name:
        chat_payload["user_name"] = auth_name
    return chat_payload


def extract(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    text = _safe_utf8(payload)
    direction = (req.get("direction") or "").lower()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")

    # Session / profile JSON is identity, not a prompt. The access token stays in the body.
    if direction == "response" and _chatgpt_account_response_path(path):
        _note_chatgpt_account_response(req, payload)
        return ""

    # Lightweight anon turn: form-urlencoded `prompt` only (never tokens/HTML).
    if _is_unauth_mweb_updates_path(path):
        if direction == "response":
            # Declarative partial-update HTML — committed assistant blocks only.
            return _extract_unauth_mweb_assistant_text(payload or b"")
        prompt = _extract_unauth_mweb_form_prompt(payload)
        if prompt:
            try:
                pairs = parse_qsl(text, keep_blank_values=True)
                form = {k: v for k, v in pairs}
                cid = ""
                raw_state = form.get("conversationState") or ""
                if raw_state:
                    try:
                        st = json.loads(raw_state)
                        if isinstance(st, dict):
                            for key in ("conversation_id", "conversationId", "id"):
                                val = st.get(key)
                                if isinstance(val, str) and val.strip():
                                    cid = val.strip()
                                    break
                    except Exception:
                        pass
                mid = form.get("userMessageId") or ""
                if isinstance(mid, str) and mid.strip():
                    req["_chatgpt_room_key"] = mid.strip()
                if cid:
                    req["_chatgpt_session_id"] = cid
                elif isinstance(form.get("oai-session-id"), str) and form.get("oai-session-id"):
                    req["_chatgpt_session_id"] = str(form.get("oai-session-id"))
            except Exception:
                pass
        return prompt

    if _is_codex_responses_path(path) and unit_type == "ws_frame":
        if direction != "request":
            return ""
        return _extract_codex_request_prompt(req, payload or b"")

    if _is_codex_desktop_ws(req) and unit_type == "ws_frame":
        if direction != "request":
            return ""
        return _extract_codex_desktop_prompt(payload or b"")

    out: Optional[str] = None
    if unit_type == "json_body" or "application/json" in content_type:
        try:
            obj = json.loads(text)
        except Exception:
            # Binary / non-JSON must not become 사용자 프롬프트 in pipeline logs.
            out = None
            obj = None
        if isinstance(obj, dict):
            _register_request_context(req, obj)
            if _is_codex_responses_path(path) and _codex_turn_is_app_initiated(obj, req):
                return ""
            latest = _extract_latest_user_prompt(obj)
            if latest:
                out = latest
            else:
                messages = obj.get("messages")
                if isinstance(messages, list):
                    chunks = []
                    for msg in messages:
                        if not isinstance(msg, dict):
                            continue
                        if _message_role(msg) and _message_role(msg) != "user":
                            continue
                        content = msg.get("content")
                        chunk = _extract_content(content)
                        if _is_codex_responses_path(path):
                            chunk = _extract_user_visible_prompt(chunk)
                        if chunk:
                            chunks.append(chunk)
                    out = "\n".join(chunks) if chunks else ""
                else:
                    # Avoid dumping entire telemetry JSON as the user prompt.
                    out = ""
        if out and isinstance(obj, dict):
            _bind_prompt_request(req, obj)
        return out

    if unit_type == "sse_event" or "text/event-stream" in content_type:
        # One payload may contain multiple SSE events (e.g. message_stream_complete + [DONE]); process each.
        data_lines = _split_sse_data_lines(text)
        if not data_lines:
            data_lines = [text.strip()]

        last_final_merged: Optional[str] = None
        for data in data_lines:
            if not data:
                continue
            if data == "[DONE]":
                # [DONE] has no stream id; treat it as "the stream that just ended" = newest in BUFFER.
                # Do not use PENDING first (would return an old turn's content when [DONE] is sent late).
                key = None
                for k in reversed(_STREAM_KEY_ORDER):
                    if k in _SSE_RESPONSE_BUFFER:
                        key = k
                        break
                if key is None:
                    key = _resolve_done_stream_key(req)
                if key and key in _SSE_RESPONSE_BUFFER:
                    merged = _SSE_RESPONSE_BUFFER.pop(key, "")
                    _SSE_ANSWER_VISIBLE.pop(key, None)
                    _set_output_ctx(req, key)
                    _set_extract_final(req, bool(merged))
                    last_final_merged = merged
                    # Cleanup PENDING keys that were already finalized elsewhere (no buffer left).
                    while _PENDING_DONE_KEYS:
                        k = _PENDING_DONE_KEYS.popleft()
                        if k not in _SSE_RESPONSE_BUFFER and k in _SSE_RESPONSE_META:
                            _set_output_ctx(req, k)
                    continue
                if key and key in _SSE_RESPONSE_META:
                    _set_output_ctx(req, key)
                # Drain PENDING: cleanup only keys that have no buffer left.
                while _PENDING_DONE_KEYS:
                    k = _PENDING_DONE_KEYS.popleft()
                    if k not in _SSE_RESPONSE_BUFFER and k in _SSE_RESPONSE_META:
                        _set_output_ctx(req, k)
                _set_extract_final(req, False)
                last_final_merged = None
                continue
            try:
                obj = json.loads(data)
            except Exception:
                _set_extract_final(req, False)
                continue
            if not isinstance(obj, dict):
                _set_extract_final(req, False)
                continue
            key = _update_sse_meta(req, obj)
            if obj.get("type") == "message_stream_complete":
                merged = _SSE_RESPONSE_BUFFER.pop(key, "")
                _SSE_ANSWER_VISIBLE.pop(key, None)
                try:
                    _PENDING_DONE_KEYS.remove(key)
                except ValueError:
                    pass
                _set_output_ctx(req, key)
                _set_extract_final(req, bool(merged))
                last_final_merged = merged
                continue
            _track_answer_visibility(key, obj)
            if not _SSE_ANSWER_VISIBLE.get(key, True):
                _set_extract_final(req, False)
                continue
            chunk = _extract_sse_assistant_text(obj)
            if chunk:
                prev = _SSE_RESPONSE_BUFFER.get(key, "")
                _SSE_RESPONSE_BUFFER[key] = prev + chunk

            if _is_stream_finished(obj) and key in _SSE_RESPONSE_BUFFER:
                merged = _SSE_RESPONSE_BUFFER.pop(key, "")
                _SSE_ANSWER_VISIBLE.pop(key, None)
                try:
                    _PENDING_DONE_KEYS.remove(key)
                except ValueError:
                    pass
                _set_output_ctx(req, key)
                _set_extract_final(req, bool(merged))
                last_final_merged = merged
                continue

            _set_extract_final(req, False)

        if last_final_merged is not None:
            return last_final_merged
        if len(data_lines) == 1:
            return ""
        _set_extract_final(req, False)
        return ""

    return text


def _extract_sse_error_text(obj: Dict[str, Any]) -> str:
    """Best-effort error / detail string from ChatGPT SSE frames (short abort streams)."""
    if not isinstance(obj, dict):
        return ""
    for key in ("error", "detail", "message"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            if key == "message" and isinstance(obj.get("author"), dict):
                continue  # assistant message envelope, not error
            return val.strip()
        if isinstance(val, dict):
            for sub in ("message", "detail", "code", "type"):
                s = val.get(sub)
                if isinstance(s, str) and s.strip():
                    return s.strip()
    t = obj.get("type")
    if isinstance(t, str) and "error" in t.lower():
        return t
    return ""


def _parse_ws_payloads(payload: bytes) -> List[bytes]:
    """Parse WebSocket frames (server→client usually unmasked) into payloads."""
    out: List[bytes] = []
    i = 0
    n = len(payload or b"")
    while i + 2 <= n:
        b0 = payload[i]
        b1 = payload[i + 1]
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) != 0
        plen = b1 & 0x7F
        i += 2
        if plen == 126:
            if i + 2 > n:
                break
            plen = int.from_bytes(payload[i : i + 2], "big")
            i += 2
        elif plen == 127:
            if i + 8 > n:
                break
            plen = int.from_bytes(payload[i : i + 8], "big")
            i += 8
        if masked:
            if i + 4 > n:
                break
            mask = payload[i : i + 4]
            i += 4
        else:
            mask = b""
        if i + plen > n:
            break
        data = bytearray(payload[i : i + plen])
        i += plen
        if mask:
            for j in range(len(data)):
                data[j] ^= mask[j % 4]
        if opcode in (0x1, 0x2):  # text / binary
            out.append(bytes(data))
        elif opcode == 0x8:  # close
            break
        # 0x9/0xA ping/pong ignored
    return out


def _iter_ws_frames(payload: bytes):
    """Yield (opcode, unmasked payload, original frame bytes) for complete frames."""
    raw = payload or b""
    i = 0
    n = len(raw)
    while i + 2 <= n:
        start = i
        b0 = raw[i]
        b1 = raw[i + 1]
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) != 0
        plen = b1 & 0x7F
        i += 2
        if plen == 126:
            if i + 2 > n:
                break
            plen = int.from_bytes(raw[i : i + 2], "big")
            i += 2
        elif plen == 127:
            if i + 8 > n:
                break
            plen = int.from_bytes(raw[i : i + 8], "big")
            i += 8
        if masked:
            if i + 4 > n:
                break
            mask = raw[i : i + 4]
            i += 4
        else:
            mask = b""
        if i + plen > n:
            break
        data = bytearray(raw[i : i + plen])
        i += plen
        if mask:
            for j in range(len(data)):
                data[j] ^= mask[j % 4]
        yield opcode, bytes(data), raw[start:i]


def _encode_ws_text_frame(payload: bytes, *, masked: bool) -> bytes:
    """FIN text frame. Client frames must be masked; server frames must not."""
    data = bytearray(payload or b"")
    header = bytearray([0x81])
    n = len(data)
    mask_bit = 0x80 if masked else 0x00
    if n < 126:
        header.append(mask_bit | n)
    elif n < 65536:
        header.append(mask_bit | 126)
        header.extend(n.to_bytes(2, "big"))
    else:
        header.append(mask_bit | 127)
        header.extend(n.to_bytes(8, "big"))
    if masked:
        mask = os.urandom(4)
        header.extend(mask)
        for j in range(n):
            data[j] ^= mask[j % 4]
    return bytes(header) + bytes(data)


def _coerce_json_objs(text: str) -> List[Dict[str, Any]]:
    """Parse a text blob into JSON objects (single JSON / NDJSON / SSE data lines)."""
    objs: List[Dict[str, Any]] = []
    if not text:
        return objs
    text = text.strip()
    if not text:
        return objs
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            objs.append(obj)
            return objs
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    objs.append(item)
            return objs
    except Exception:
        pass
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def _looks_like_json_or_sse(text: str) -> bool:
    s = (text or "").lstrip()
    if not s:
        return False
    return (
        s.startswith("{")
        or s.startswith("[")
        or s.startswith("data:")
        or s.startswith("event:")
        or "\ndata:" in s
    )


def _iter_ws_json_objects(payload: bytes) -> List[Dict[str, Any]]:
    """Yield JSON dicts from raw WS frame bytes (or already-unwrapped payloads).

    Celsius `/ws/user` frames are JSON arrays::
        [{"type":"message","topic_id":"conversation-turn-…",
          "payload":{"type":"conversation-turn-stream",
                     "payload":{"type":"stream-item","encoded_item":"event: delta\\ndata: …"}}}]
    ``encoded_item`` is the same conduit SSE used on HTTP. Subscribe ``reply.catchups``
    may replay the same envelopes.
    """
    raw_text = _safe_utf8(payload or b"").lstrip()
    # Already-unwrapped JSON (unit tests / some tees) — do not parse as WS frames
    # (`{`/`[` bytes can look like bogus WebSocket headers).
    if raw_text.startswith("{") or raw_text.startswith("["):
        candidates = [payload or b""]
    else:
        frames = _parse_ws_payloads(payload or b"")
        candidates = frames if frames else ([payload] if payload else [])
    out: List[Dict[str, Any]] = []
    seen_mid: set = set()

    def _add(obj: Dict[str, Any], depth: int = 0) -> None:
        if not isinstance(obj, dict) or depth > 10:
            return
        sid = obj.get("stream_item_id")
        turn_hint = ""
        for key in ("topic_id", "turn_id", "turn_exchange_id"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                turn_hint = val.strip()
                break
        if isinstance(sid, str) and sid.strip() and turn_hint:
            topic = (
                turn_hint
                if turn_hint.startswith("conversation-turn-")
                else f"conversation-turn-{turn_hint}"
            )
            if _ws_item_duplicate(topic, sid.strip()):
                return
        mid = ""
        if isinstance(obj.get("message"), dict):
            mid = str(obj["message"].get("id") or "")
        elif isinstance(obj.get("v"), dict) and isinstance(obj["v"].get("message"), dict):
            mid = str(obj["v"]["message"].get("id") or "")
        if mid and mid in seen_mid:
            return
        if mid:
            seen_mid.add(mid)
        out.append(obj)
        for nest_key in (
            "body",
            "data",
            "payload",
            "message",
            "content",
            "text",
            "encoded_item",
            "reply",
            "catchups",
            "update_content",
            "messages",
        ):
            nest = obj.get(nest_key)
            if isinstance(nest, str) and _looks_like_json_or_sse(nest):
                for nested in _coerce_json_objs(nest):
                    _add(nested, depth + 1)
            elif isinstance(nest, dict):
                _add(nest, depth + 1)
                if isinstance(nest.get("message"), dict):
                    _add(
                        {"message": nest["message"], "v": {"message": nest["message"]}},
                        depth + 1,
                    )
            elif isinstance(nest, list):
                for item in nest:
                    if isinstance(item, dict) and nest_key == "messages" and isinstance(
                        item.get("author"), dict
                    ):
                        _add({"message": item, "v": {"message": item}}, depth + 1)
                    elif isinstance(item, dict):
                        _add(item, depth + 1)
                    elif isinstance(item, str) and _looks_like_json_or_sse(item):
                        for nested in _coerce_json_objs(item):
                            _add(nested, depth + 1)

    for raw in candidates:
        text = _safe_utf8(raw).strip()
        if not text:
            continue
        for obj in _coerce_json_objs(text):
            _add(obj)
    return out


# Per-topic WS response assembly (high-reasoning answers stream across tees).
_WS_RESP_BUFFER: Dict[str, str] = {}
_WS_RESP_FINISHED: Dict[str, bool] = {}
_WS_RESP_CONV_TOPICS: Dict[str, Set[str]] = {}
_WS_SEEN_ITEMS: Dict[str, Set[str]] = {}
_WS_RESP_BUF_MAX = 64


def _ws_item_duplicate(topic: str, stream_item_id: str) -> bool:
    """True if this Celsius stream_item_id was already expanded (catchup vs live)."""
    if not topic or not stream_item_id:
        return False
    seen = _WS_SEEN_ITEMS.setdefault(topic, set())
    if stream_item_id in seen:
        return True
    seen.add(stream_item_id)
    if len(_WS_SEEN_ITEMS) > _WS_RESP_BUF_MAX:
        for old in list(_WS_SEEN_ITEMS.keys())[: max(1, len(_WS_SEEN_ITEMS) // 4)]:
            _WS_SEEN_ITEMS.pop(old, None)
    return False


def _ws_bind_conversation(conversation_id: str, topic: str) -> None:
    cid = (conversation_id or "").strip()
    t = (topic or "").strip()
    if not cid or not t:
        return
    _WS_RESP_CONV_TOPICS.setdefault(cid, set()).add(t)


def _ws_topics_for_conversation(conversation_id: str) -> Set[str]:
    cid = (conversation_id or "").strip()
    if not cid:
        return set()
    return set(_WS_RESP_CONV_TOPICS.get(cid) or ())


def _ws_topic_key_from_objs(objs: List[Dict[str, Any]], payload: bytes) -> str:
    topic = ""
    turn = ""
    conv = ""
    for obj in objs:
        tid = obj.get("topic_id") or obj.get("topic")
        if isinstance(tid, str) and tid.strip().startswith("conversation-turn-"):
            topic = tid.strip()
        tex = obj.get("turn_id") or obj.get("turn_exchange_id")
        if isinstance(tex, str) and tex.strip():
            turn = tex.strip()
        cid = obj.get("conversation_id")
        if isinstance(cid, str) and cid.strip():
            conv = cid.strip()
        opts = obj.get("options")
        if isinstance(opts, list):
            for opt in opts:
                if isinstance(opt, dict):
                    ot = opt.get("topic_id")
                    if isinstance(ot, str) and ot.strip().startswith("conversation-turn-"):
                        topic = ot.strip()
    if topic:
        return topic
    if turn:
        return turn if turn.startswith("conversation-turn-") else f"conversation-turn-{turn}"
    text = _safe_utf8(payload or b"")
    m = re.search(r"conversation-turn-[0-9a-fA-F-]{20,}", text)
    if m:
        return m.group(0)
    if conv:
        return f"conv:{conv}"
    return ""


def _ws_conversation_id_from_objs(objs: List[Dict[str, Any]]) -> str:
    for obj in objs:
        cid = obj.get("conversation_id")
        if isinstance(cid, str) and cid.strip():
            return cid.strip()
        pl = obj.get("payload")
        if isinstance(pl, dict):
            cid = pl.get("conversation_id")
            if isinstance(cid, str) and cid.strip():
                return cid.strip()
            inner = pl.get("payload")
            if isinstance(inner, dict):
                cid = inner.get("conversation_id")
                if isinstance(cid, str) and cid.strip():
                    return cid.strip()
    return ""


def _conversation_id_from_raw_bytes(payload: bytes) -> str:
    """Read conversation_id without mutating WS seen-item state."""
    text = _safe_utf8(payload or b"")
    for match in re.finditer(r'"conversation_id"\s*:\s*"([^"]+)"', text):
        cid = (match.group(1) or "").strip()
        if cid and not cid.startswith("pending:"):
            return cid
    return ""


def _ws_merge_text(prev: str, incoming: str) -> str:
    """Append conduit deltas without duplicating snapshot/replay overlap."""
    if not incoming:
        return prev
    if not prev:
        return incoming
    if incoming == prev:
        return prev
    if len(incoming) > len(prev) and incoming.startswith(prev):
        return incoming
    if len(prev) > len(incoming) and prev.startswith(incoming):
        return prev
    if len(incoming) >= 8 and prev.endswith(incoming):
        return prev
    max_k = min(len(prev), len(incoming), 512)
    for k in range(max_k, 2, -1):
        if prev[-k:] == incoming[:k]:
            return prev + incoming[k:]
    return prev + incoming


def _ws_buffer_append(topic: str, chunk: str) -> None:
    if not topic or not chunk:
        return
    prev = _WS_RESP_BUFFER.get(topic, "")
    _WS_RESP_BUFFER[topic] = _ws_merge_text(prev, chunk)
    if len(_WS_RESP_BUFFER) > _WS_RESP_BUF_MAX:
        for old in list(_WS_RESP_BUFFER.keys())[: max(1, len(_WS_RESP_BUFFER) // 4)]:
            _WS_RESP_BUFFER.pop(old, None)
            _WS_RESP_FINISHED.pop(old, None)
            _WS_SEEN_ITEMS.pop(old, None)


def _ws_buffer_flush(topic: str) -> str:
    if not topic:
        return ""
    text = _WS_RESP_BUFFER.pop(topic, "")
    _WS_RESP_FINISHED.pop(topic, None)
    _WS_SEEN_ITEMS.pop(topic, None)
    for cid, topics in list(_WS_RESP_CONV_TOPICS.items()):
        topics.discard(topic)
        if not topics:
            _WS_RESP_CONV_TOPICS.pop(cid, None)
    return text


def _ws_flush_conversation(conversation_id: str, extra_topic: str = "") -> str:
    topics = _ws_topics_for_conversation(conversation_id)
    if extra_topic:
        topics.add(extra_topic)
    cid = (conversation_id or "").strip()
    if cid:
        topics.add(f"conv:{cid}")
        _WS_RESP_CONV_TOPICS.pop(cid, None)
    parts: List[str] = []
    for topic in topics:
        part = _ws_buffer_flush(topic)
        if part:
            parts.append(part)
    return "".join(parts)


def _extract_ws_assistant_text(
    payload: bytes,
) -> Tuple[Optional[str], Dict[str, int], bool]:
    """Extract assistant-visible text from Celsius WS frames.

    Returns (text, type_counts, is_final).
    High-reasoning often delivers the answer only on WS after stream_handoff.
    """
    type_counts: Dict[str, int] = {}
    chunks: List[str] = []
    snapshots: List[str] = []
    finished = False
    conv_complete = False
    objs = _iter_ws_json_objects(payload or b"")
    topic = _ws_topic_key_from_objs(objs, payload or b"")
    # A turn's messages span many WS frames: remember which message is open per topic.
    vis_key = f"ws:{topic}" if topic else "ws:_frame"

    for obj in objs:
        t = obj.get("type")
        if isinstance(t, str) and t:
            type_counts[t] = type_counts.get(t, 0) + 1
            # conversation.done / server.stream.done / Celsius turn-complete: always terminal.
            # message_stream_complete: terminal only when it is a bare control
            # event (HTTP SSE style) OR the embedded message is finished.
            # Snapshots with status=in_progress reuse this type — do not flush yet.
            if t in (
                "conversation.done",
                "server.stream.done",
                "conversation-turn-complete",
            ):
                finished = True
                if t == "conversation-turn-complete":
                    conv_complete = True
            elif t == "message_stream_complete":
                msg_status = ""
                v0 = obj.get("v")
                if isinstance(v0, dict) and isinstance(v0.get("message"), dict):
                    msg_status = str(v0["message"].get("status") or "")
                elif isinstance(obj.get("message"), dict):
                    msg_status = str(obj["message"].get("status") or "")
                if not msg_status or msg_status in (
                    "finished_successfully",
                    "finished",
                ):
                    finished = True

        # Do NOT treat per-item ``finished_successfully`` as turn-final.
        # High-reasoning / web.run emits several assistant snapshots (162 then
        # 5k+) each already marked finished; flushing the first one made
        # output_filter drop the real answer via message_id dedup.
        is_snapshot = False
        msg_for_skip = None
        v = obj.get("v")
        if isinstance(v, dict) and isinstance(v.get("message"), dict):
            is_snapshot = True
            msg_for_skip = v.get("message")
        elif isinstance(obj.get("message"), dict):
            is_snapshot = True
            msg_for_skip = obj.get("message")
        _track_answer_visibility(vis_key, obj)
        if not _SSE_ANSWER_VISIBLE.get(vis_key, True):
            continue
        if isinstance(msg_for_skip, dict) and not _is_answer_message(msg_for_skip):
            continue

        chunk = _extract_sse_assistant_text(obj)
        if chunk:
            if is_snapshot:
                snapshots.append(chunk)
                # conversation-update (add-messages / async-task-update-message)
                # carries the whole answer with no turn-complete of its own.
                if (
                    "conversation-update" in type_counts
                    and isinstance(msg_for_skip, dict)
                    and msg_for_skip.get("status") == "finished_successfully"
                ):
                    finished = True
            else:
                chunks.append(chunk)

    if not topic:
        _SSE_ANSWER_VISIBLE.pop(vis_key, None)

    joined = "".join(chunks)
    best_snap = max(snapshots, key=len) if snapshots else ""
    result = joined if len(joined) >= len(best_snap) else best_snap

    conv_id = _ws_conversation_id_from_objs(objs)
    if topic and conv_id:
        _ws_bind_conversation(conv_id, topic)

    if result and topic:
        if best_snap and len(best_snap) >= len(joined):
            _WS_RESP_BUFFER[topic] = _ws_merge_text(_WS_RESP_BUFFER.get(topic, ""), best_snap)
        else:
            if joined:
                _ws_buffer_append(topic, joined)
            if best_snap:
                _WS_RESP_BUFFER[topic] = _ws_merge_text(
                    _WS_RESP_BUFFER.get(topic, ""), best_snap
                )
        if finished:
            _WS_RESP_FINISHED[topic] = True

    if finished or (topic and _WS_RESP_FINISHED.get(topic)):
        if conv_complete and conv_id:
            flushed = _ws_flush_conversation(conv_id, extra_topic=topic) or result or ""
        else:
            flushed = _ws_buffer_flush(topic) or result or ""
            if not flushed and conv_id:
                flushed = _ws_flush_conversation(conv_id, extra_topic=topic)
        _SSE_ANSWER_VISIBLE.pop(vis_key, None)
        if flushed:
            return flushed, type_counts, True

    # Still streaming across tees — keep buffering, do not emit partials.
    if topic and _WS_RESP_BUFFER.get(topic) and not finished:
        return None, type_counts, False

    # One-shot tee that already contains finished_successfully / stream_complete.
    if result and finished:
        return result, type_counts, True

    return None, type_counts, False


def _capture_binary_conversation_stream(req: Dict[str, Any], payload: bytes) -> None:
    """Save ChatGPT conversation bodies that are no longer plain SSE.

    The live failure starts with ``8b 10 80`` then ``event: delta_encoding`` and
    a compressed tail (``?? 11 00 40``). Wire pcaps stay encrypted; this is the
    decrypted body plus the headers the proxy actually forwarded.
    """
    raw = payload or b""
    if not raw.startswith(b"\x8b\x10\x80") and b"\x11\x00\x40" not in raw[:80]:
        return
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    headers = meta.get("response_headers") if isinstance(meta.get("response_headers"), dict) else {}
    wanted = (
        "content-encoding",
        "content-type",
        "vary",
        "use-as-dictionary",
        "available-dictionary",
        "dictionary-id",
    )
    found = {}
    for key, value in headers.items():
        if str(key).lower() in wanted and value:
            found[str(key).lower()] = str(value)[:180]
    ce = str(req.get("content_encoding") or meta.get("content_encoding") or "")
    if ce:
        found["content-encoding"] = ce[:180]
    logger.info(
        "chatgpt binary_stream payload_len=%d content_type=%s meta_keys=%s headers=%s head=%s",
        len(raw),
        str(req.get("content_type") or "")[:80],
        ",".join(sorted(str(k) for k in meta.keys())[:12]),
        found or "-",
        raw[:24].hex(),
    )
    try:
        import time as _time

        dump_dir = os.environ.get(
            "AIVAX_GPT_STREAM_CAPTURE_DIR",
            "/home1/aivax/ai_engine/var/gpt_stream_capture",
        )
        os.makedirs(dump_dir, exist_ok=True)
        rid = str(req.get("request_id") or "unknown")[:36]
        dump_path = os.path.join(dump_dir, f"{int(_time.time())}_{rid}_{len(raw)}.bin")
        with open(dump_path, "wb") as fh:
            fh.write(raw[: 2 * 1024 * 1024])
        logger.info("chatgpt binary_stream dumped path=%s", dump_path)
    except Exception:
        logger.debug("chatgpt binary_stream dump failed", exc_info=True)


_UNAUTH_MISS_DUMP_LIMIT = 200


def _dump_unauth_updates_miss(req: Dict[str, Any], payload: bytes) -> str:
    """Persist an unauth turn body the extractor could not read. Bounded on disk."""
    try:
        dump_dir = os.environ.get(
            "AIVAX_UNAUTH_DUMP_DIR", "/home1/aivax/ai_engine/var/unauth_updates"
        )
        os.makedirs(dump_dir, exist_ok=True)
        if len(os.listdir(dump_dir)) >= _UNAUTH_MISS_DUMP_LIMIT:
            return ""
        rid = str(req.get("request_id") or "unknown")[:36]
        dump_path = os.path.join(dump_dir, f"{rid}_{len(payload)}.bin")
        with open(dump_path, "wb") as fh:
            fh.write(payload[: 2 * 1024 * 1024])
        return dump_path
    except Exception:
        logger.debug("unauth updates dump failed", exc_info=True)
        return ""


def extract_full_body(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    """Suricata eve / Deep INLINE full-body path: SSE or Celsius WS assistant text."""
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    unit = (req.get("unit_type") or "").lower()
    host = str(meta.get("host") or "")
    # Inline response tee always sets _eve_full_body, so the profile JSON lands here.
    if _chatgpt_account_response_path(path):
        _note_chatgpt_account_response(req, payload)
        req["_extract_final"] = True
        return ""
    if _is_codex_responses_path(path) and (
        unit == "ws_frame" or (req.get("direction") or "").lower() == "request"
    ):
        if (req.get("direction") or "").lower() == "request":
            prompt = _extract_codex_request_prompt(req, payload or b"")
            if prompt:
                req["_extract_final"] = True
            return prompt or ""
        return _extract_codex_ws_response(req, payload or b"")
    if _is_unauth_mweb_updates_path(path):
        answer = _extract_unauth_mweb_assistant_text(payload or b"")
        if answer:
            req["_extract_final"] = True
            logger.info(
                "chatgpt unauth_updates answer ok bytes=%d result_len=%d",
                len(payload or b""),
                len(answer),
            )
            return answer
        lowered = _safe_utf8(payload or b"").lower()
        # Anonymous turns log the prompt, so a miss here becomes a blank answer in
        # the UI half an hour later. Keep the body that defeated the extractor and
        # warn, otherwise the next occurrence is just as unexplainable as the last.
        dump_path = _dump_unauth_updates_miss(req, payload or b"")
        logger.warning(
            "chatgpt unauth_updates answer miss bytes=%d role=%d conv=%d markdown=%d "
            "template=%d head=%s dump=%s",
            len(payload or b""),
            lowered.count("data-message-role"),
            lowered.count("data-conversation"),
            lowered.count("markdown"),
            lowered.count("<template"),
            (payload or b"")[:12].hex(),
            dump_path or "-",
        )
        req["_extract_final"] = False
        return None
    if unit == "ws_frame" or "/ws/user" in path.lower() or host.lower() == "ws.chatgpt.com":
        conv_id = _conversation_id_from_raw_bytes(payload or b"")
        if conv_id:
            req["_chatgpt_session_id"] = conv_id
        result, type_counts, is_final = _extract_ws_assistant_text(payload or b"")
        _answer_trace_dump(
            req,
            payload or b"",
            "ws",
            result_len=len(result or ""),
            final=is_final,
            types=",".join(sorted(type_counts)) or "-",
            buf_topics=len(_WS_RESP_BUFFER),
        )
        if result and is_final:
            parent = resolve_resume_parent_request_id(payload or b"")
            if not parent and "conversation-update" in type_counts:
                # The prompt request may not be registered yet. Keep the answer
                # until that request's handoff stores this conversation id.
                if conv_id:
                    _stash_pending_answer(conv_id, result)
                logger.info(
                    "chatgpt extract_ws conversation_update_no_parent payload_len=%d result_len=%d conv=%s",
                    len(payload or b""),
                    len(result),
                    (conv_id or "-")[:36],
                )
                req["_extract_final"] = False
                return None
            req["_extract_final"] = True
            if parent:
                req["_chatgpt_ui_message_id"] = parent
            logger.info(
                "chatgpt extract_ws ok payload_len=%d result_len=%d types=%s final=%s parent=%s path=%s",
                len(payload or b""),
                len(result),
                type_counts,
                is_final,
                (parent or "-")[:36],
                path[:80],
            )
            return result
        buffering = bool(_WS_RESP_BUFFER)
        if buffering:
            logger.debug(
                "chatgpt extract_ws buffering payload_len=%d types=%s buf_topics=%d path=%s",
                len(payload or b""),
                type_counts,
                len(_WS_RESP_BUFFER),
                path[:80],
            )
            req["_extract_final"] = False
            return None
        if type_counts or len(payload or b"") >= 64:
            logger.info(
                "chatgpt extract_ws empty payload_len=%d types=%s frames=%d path=%s",
                len(payload or b""),
                type_counts,
                len(_parse_ws_payloads(payload or b"")),
                path[:80],
            )
            if 64 <= len(payload or b"") <= 262144 and not result:
                try:
                    import time as _time

                    dump_dir = os.environ.get(
                        "AIVAX_EMPTY_WS_DUMP_DIR",
                        "/home1/aivax/ai_engine/var/empty_ws",
                    )
                    os.makedirs(dump_dir, exist_ok=True)
                    rid = str(req.get("request_id") or "unknown")[:36]
                    dump_path = os.path.join(
                        dump_dir,
                        f"{int(_time.time_ns())}_{rid}_{len(payload or b'')}.ws",
                    )
                    with open(dump_path, "wb") as fh:
                        fh.write(payload or b"")
                    logger.info("chatgpt extract_ws empty dumped path=%s", dump_path)
                except Exception:
                    logger.debug("chatgpt empty WS dump failed", exc_info=True)
        req["_extract_final"] = False
        return None

    text = _safe_utf8(payload)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]

    assembler = _DeltaMessageAssembler()
    error_hints: List[str] = []
    type_counts: Dict[str, int] = {}
    snapshots: List[str] = []
    saw_tool_call = False
    for block in blocks:
        for line in block.split("\n"):
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            cid = _extract_conversation_id(obj)
            if cid:
                req["_chatgpt_session_id"] = cid
            t = obj.get("type")
            if isinstance(t, str) and t:
                type_counts[t] = type_counts.get(t, 0) + 1
                if _responses_event_is_tool_call(t, obj) or _responses_output_waits_on_client(obj):
                    saw_tool_call = True
            snap = _extract_responses_snapshot_text(obj)
            if snap:
                snapshots.append(snap)
            rid = obj.get("response_id")
            if isinstance(rid, str) and rid:
                req["_chatgpt_session_id"] = rid
            resp = obj.get("response")
            if isinstance(resp, dict):
                rid = resp.get("id") or resp.get("response_id")
                if isinstance(rid, str) and rid:
                    req["_chatgpt_session_id"] = rid
            err = _extract_sse_error_text(obj)
            if err:
                error_hints.append(err)
            assembler.feed(obj)

    result = assembler.answer_text()
    if not result and snapshots:
        result = max(snapshots, key=len)

    # Bind the handoff topic to this turn even when the frame carries control
    # types we do not model yet. Without the binding the later WS answer has no
    # parent turn and falls back to the long-lived WS connection id, so every
    # answer after the first is dropped as a duplicate.
    current_request = str(req.get("request_id") or "")
    handoff_keys: List[str] = []
    if _CHATGPT_SSE_HANDOFF_TYPES.intersection(type_counts):
        handoff_keys = register_stream_handoff_parent(payload or b"", current_request)

    parent = resolve_resume_parent_request_id(payload or b"") or _user_message_parent(
        payload or b""
    )
    if parent and parent != current_request:
        req["_chatgpt_ui_message_id"] = parent
    if not result:
        pending = _pending_answer_for_payload(payload or b"")
        if pending:
            result = pending
            logger.info(
                "chatgpt pending_ws_answer_attached request_id=%s result_len=%d",
                current_request[:36],
                len(result),
            )

    _answer_trace_dump(
        req,
        payload or b"",
        "sse",
        result_len=len(result or ""),
        parent=(str(req.get("_chatgpt_ui_message_id") or "-"))[:36],
        types=",".join(sorted(type_counts)) or "-",
        path=path.split("?", 1)[0][:80],
    )
    if not result:
        _answer_trace_watch(current_request, payload or b"", handoff_keys)

    # An app-started turn (ambient suggestions, title, compaction) is not an
    # answer to something the user typed. A Codex turn that invokes a tool,
    # even when it also carries a short status line, is not the visible answer.
    # That text arrives on the next response, after the tool result.
    app_turn = _is_codex_responses_path(path) and (
        _codex_turn_is_app_initiated(None, req) or _codex_connection_in_app_turn(req)
    )
    codex_tool_turn = _is_codex_responses_path(path) and saw_tool_call
    if app_turn or codex_tool_turn or (not result and saw_tool_call):
        req["_chatgpt_sse_control_only"] = True
        req["_chatgpt_sse_empty_meta"] = {
            "payload_len": len(payload or b""),
            "types": type_counts,
            "app_turn": app_turn,
            "tool_call": saw_tool_call,
        }
        logger.info(
            "chatgpt extract_full_body control_only payload_len=%d types=%s app_turn=%s tool_call=%s",
            len(payload or b""),
            type_counts,
            app_turn,
            saw_tool_call,
        )
        return None

    if result:
        req["_extract_final"] = True
        logger.info(
            "chatgpt extract_full_body ok payload_len=%d result_len=%d types=%s path=%s",
            len(payload or b""),
            len(result),
            type_counts,
            path[:80],
        )
        if req.get("_chatgpt_ui_message_id"):
            logger.info(
                "chatgpt resume parent remap request_id=%s parent=%s result_len=%d",
                current_request[:36],
                str(req.get("_chatgpt_ui_message_id") or "")[:36],
                len(result),
            )
        return result

    if type_counts and set(type_counts).issubset(_CHATGPT_SSE_CONTROL_TYPES):
        req["_chatgpt_sse_control_only"] = True
        req["_chatgpt_sse_empty_meta"] = {
            "payload_len": len(payload or b""),
            "types": type_counts,
        }
        logger.info(
            "chatgpt extract_full_body control_only payload_len=%d types=%s handoff_keys=%d",
            len(payload or b""),
            type_counts,
            len(handoff_keys),
        )
        return None

    if error_hints and not type_counts.intersection(_CHATGPT_SSE_CONTROL_TYPES):
        err_text = error_hints[-1]
        req["_chatgpt_sse_error"] = err_text
        logger.info(
            "chatgpt extract_full_body sse_error payload_len=%d types=%s err=%r",
            len(payload or b""),
            type_counts,
            err_text[:200],
        )
        return None

    data_lines = sum(1 for line in text.split("\n") if line.startswith("data:"))
    if data_lines or len(text) > 0:
        logger.info(
            "chatgpt extract_full_body empty payload_len=%d data_lines=%d types=%s head=%r",
            len(payload or b""),
            data_lines,
            type_counts,
            text[:240],
        )
        req["_chatgpt_sse_empty"] = True
        req["_chatgpt_sse_empty_meta"] = {
            "payload_len": len(payload or b""),
            "data_lines": data_lines,
            "types": type_counts,
        }
        _capture_binary_conversation_stream(req, payload or b"")
        if len(payload or b"") <= 8192:
            try:
                import time as _time

                dump_dir = os.environ.get(
                    "AIVAX_EMPTY_SSE_DUMP_DIR",
                    "/home/juyonglee/AIVAX/inline_engine/aivax/evidence/PROVE/I350_BYPASS_1G/empty_sse",
                )
                os.makedirs(dump_dir, exist_ok=True)
                rid = str(req.get("request_id") or "unknown")[:36]
                dump_path = os.path.join(dump_dir, f"{int(_time.time())}_{rid}.sse")
                with open(dump_path, "wb") as fh:
                    fh.write(payload or b"")
                logger.info("chatgpt extract_full_body empty dumped path=%s", dump_path)
            except Exception:
                logger.debug("chatgpt empty SSE dump failed", exc_info=True)
        return None
    return None


def rewrite_always(req: Dict[str, Any], payload: bytes) -> Optional[Tuple[bytes, str]]:
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "request":
        return None

    cfg = req.get("_engine_cfg")
    forced_text = ""
    if isinstance(cfg, dict):
        maybe = cfg.get("chatgpt_force_request_text")
        if isinstance(maybe, str):
            forced_text = maybe
    if not forced_text:
        return None

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    if _is_unauth_mweb_updates_path(path):
        encoded = _rewrite_unauth_mweb_form_prompt(payload, forced_text)
        if encoded is None:
            return None
        return encoded, "adapter:chatgpt_force_request_text"

    if not (unit_type == "json_body" or "application/json" in content_type):
        return None

    out = _rewrite_request_payload(payload, forced_text, allow_empty=False)
    if out is None:
        return None

    encoded, _ = out
    return encoded, "adapter:chatgpt_force_request_text"


_CHATGPT_TOP_FILE_KEYS = ("attachments", "file_ids", "attachment_ids")
_CHATGPT_PART_FILE_KEYS = ("file_id", "id", "asset_pointer", "image_url", "mime_type")


def _last_user_message(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    messages = obj.get("messages")
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and _message_role(msg) == "user":
            return msg
    return None


def _is_file_part(part: Any) -> bool:
    return isinstance(part, dict) and any(part.get(k) for k in _CHATGPT_PART_FILE_KEYS)


def consent_attachment_state(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    """File references of the blocked turn. The upload already reached OpenAI."""
    if (req.get("direction") or "").lower() != "request":
        return None
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    top = {
        key: json.loads(json.dumps(obj[key]))
        for key in _CHATGPT_TOP_FILE_KEYS
        if isinstance(obj.get(key), list) and obj[key]
    }
    meta_attachments: List[Any] = []
    parts: List[Any] = []
    content_type = ""
    msg = _last_user_message(obj)
    if msg is not None:
        meta = msg.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("attachments"), list):
            meta_attachments = json.loads(json.dumps(meta["attachments"]))
        content = msg.get("content")
        if isinstance(content, dict) and isinstance(content.get("parts"), list):
            parts = [json.loads(json.dumps(p)) for p in content["parts"] if _is_file_part(p)]
            if parts:
                content_type = str(content.get("content_type") or "")
    if not top and not meta_attachments and not parts:
        return None
    return {
        "top": top,
        "meta_attachments": meta_attachments,
        "parts": parts,
        "content_type": content_type,
    }


def restore_consent_attachments(
    req: Dict[str, Any],
    payload: bytes,
    state: Any,
    prompt: str = "",
) -> Optional[bytes]:
    if not isinstance(state, dict):
        return None
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    for key, value in (state.get("top") or {}).items():
        obj[key] = value
    meta_attachments = list(state.get("meta_attachments") or [])
    parts = list(state.get("parts") or [])
    if meta_attachments or parts:
        msg = _last_user_message(obj)
        if msg is None:
            return None
        if meta_attachments:
            meta = msg.get("metadata")
            if not isinstance(meta, dict):
                meta = {}
                msg["metadata"] = meta
            meta["attachments"] = meta_attachments
        if parts:
            content = msg.get("content")
            if not isinstance(content, dict):
                return None
            text_parts = [p for p in (content.get("parts") or []) if not _is_file_part(p)]
            content["parts"] = parts + text_parts
            content["content_type"] = state.get("content_type") or "multimodal_text"
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def rewrite_prompt(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    policy_code: str,
) -> Optional[Tuple[bytes, str]]:
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "request":
        return None

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    if _is_unauth_mweb_updates_path(path):
        encoded = _rewrite_unauth_mweb_form_prompt(payload, replacement_text)
        if encoded is None:
            return None
        code = (policy_code or "masking").strip().lower()
        return encoded, f"adapter:chatgpt_policy_{code}"

    if not (unit_type == "json_body" or "application/json" in content_type):
        return None

    out = _rewrite_request_payload(payload, replacement_text, allow_empty=True)
    if out is None:
        return None
    encoded, _ = out

    code = (policy_code or "masking").strip().lower()
    return encoded, f"adapter:chatgpt_policy_{code}"


def rewrite(
    req: Dict[str, Any],
    payload: bytes,
    src: str,
    dst: str,
) -> Optional[Tuple[bytes, int]]:
    return None


def extract_session_id(req: Dict[str, Any], payload: bytes) -> str:
    session_hint = str(req.get("_chatgpt_session_id") or "")
    if session_hint and not session_hint.startswith("pending:"):
        return session_hint

    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    text = _safe_utf8(payload)
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    host = str(meta.get("host") or "")

    if direction == "request" and _is_unauth_mweb_updates_path(path):
        try:
            form = {k: v for k, v in parse_qsl(text, keep_blank_values=True)}
        except Exception:
            return ""
        sid = str(form.get("oai-session-id") or "").strip()
        if sid:
            return sid
        mid = str(form.get("userMessageId") or "").strip()
        return mid

    if direction == "request" and (unit_type == "json_body" or "application/json" in content_type):
        try:
            obj = json.loads(text)
        except Exception:
            return ""
        if not isinstance(obj, dict):
            return ""
        for key in ("conversation_id", "session_id", "previous_response_id", "response_id"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    if direction == "response" and (unit_type == "sse_event" or "text/event-stream" in content_type):
        if text.strip() == "[DONE]":
            key = _peek_done_key(req)
            if key:
                cached = _SSE_RESPONSE_META.get(key, {})
                session = str(cached.get("session_id") or "")
                return session or key
            return ""
        for obj in _iter_sse_json_objs(payload or b""):
            conversation_id = _extract_conversation_id(obj)
            if conversation_id:
                req["_chatgpt_session_id"] = conversation_id
                return conversation_id
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            conversation_id = _extract_conversation_id(obj)
            if conversation_id:
                req["_chatgpt_session_id"] = conversation_id
                return conversation_id

    if direction == "response" and (
        unit_type == "ws_frame"
        or "/ws/user" in path.lower()
        or host.lower() == "ws.chatgpt.com"
    ):
        conversation_id = _conversation_id_from_raw_bytes(payload or b"")
        if conversation_id:
            req["_chatgpt_session_id"] = conversation_id
            return conversation_id

    stream_key = str(req.get("_chatgpt_stream_key") or "")
    if stream_key and stream_key != _SSE_FALLBACK_KEY:
        cached = _SSE_RESPONSE_META.get(stream_key, {})
        session = str(cached.get("session_id") or "")
        return session or stream_key
    if direction == "response" and (unit_type == "sse_event" or "text/event-stream" in content_type):
        if stream_key and stream_key in _SSE_RESPONSE_META:
            cached = _SSE_RESPONSE_META.get(stream_key, {})
            session = str(cached.get("session_id") or "")
            if session:
                return session
        if len(_SSE_RESPONSE_META) == 1:
            key = next(iter(_SSE_RESPONSE_META))
            cached = _SSE_RESPONSE_META.get(key, {})
            session = str(cached.get("session_id") or "")
            if session:
                return session

    return ""


def _rewrite_codex_output_text(item: Any, replacement: str) -> int:
    if not isinstance(item, dict):
        return 0
    changed = 0
    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str) and part["text"] != replacement:
                part["text"] = replacement
                changed += 1
    if isinstance(item.get("text"), str) and item["text"] != replacement:
        item["text"] = replacement
        changed += 1
    return changed


def _rewrite_codex_ws_obj(obj: Dict[str, Any], replacement: str, state_key: str) -> int:
    """Put the block phrase on the first delta and on the terminal snapshot.

    Later deltas are cleared so the model answer is not appended. The completed
    snapshot keeps the phrase: the IDE stores that object, not the delta stream.
    """
    etype = str(obj.get("type") or "")
    if etype in _RESPONSES_SSE_DELTA_TYPES and isinstance(obj.get("delta"), str):
        emitted = bool(_SSE_RESPONSE_OVERRIDE_EMITTED.get(state_key, False))
        target = "" if emitted else replacement
        changed = 0
        if obj["delta"] != target:
            obj["delta"] = target
            changed = 1
        _SSE_RESPONSE_OVERRIDE_EMITTED[state_key] = True
        return changed
    if etype in ("response.output_text.done", "response.text.done"):
        return _rewrite_codex_output_text(obj, replacement)
    if etype == "response.content_part.done":
        return _rewrite_codex_output_text(obj.get("part"), replacement)
    if etype == "response.output_item.done":
        return _rewrite_codex_output_text(obj.get("item"), replacement)
    if etype in ("response.completed", "response.done"):
        changed = 0
        resp = obj.get("response")
        if isinstance(resp, dict):
            rows = resp.get("output")
            if isinstance(rows, list):
                for row in rows:
                    changed += _rewrite_codex_output_text(row, replacement)
        _SSE_RESPONSE_OVERRIDE_EMITTED.pop(state_key, None)
        return changed
    return 0


def _rewrite_codex_ws_response(
    req: Dict[str, Any], payload: bytes, replacement_text: str
) -> Optional[Tuple[bytes, str]]:
    replacement = (replacement_text or "").strip()
    if not replacement:
        return None
    state_key = str(req.get("_chatgpt_stream_key") or "") or _SSE_FALLBACK_KEY
    out = bytearray()
    changed = 0
    saw_frame = False
    for opcode, data, original in _iter_ws_frames(payload or b""):
        saw_frame = True
        if opcode not in (0x1, 0x2):
            out += original
            continue
        try:
            obj = json.loads(data)
        except Exception:
            out += original
            continue
        if not isinstance(obj, dict):
            out += original
            continue
        key = _codex_response_stream_key(obj, req) or state_key
        n = _rewrite_codex_ws_obj(obj, replacement, key)
        if n <= 0:
            out += original
            continue
        changed += n
        encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # Server→client frames are unmasked.
        out += _encode_ws_text_frame(encoded, masked=False)
    if not saw_frame or changed <= 0:
        return None
    return bytes(out), "adapter:chatgpt_policy_block_message"


def response_stream_done(req: Dict[str, Any], payload: bytes) -> bool:
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "response":
        return False
    if unit_type == "ws_frame":
        for _opcode, data, _original in _iter_ws_frames(payload or b""):
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if isinstance(obj, dict) and str(obj.get("type") or "") in (
                "response.completed",
                "response.done",
            ):
                return True
        return False
    if not (unit_type == "sse_event" or "text/event-stream" in content_type):
        return False

    text = _safe_utf8(payload)
    if text.strip() == "[DONE]":
        return True

    try:
        obj = json.loads(text)
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    return obj.get("type") == "message_stream_complete"


def _rewrite_full_message_content(obj: Dict[str, Any], replacement_text: str, state_key: str) -> int:
    """Rewrite content in full-message delta or message_stream_complete events.

    Handles:
    - {"v": {"message": {"content": {"parts": [...]}, ...}}}
    - {"message": {"content": {"parts": [...]}, ...}}
    """
    changed = 0

    v = obj.get("v")
    if isinstance(v, dict):
        msg = v.get("message")
        if isinstance(msg, dict) and _message_role(msg) == "assistant":
            content = msg.get("content")
            if isinstance(content, dict):
                parts = content.get("parts")
                if isinstance(parts, list) and parts:
                    content["parts"] = [replacement_text]
                    _SSE_RESPONSE_OVERRIDE_EMITTED[state_key] = True
                    changed += 1

    msg = obj.get("message")
    if isinstance(msg, dict) and _message_role(msg) == "assistant":
        content = msg.get("content")
        if isinstance(content, dict):
            parts = content.get("parts")
            if isinstance(parts, list) and parts:
                content["parts"] = [replacement_text]
                _SSE_RESPONSE_OVERRIDE_EMITTED[state_key] = True
                changed += 1

    return changed


def _rewrite_parts_stream_chunk(
    op_lc: str,
    text: str,
    replacement_text: str,
    emitted: bool,
) -> Tuple[Optional[str], int, bool]:
    """Policy rewrite for one assistant text chunk (append/replace/add). Returns (new_text, changed, emitted_out)."""
    if op_lc == "replace":
        emitted = True
        if text != replacement_text:
            return replacement_text, 1, emitted
        return text, 0, emitted
    if op_lc not in ("append", "add"):
        return text, 0, emitted
    if emitted:
        return None, 1, emitted
    emitted = True
    if text != replacement_text:
        return replacement_text, 1, emitted
    return replacement_text, 0, emitted


def _rewrite_sse_patch(obj: Dict[str, Any], replacement_text: str, state_key: str) -> int:
    emitted = bool(_SSE_RESPONSE_OVERRIDE_EMITTED.get(state_key, False))
    changed = 0
    path = obj.get("p")
    op = obj.get("o")
    val = obj.get("v")

    # Root-level single frame (matches _extract_root_parts_delta_text).
    # Non-string ``v`` must be handled here too; otherwise extract() sees text but rewrite misses it (leak).
    if isinstance(path, str) and path.startswith("/message/content/parts/") and isinstance(op, str):
        op_lc = op.lower()
        if op_lc in ("append", "replace", "add"):
            if isinstance(val, str):
                chunk_src = val
            elif isinstance(val, (int, float, bool)) or val is None:
                chunk_src = ""
            else:
                chunk_src = _extract_content(val)
            new_text, d, emitted = _rewrite_parts_stream_chunk(
                op_lc, chunk_src, replacement_text, emitted
            )
            changed += d
            obj["v"] = "" if new_text is None else new_text
            _SSE_RESPONSE_OVERRIDE_EMITTED[state_key] = emitted
            return changed

    # Bare continuation {"v":"…"} without JSON Pointer ``p`` (same rule as extraction).
    if isinstance(val, str) and val and (path is None or path == ""):
        new_text, d, emitted = _rewrite_parts_stream_chunk(
            "append", val, replacement_text, emitted
        )
        changed += d
        obj["v"] = "" if new_text is None else new_text
        _SSE_RESPONSE_OVERRIDE_EMITTED[state_key] = emitted
        return changed

    patch_list = val if isinstance(val, list) else None
    if not isinstance(patch_list, list):
        return 0

    new_patch_list = []
    for item in patch_list:
        if not isinstance(item, dict):
            new_patch_list.append(item)
            continue
        item_path = item.get("p")
        item_op = item.get("o")
        raw_v = item.get("v")
        if item_path == "/message/metadata/token_count":
            # 테스트 목적: token_count 메타 패치는 제거한다.
            changed += 1
            continue
        if not isinstance(item_path, str) or not isinstance(item_op, str):
            new_patch_list.append(item)
            continue
        if isinstance(raw_v, str):
            text = raw_v
        elif isinstance(raw_v, (int, float, bool)) or raw_v is None:
            text = ""
        else:
            text = _extract_content(raw_v)
        if not item_path.startswith("/message/content/parts/"):
            new_patch_list.append(item)
            continue
        op_lc = item_op.lower()
        if op_lc not in ("append", "replace", "add"):
            new_patch_list.append(item)
            continue

        new_text, d, emitted = _rewrite_parts_stream_chunk(
            op_lc, text, replacement_text, emitted
        )
        changed += d
        if new_text is None:
            continue
        if text != new_text:
            item["v"] = new_text
        new_patch_list.append(item)

    if len(new_patch_list) != len(patch_list):
        obj["v"] = new_patch_list

    _SSE_RESPONSE_OVERRIDE_EMITTED[state_key] = emitted
    return changed


def _rewrite_responses_api_sse(
    obj: Dict[str, Any],
    replacement_text: str,
    session_id: str,
) -> int:
    etype = str(obj.get("type") or "")
    if etype not in _RESPONSES_SSE_DELTA_TYPES and etype not in _RESPONSES_SSE_DONE_TYPES:
        return 0
    state_key = session_id or _SSE_FALLBACK_KEY
    emitted = bool(_SSE_RESPONSE_OVERRIDE_EMITTED.get(state_key, False))
    target = replacement_text if not emitted else ""
    changed = 0
    if etype in _RESPONSES_SSE_DELTA_TYPES and isinstance(obj.get("delta"), str):
        if obj["delta"] != target:
            obj["delta"] = target
            changed += 1
        emitted = True
    elif etype in ("response.output_text.done", "response.text.done") and isinstance(
        obj.get("text"), str
    ):
        if obj["text"] != target:
            obj["text"] = target
            changed += 1
        emitted = True
    elif etype in ("response.completed", "response.done"):
        resp = obj.get("response")
        if isinstance(resp, dict):
            out = resp.get("output")
            if isinstance(out, list):
                for row in out:
                    if not isinstance(row, dict):
                        continue
                    content = row.get("content")
                    if not isinstance(content, list):
                        continue
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if isinstance(part.get("text"), str) and part["text"] != target:
                            part["text"] = target
                            changed += 1
        emitted = True
    _SSE_RESPONSE_OVERRIDE_EMITTED[state_key] = emitted
    return changed


def _mutate_single_sse_response_obj(
    obj: Dict[str, Any],
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
) -> Tuple[int, str]:
    session_id = (
        str(req.get("_chatgpt_stream_key") or "")
        or _extract_conversation_id(obj)
        or extract_session_id(req, payload)
        or _SSE_FALLBACK_KEY
    )
    changed = 0
    changed += _rewrite_responses_api_sse(obj, replacement_text, session_id)
    if changed <= 0:
        changed += _rewrite_sse_patch(obj, replacement_text, session_id)
    if changed <= 0:
        changed += _rewrite_full_message_content(obj, replacement_text, session_id)
    if obj.get("type") == "message_stream_complete" and session_id:
        _SSE_RESPONSE_OVERRIDE_EMITTED.pop(session_id, None)
    return changed, session_id


def rewrite_response(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
) -> Optional[Tuple[bytes, str]]:
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "response":
        return None
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    if unit_type == "ws_frame" and _is_codex_responses_path(str(meta.get("path") or "")):
        return _rewrite_codex_ws_response(req, payload, replacement_text)
    if not (unit_type == "sse_event" or "text/event-stream" in content_type):
        return None

    text = _safe_utf8(payload)
    if text.strip() == "[DONE]":
        session_id = extract_session_id(req, payload)
        if session_id:
            _SSE_RESPONSE_OVERRIDE_EMITTED.pop(session_id, None)
        return None

    blobs_in, use_data_prefix = _coerce_sse_json_blobs(text)
    if not blobs_in:
        return None

    if len(blobs_in) == 1:
        blob = blobs_in[0]
        try:
            obj = json.loads(blob)
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        changed, _ = _mutate_single_sse_response_obj(obj, req, payload, replacement_text)
        if changed <= 0:
            return None
        out_json = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        out = f"data: {out_json}" if use_data_prefix else out_json
        return out.encode("utf-8"), "adapter:chatgpt_policy_block_message"

    out_blobs: List[str] = []
    total_changed = 0
    for blob in blobs_in:
        if blob.strip() == "[DONE]":
            out_blobs.append(blob.strip())
            continue
        try:
            obj = json.loads(blob)
        except Exception:
            out_blobs.append(blob)
            continue
        if not isinstance(obj, dict):
            out_blobs.append(blob)
            continue
        changed, _ = _mutate_single_sse_response_obj(obj, req, payload, replacement_text)
        if changed > 0:
            total_changed += changed
            out_blobs.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        else:
            out_blobs.append(blob)

    if total_changed <= 0:
        return None
    encoded = "\n\n".join(f"data: {b}" for b in out_blobs).encode("utf-8")
    return encoded, "adapter:chatgpt_policy_block_message"


def _join_response_prefix(prefix: str, text: str) -> str:
    lead = str(prefix or "").rstrip()
    body = str(text or "")
    if not lead:
        return body
    if body.startswith(lead):
        return body
    return f"{lead}\n\n{body}"


def _prefix_sse_obj(obj: Dict[str, Any], prefix: str, state_key: str) -> int:
    """Put the notice on the first assistant text chunk and leave the rest."""
    if _SSE_RESPONSE_PREFIX_EMITTED.get(state_key):
        return 0
    etype = str(obj.get("type") or "")
    if etype in _RESPONSES_SSE_DELTA_TYPES and isinstance(obj.get("delta"), str) and obj["delta"]:
        obj["delta"] = _join_response_prefix(prefix, obj["delta"])
        _SSE_RESPONSE_PREFIX_EMITTED[state_key] = True
        return 1
    path = obj.get("p")
    op = str(obj.get("o") or "").lower()
    val = obj.get("v")
    if (
        isinstance(path, str)
        and path.startswith("/message/content/parts/")
        and op in ("append", "replace", "add")
        and isinstance(val, str)
        and val
    ):
        obj["v"] = _join_response_prefix(prefix, val)
        _SSE_RESPONSE_PREFIX_EMITTED[state_key] = True
        return 1
    if isinstance(val, str) and val and (path is None or path == ""):
        obj["v"] = _join_response_prefix(prefix, val)
        _SSE_RESPONSE_PREFIX_EMITTED[state_key] = True
        return 1
    if isinstance(val, list):
        for item in val:
            if not isinstance(item, dict):
                continue
            item_path = item.get("p")
            item_op = str(item.get("o") or "").lower()
            item_val = item.get("v")
            if (
                isinstance(item_path, str)
                and item_path.startswith("/message/content/parts/")
                and item_op in ("append", "replace", "add")
                and isinstance(item_val, str)
                and item_val
            ):
                item["v"] = _join_response_prefix(prefix, item_val)
                _SSE_RESPONSE_PREFIX_EMITTED[state_key] = True
                return 1
    for holder in (obj.get("v"), obj):
        if not isinstance(holder, dict):
            continue
        msg = holder.get("message")
        if not isinstance(msg, dict) or _message_role(msg) != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], str) and parts[0]:
            parts[0] = _join_response_prefix(prefix, parts[0])
            _SSE_RESPONSE_PREFIX_EMITTED[state_key] = True
            return 1
    return 0


def prefix_response(
    req: Dict[str, Any],
    payload: bytes,
    prefix: str,
) -> Optional[Tuple[bytes, str]]:
    """Prepend a notice to the first assistant text. Later chunks stay as the model wrote them."""
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "response":
        return None
    if not (unit_type == "sse_event" or "text/event-stream" in content_type):
        return None
    lead = str(prefix or "").strip()
    if not lead:
        return None
    state_key = str(req.get("_cso_response_prefix_key") or "").strip() or _SSE_FALLBACK_KEY
    text = _safe_utf8(payload)
    if text.strip() == "[DONE]":
        _SSE_RESPONSE_PREFIX_EMITTED.pop(state_key, None)
        return None
    blobs_in, use_data_prefix = _coerce_sse_json_blobs(text)
    if not blobs_in:
        return None
    changed = 0
    out_blobs: List[str] = []
    for blob in blobs_in:
        if blob.strip() == "[DONE]":
            out_blobs.append(blob.strip())
            continue
        try:
            obj = json.loads(blob)
        except Exception:
            out_blobs.append(blob)
            continue
        if not isinstance(obj, dict):
            out_blobs.append(blob)
            continue
        if _prefix_sse_obj(obj, lead, state_key) > 0:
            changed += 1
            out_blobs.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        else:
            out_blobs.append(blob)
    if changed <= 0:
        return None
    if len(out_blobs) == 1 and not use_data_prefix:
        encoded = out_blobs[0]
    else:
        encoded = "\n\n".join(
            b if b.strip() == "[DONE]" or b.startswith("data:") else f"data: {b}"
            for b in out_blobs
        )
    return encoded.encode("utf-8"), "adapter:chatgpt_cso_open_notice"


def clear_response_prefix_state(session_id: str) -> None:
    key = (session_id or "").strip()
    if key:
        _SSE_RESPONSE_PREFIX_EMITTED.pop(key, None)


def clear_response_override_state(session_id: str) -> None:
    key = (session_id or "").strip()
    if not key:
        return
    _SSE_RESPONSE_OVERRIDE_EMITTED.pop(key, None)
    room_key = _ROOM_BY_CONVERSATION.get(key, "")
    if room_key:
        for turn_key, turn_room in list(_TURN_TO_ROOM.items()):
            if turn_room == room_key:
                _SSE_RESPONSE_OVERRIDE_EMITTED.pop(turn_key, None)
