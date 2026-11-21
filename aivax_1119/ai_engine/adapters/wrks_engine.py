"""
Wrks.ai Adapter

Chat turns (gateway-api.wrks.ai):
  legacy  POST /conversation/{id}/message/stream
  v2      POST /ai/chat/stream/{chatId}
  agent   POST /ai/chat/agent/{agentId}/stream/{chatId}

Response SSE: legacy thread.message.delta/completed, or AI-SDK parts
(text-delta / finish / data:[DONE]).
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import account_lookup as _account_lookup
except ImportError:
    _account_lookup = None

from ai_service_codes import AI_SERVICE_WRKS
from adapters.wrks_upload import (
    POLICY_ATTACHMENTS_FINAL,
    build_policy_attachments,
    is_wrks_file_upload_request,
    is_wrks_file_upload_response,
    normalize_upload,
    strip_chat_attachments,
    _collect_attachment_refs,
)

logger = logging.getLogger(__name__)

_CONVERSATION_PATH_RE = re.compile(
    r"/conversation/(?P<conv>[^/]+)/message/stream(?:[/?].*)?$"
)
# Agent UI v2 chat streams (2026-08): chatId is the trailing UUID.
_AI_CHAT_STREAM_PATH_RE = re.compile(
    r"/ai/chat/stream/(?P<conv>[^/]+)(?:/)?$"
)
_AI_CHAT_AGENT_STREAM_PATH_RE = re.compile(
    r"/ai/chat/agent/(?P<agent>[^/]+)/stream/(?P<conv>[^/]+)(?:/)?$"
)
_CHAT_STREAM_PATH_RES = (
    _CONVERSATION_PATH_RE,
    _AI_CHAT_STREAM_PATH_RE,
    _AI_CHAT_AGENT_STREAM_PATH_RE,
)

_WRKS_RESPONSE_BUFFER: Dict[str, str] = {}
# modelId from the chat POST → name string from GET /ai/chat/models.
_WRKS_MODEL_CATALOG: Dict[str, str] = {}
_WRKS_MODEL_NAME_KEYS = ("name", "modelName", "displayName", "label", "title", "model")
_WRKS_MODEL_REJECT = frozenset(
    {"user", "assistant", "system", "model", "human", "bot", "tool", "none", "null"}
)
_WRKS_RESPONSE_MESSAGE_ID: Dict[str, str] = {}
_WRKS_RESPONSE_OVERRIDE_EMITTED: Dict[str, bool] = {}
_WRKS_EMITTED_LOCK = threading.Lock()
_WRKS_FALLBACK_KEY = "__fallback__"
# message 필드에 붙는 이미지 분석 URL — input filter·로그용 prompt에서 제거
_ANALYZE_IMAGE_URL_IN_PROMPT_RE = re.compile(
    r"\s*\[analyzeImageUrl\]\s*https://images\.wrks\.ai/dalle/[A-Za-z0-9_-]+\.[A-Za-z0-9]+\s*",
    re.IGNORECASE,
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
        return ""
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return raw


def _extract_identity_from_jwt(token: str) -> Tuple[str, str]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return "", ""
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:
        return "", ""
    if not isinstance(claims, dict):
        return "", ""
    user_id = ""
    email = ""
    for key in ("id", "sub", "_id", "user_id", "userId"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            user_id = value.strip()
            break
    for key in ("email", "mail"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            email = value.strip()
            break
    return user_id, email


def _is_json_request(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "request":
        return False
    return unit_type == "json_body" or "application/json" in content_type


def _is_sse_response(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "response":
        return False
    return unit_type == "sse_event" or "text/event-stream" in content_type


def _wrks_path_base(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("path") or "").split("?", 1)[0]


def _is_wrks_host(req: Dict[str, Any]) -> bool:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return "gateway-api.wrks.ai" in str(meta.get("host") or "").lower()


def _match_chat_stream_path(path: str) -> Optional[re.Match[str]]:
    base = (path or "").split("?", 1)[0]
    for cre in _CHAT_STREAM_PATH_RES:
        m = cre.search(base)
        if m:
            return m
    return None


def _is_message_stream_request(req: Dict[str, Any]) -> bool:
    if not _is_wrks_host(req):
        return False
    return _match_chat_stream_path(_wrks_path_base(req)) is not None


def wrks_should_skip_policy(req: Dict[str, Any], payload: bytes = b"") -> bool:
    """Input/Output filter는 chat stream POST만 대상. 업로드·기타 API는 passthrough."""
    del payload
    if (req.get("direction") or "").lower() != "request":
        return False
    if is_wrks_file_upload_request(req):
        return True
    if _is_message_stream_request(req):
        return False
    return True


def _is_my_info_response(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    method = str(meta.get("method") or "").upper()
    path = str(meta.get("path") or "").split("?", 1)[0]
    return "gateway-api.wrks.ai" in host and method == "GET" and path.rstrip("/") == "/my/info"


def _wrks_model_text(value: Any) -> str:
    """Keep the model name Works AI sent."""
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or len(name) > 80 or name.lower() in _WRKS_MODEL_REJECT:
        return ""
    if any(ord(ch) < 32 for ch in name):
        return ""
    return name


def _wrks_path(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("path") or "").split("?", 1)[0].rstrip("/")


def _is_models_response(req: Dict[str, Any]) -> bool:
    if (req.get("direction") or "").lower() != "response":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    return "wrks.ai" in host and _wrks_path(req) == "/ai/chat/models"


def _wrks_catalog_id(obj: Dict[str, Any]) -> str:
    for key in ("id", "modelId"):
        value = obj.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _wrks_remember_models(node: Any, found: Dict[str, str], depth: int = 0) -> None:
    if depth > 6 or node is None:
        return
    if isinstance(node, list):
        for item in node[:200]:
            _wrks_remember_models(item, found, depth + 1)
        return
    if not isinstance(node, dict):
        return
    mid = _wrks_catalog_id(node)
    name = ""
    for key in _WRKS_MODEL_NAME_KEYS:
        name = _wrks_model_text(node.get(key))
        if name:
            break
    if mid and name and name != mid:
        found[mid] = name
    for value in node.values():
        if isinstance(value, (dict, list)):
            _wrks_remember_models(value, found, depth + 1)


def _wrks_note_models_response(req: Dict[str, Any], payload: bytes) -> None:
    del req
    try:
        obj = json.loads(_safe_utf8(payload or b""))
    except Exception:
        logger.info("wrks_models parse_fail bytes=%s", len(payload or b""))
        return
    found: Dict[str, str] = {}
    _wrks_remember_models(obj, found)
    if found:
        _WRKS_MODEL_CATALOG.update(found)
    sample = ",".join(f"{mid}={name}" for mid, name in list(found.items())[:8])
    logger.info("wrks_models count=%s sample=%s", len(found), sample or "-")


def _wrks_bind_model(req: Dict[str, Any], obj: Dict[str, Any]) -> str:
    value = obj.get("modelId")
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        mid = str(value)
    elif isinstance(value, str) and value.strip():
        mid = value.strip()
    else:
        return ""
    name = _WRKS_MODEL_CATALOG.get(mid, "")
    if name:
        req["_wrks_model"] = name
        logger.info("wrks_model name=%s id=%s", name, mid)
        return name
    logger.info("wrks_model id=%s catalog=miss", mid)
    return ""


def _is_auth_session_response(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    method = str(meta.get("method") or "").upper()
    path = str(meta.get("path") or "").split("?", 1)[0]
    return "wrks.ai" in host and method == "GET" and path.rstrip("/") == "/api/auth/session"


def _parse_request_json(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    if not _is_json_request(req):
        return None
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _extract_conversation_id(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    m = _match_chat_stream_path(path)
    if m:
        return str(m.group("conv") or "").strip()
    return ""


def _text_from_parts(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    chunks: List[str] = []
    for part in parts:
        if isinstance(part, str) and part.strip():
            chunks.append(part.strip())
            continue
        if not isinstance(part, dict):
            continue
        for key in ("text", "content", "value", "delta"):
            value = part.get(key)
            if isinstance(value, str) and value.strip():
                chunks.append(value.strip())
                break
    return "\n".join(chunks).strip()


def _extract_prompt(obj: Dict[str, Any]) -> str:
    message = obj.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    if isinstance(message, dict):
        nested = _extract_prompt(message)
        if nested:
            return nested
        parts_text = _text_from_parts(message.get("parts") or message.get("content"))
        if parts_text:
            return parts_text
    for key in ("prompt", "input", "query", "text", "content"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    parts_text = _text_from_parts(obj.get("parts"))
    if parts_text:
        return parts_text
    messages = obj.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or msg.get("sender") or "").lower()
            if role and role not in ("user", "human"):
                continue
            nested = _extract_prompt(msg)
            if nested:
                return nested
    return ""


def _sanitize_prompt_for_policy(text: str) -> str:
    """wrks 이미지 업로드 시 message에 붙는 [analyzeImageUrl] URL을 정책 API prompt에서 제거."""
    cleaned = _ANALYZE_IMAGE_URL_IN_PROMPT_RE.sub("", text or "")
    return cleaned.strip()


def _wrks_person_name(value: Any) -> str:
    """Person name from info.name. Workspace titles, emails, and URLs are not names."""
    if value is None or not isinstance(value, str):
        return ""
    text = " ".join(html.unescape(value).split())
    if not text or len(text) > 80 or "@" in text:
        return ""
    lowered = text.lower()
    if "/" in text or "\\" in text or lowered.startswith("http"):
        return ""
    return text


def _register_wrks_account(
    session_key: str, user_id: str, email: str, display_name: str = ""
) -> None:
    if not _account_lookup or not session_key:
        return
    if user_id or email:
        _account_lookup.set_account(
            "wrks",
            session_key,
            user_id or "",
            email or "",
            display_name=_wrks_person_name(display_name),
        )


def _wrks_cached_name(req: Dict[str, Any]) -> str:
    current = _wrks_person_name(req.get("_wrks_session_name"))
    if current:
        return current
    if not _account_lookup or not hasattr(_account_lookup, "get_result"):
        return ""
    token = _extract_bearer_token(req)
    if not token:
        return ""
    try:
        info = _account_lookup.get_result("wrks", token)
    except Exception:
        return ""
    raw = getattr(info, "display_name", "") if info else ""
    if not isinstance(raw, str):
        return ""
    return _wrks_person_name(raw)


def _log_my_info(req: Dict[str, Any], obj: Dict[str, Any]) -> None:
    data = obj.get("data")
    if not isinstance(data, dict):
        data = obj if isinstance(obj.get("info"), dict) else {}
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        info = obj.get("user") if isinstance(obj.get("user"), dict) else None
    if not isinstance(info, dict):
        return
    user_id = str(info.get("id") or info.get("userId") or info.get("user_id") or "").strip()
    email = str(info.get("email") or info.get("mail") or "").strip()
    name = _wrks_person_name(info.get("name"))
    token = _extract_bearer_token(req)
    if name:
        req["_wrks_session_name"] = name
    logging.info(
        "[Account Info] wrks: my/info user_id=%s email=%s name=%s token=%s",
        user_id or "-",
        email or "-",
        name or "-",
        token[:12] + "..." if len(token) > 12 else token or "-",
    )
    if token:
        _register_wrks_account(token, user_id, email, name)


def _log_auth_session(req: Dict[str, Any], obj: Dict[str, Any]) -> None:
    user = obj.get("user")
    if not isinstance(user, dict):
        # NextAuth / alternate envelopes
        data = obj.get("data")
        if isinstance(data, dict) and isinstance(data.get("user"), dict):
            user = data["user"]
        elif isinstance(obj.get("session"), dict) and isinstance(obj["session"].get("user"), dict):
            user = obj["session"]["user"]
        else:
            return
    user_id = str(user.get("id") or user.get("userId") or user.get("sub") or "").strip()
    email = str(user.get("email") or user.get("mail") or "").strip()
    name = _wrks_person_name(user.get("name"))
    access_token = str(
        user.get("access_token")
        or user.get("accessToken")
        or obj.get("access_token")
        or obj.get("accessToken")
        or ""
    ).strip()
    req["_wrks_session_user_id"] = user_id
    req["_wrks_session_email"] = email
    if name:
        req["_wrks_session_name"] = name
    logging.info(
        "[Account Info] wrks: auth/session user_id=%s email=%s name=%s",
        user_id or "-",
        email or "-",
        name or "-",
    )
    if access_token:
        _register_wrks_account(access_token, user_id, email, name)
    bearer = _extract_bearer_token(req)
    if bearer and (user_id or email):
        _register_wrks_account(bearer, user_id, email, name)


def _lookup_wrks_account(req: Dict[str, Any]) -> Tuple[str, str]:
    token = _extract_bearer_token(req)
    if token and _account_lookup:
        info = _account_lookup.get("wrks", token)
        if info:
            return info[0], info[1]
    if token:
        user_id, email = _extract_identity_from_jwt(token)
        if user_id or email:
            _register_wrks_account(token, user_id, email)
            return user_id, email
    user_id = str(req.get("_wrks_session_user_id") or "").strip()
    email = str(req.get("_wrks_session_email") or "").strip()
    return user_id, email


def _parse_sse_payload(text: str) -> Tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""
    if raw == "[DONE]":
        return "", "[DONE]"
    if raw.startswith("{"):
        return "", raw

    event_name = ""
    data_lines: List[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
            continue
        if line.startswith("id:"):
            continue
    if data_lines:
        return event_name, "\n".join(data_lines)
    return event_name, raw


def _parse_sse_data_json(raw: str) -> Optional[Dict[str, Any]]:
    _, data_text = _parse_sse_payload(raw)
    if not data_text or data_text == "[DONE]":
        return None
    try:
        obj = json.loads(data_text)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _extract_event_name(obj: Dict[str, Any]) -> str:
    event = obj.get("event")
    if isinstance(event, str) and event:
        return event
    return ""


def _extract_text_from_content_blocks(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "") != "text":
            continue
        text_obj = block.get("text")
        if isinstance(text_obj, dict):
            value = text_obj.get("value")
            if isinstance(value, str) and value:
                chunks.append(value)
        elif isinstance(text_obj, str) and text_obj:
            chunks.append(text_obj)
    return "".join(chunks)


def _extract_delta_text(obj: Dict[str, Any]) -> str:
    event = _extract_event_name(obj)
    if event == "thread.message.delta":
        data = obj.get("data")
        if not isinstance(data, dict):
            return ""
        delta = data.get("delta")
        if not isinstance(delta, dict):
            return ""
        return _extract_text_from_content_blocks(delta.get("content"))

    # AI-SDK / Agent API v2 stream parts
    part_type = str(obj.get("type") or "").strip().lower()
    if part_type in ("text-delta", "text_delta", "response.output_text.delta"):
        for key in ("delta", "text", "content"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _extract_completed_text(obj: Dict[str, Any]) -> str:
    event = _extract_event_name(obj)
    if event == "thread.message.completed":
        data = obj.get("data")
        if not isinstance(data, dict):
            return ""
        return _extract_text_from_content_blocks(data.get("content"))

    part_type = str(obj.get("type") or "").strip().lower()
    if part_type in ("text", "message", "response.output_text.done"):
        for key in ("text", "content", "delta", "message"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _extract_message_id(obj: Dict[str, Any]) -> str:
    data = obj.get("data")
    if isinstance(data, dict):
        value = data.get("id")
        if isinstance(value, str) and value:
            return value
    for key in ("messageId", "message_id", "id"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip() and str(obj.get("type") or "") != "text-delta":
            # Avoid treating text-delta part ids as the chat message id when empty.
            if key == "id" and str(obj.get("type") or "").endswith("delta"):
                continue
            return value.strip()
    return ""


def _is_stream_finish_part(obj: Dict[str, Any]) -> bool:
    part_type = str(obj.get("type") or "").strip().lower()
    if part_type in ("finish", "done", "response.completed", "message_stop"):
        return True
    event = _extract_event_name(obj)
    return event in ("thread.run.completed", "done")


def _resolve_stream_key(req: Dict[str, Any], obj: Optional[Dict[str, Any]] = None) -> str:
    conversation_id = _extract_conversation_id(req)
    if conversation_id:
        return conversation_id
    if isinstance(obj, dict):
        message_id = _extract_message_id(obj)
        if message_id:
            return message_id
    req_key = str(req.get("_wrks_stream_key") or "")
    if req_key:
        return req_key
    if len(_WRKS_RESPONSE_BUFFER) == 1:
        return next(iter(_WRKS_RESPONSE_BUFFER))
    if len(_WRKS_RESPONSE_MESSAGE_ID) == 1:
        return next(iter(_WRKS_RESPONSE_MESSAGE_ID))
    return _WRKS_FALLBACK_KEY


def _set_output_ctx(req: Dict[str, Any], key: str) -> None:
    session_id = key if key != _WRKS_FALLBACK_KEY else ""
    req["_wrks_output_session_id"] = session_id
    req["_wrks_output_message_id"] = str(_WRKS_RESPONSE_MESSAGE_ID.get(key, "") or "")


def _set_extract_final(req: Dict[str, Any], is_final: bool) -> None:
    req["_extract_final"] = bool(is_final)


def extract(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    # WRKS_SCHEMA_DUMP — temporary capture for new /ai/* schema (remove after migration)
    try:
        _meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        _path = str(_meta.get("path") or "")
        _direction = str(req.get("direction") or "")
        if any(
            x in _path
            for x in (
                "/ai/chat/",
                "/ai/file/",
                "/my/info",
                "/api/auth/session",
                "/agent/chat/file",
            )
        ):
            import os
            import time as _time

            _dump_dir = "/tmp/wrks_dump"
            os.makedirs(_dump_dir, exist_ok=True)
            _ts = _time.strftime("%H%M%S")
            _safe = _path.replace("/", "_").replace("?", "_")[:100]
            _fn = f"{_dump_dir}/{_ts}_{_direction}_{_safe}.bin"
            open(_fn, "wb").write(payload or b"")
            try:
                _obj = json.loads(_safe_utf8(payload or b""))
                if isinstance(_obj, dict):
                    open(_fn + ".keys.txt", "w", encoding="utf-8").write(
                        "keys="
                        + ",".join(sorted(map(str, _obj.keys())))
                        + "\n"
                        + json.dumps(_obj, ensure_ascii=False)[:4000]
                    )
                else:
                    open(_fn + ".keys.txt", "w", encoding="utf-8").write(
                        f"type={type(_obj).__name__}\n"
                    )
            except Exception as _e:
                _head = (payload or b"")[:200]
                open(_fn + ".keys.txt", "w", encoding="utf-8").write(
                    f"nonjson err={_e} head={_head!r}\n"
                )
            logger.info("WRKS_SCHEMA_DUMP wrote %s bytes=%s", _fn, len(payload or b""))
    except Exception:
        logger.exception("WRKS_SCHEMA_DUMP failed")

    if is_wrks_file_upload_request(req) or is_wrks_file_upload_response(req):
        req["_extract_final"] = False
        return ""

    if _is_my_info_response(req):
        try:
            obj = json.loads(_safe_utf8(payload))
            if isinstance(obj, dict):
                _log_my_info(req, obj)
        except Exception:
            pass
        req["_extract_final"] = False
        return ""

    if _is_models_response(req):
        _wrks_note_models_response(req, payload)
        req["_extract_final"] = False
        return ""

    if _is_auth_session_response(req):
        try:
            obj = json.loads(_safe_utf8(payload))
            if isinstance(obj, dict):
                _log_auth_session(req, obj)
        except Exception:
            pass
        req["_extract_final"] = False
        return ""

    if _is_sse_response(req):
        raw = _safe_utf8(payload)
        _, data_text = _parse_sse_payload(raw)
        if not data_text:
            _set_extract_final(req, False)
            return ""

        if data_text == "[DONE]":
            key = _resolve_stream_key(req)
            merged = _WRKS_RESPONSE_BUFFER.pop(key, "")
            _set_output_ctx(req, key)
            _set_extract_final(req, bool(merged))
            return merged

        try:
            obj = json.loads(data_text)
        except Exception:
            _set_extract_final(req, False)
            return ""
        if not isinstance(obj, dict):
            _set_extract_final(req, False)
            return ""

        # Initial user-info envelope (no event field) — skip for prompt/output extraction.
        if not _extract_event_name(obj) and isinstance(obj.get("data"), dict):
            info = obj["data"].get("info")
            if isinstance(info, dict):
                _set_extract_final(req, False)
                return ""

        key = _resolve_stream_key(req, obj)
        req["_wrks_stream_key"] = key
        message_id = _extract_message_id(obj)
        if message_id:
            _WRKS_RESPONSE_MESSAGE_ID[key] = message_id

        completed = _extract_completed_text(obj)
        if completed:
            _WRKS_RESPONSE_BUFFER[key] = completed
            merged = _WRKS_RESPONSE_BUFFER.pop(key, "")
            _set_output_ctx(req, key)
            _set_extract_final(req, bool(merged))
            return merged

        delta = _extract_delta_text(obj)
        if delta:
            _WRKS_RESPONSE_BUFFER[key] = _WRKS_RESPONSE_BUFFER.get(key, "") + delta

        if _is_stream_finish_part(obj):
            merged = _WRKS_RESPONSE_BUFFER.pop(key, "")
            _set_output_ctx(req, key)
            _set_extract_final(req, bool(merged))
            return merged

        _set_extract_final(req, False)
        return ""

    if not _is_message_stream_request(req):
        req["_extract_final"] = False
        return ""

    obj = _parse_request_json(req, payload)
    if obj is None:
        return None
    _wrks_bind_model(req, obj)
    prompt = _extract_prompt(obj)
    if prompt:
        return _sanitize_prompt_for_policy(prompt)
    file_ids = obj.get("gptFileIds")
    if isinstance(file_ids, list) and file_ids:
        return "사용자가 파일을 업로드하였습니다."
    for key in ("fileIds", "imageFileIds", "fileId", "imageFileId"):
        value = obj.get(key)
        if isinstance(value, list) and value:
            return "사용자가 파일을 업로드하였습니다."
        if isinstance(value, (str, int)) and str(value).strip():
            return "사용자가 파일을 업로드하였습니다."
    return ""


def build_policy_api_payload(
    req: Dict[str, Any],
    payload: bytes,
    extracted_text: str,
) -> Optional[Dict[str, Any]]:
    direction = (req.get("direction") or "").lower()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}

    if direction == "response":
        session_id = str(req.get("_wrks_output_session_id") or _extract_conversation_id(req) or "")
        message_id = str(req.get("_wrks_output_message_id") or "")
        if not message_id:
            obj = _parse_sse_data_json(_safe_utf8(payload))
            if isinstance(obj, dict):
                message_id = _extract_message_id(obj)
        user_id, email = _lookup_wrks_account(req)
        if not (user_id or email) and _account_lookup:
            token = _extract_bearer_token(req)
            keys = [token] if token else []
            info = _account_lookup.wait_for(
                "wrks",
                keys,
                env_name="AIVAX_WRKS_ACCOUNT_WAIT_MS",
            )
            if info:
                user_id, email = info[0], info[1]
        response_name = _wrks_cached_name(req)
        response_payload = {
            "llm_output": extracted_text,
            "user_id": user_id,
            "email": email,
            "ai_service": AI_SERVICE_WRKS,
            "client_host": str(meta.get("host") or ""),
            "session_id": session_id,
            "message_id": message_id,
        }
        if response_name:
            response_payload["user_name"] = response_name
        return response_payload

    obj = _parse_request_json(req, payload)
    if obj is None:
        return None

    prompt = _sanitize_prompt_for_policy(_extract_prompt(obj) or extracted_text)
    user_id, email = _lookup_wrks_account(req)
    user_name = _wrks_cached_name(req)

    request_payload = {
        "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
        "prompt": prompt,
        "encoding": False,
        "user_id": user_id,
        "email": email,
        "ai_service": AI_SERVICE_WRKS,
        "client_host": str(meta.get("host") or ""),
        "session_id": _extract_conversation_id(req),
        "attachments": build_policy_attachments(req, obj, payload),
        "message_id": "",
    }
    if user_name:
        request_payload["user_name"] = user_name
    return request_payload


_BLOCK_SYSINST_VERTICAL_GAP = "\n" * 1


def _apply_wrks_prompt_rewrite(obj: Dict[str, Any], replacement_text: str) -> int:
    changed = 0
    if isinstance(obj.get("message"), str):
        if obj["message"] != replacement_text:
            obj["message"] = replacement_text
            changed += 1
    else:
        for key in ("prompt", "input", "query", "text"):
            if isinstance(obj.get(key), str):
                if obj[key] != replacement_text:
                    obj[key] = replacement_text
                    changed += 1
                break
        else:
            if replacement_text:
                obj["message"] = replacement_text
                changed += 1
    return changed


def _sanitize_message_after_strip(obj: Dict[str, Any]) -> int:
    """Remove [analyzeImageUrl] tails left after file id strip."""
    if not isinstance(obj.get("message"), str):
        return 0
    cleaned = _sanitize_prompt_for_policy(obj["message"])
    if cleaned != obj["message"]:
        obj["message"] = cleaned
        return 1
    return 0


def _rewrite_wrks_payload(
    obj: Dict[str, Any],
    replacement_text: str,
) -> Optional[Tuple[bytes, int]]:
    had_attachments = bool(_collect_attachment_refs(obj))
    if isinstance(obj.get("message"), str) and _ANALYZE_IMAGE_URL_IN_PROMPT_RE.search(obj["message"]):
        had_attachments = True

    changed = _apply_wrks_prompt_rewrite(obj, replacement_text)
    strip_cnt = strip_chat_attachments(obj)
    changed += _sanitize_message_after_strip(obj)

    if changed <= 0 and strip_cnt <= 0 and not had_attachments:
        return None
    if changed <= 0 and (strip_cnt > 0 or had_attachments):
        changed = 1

    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, changed


_WRKS_LIST_FILE_KEYS = (
    "gptFileIds",
    "visionImageGptFileIds",
    "fileIds",
    "imageFileIds",
    "visionImageFileIds",
)
_WRKS_SCALAR_FILE_KEYS = ("visionImageGptFileId", "fileId", "imageFileId")


def _consent_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _is_wrks_file_part(part: Any) -> bool:
    return isinstance(part, dict) and str(part.get("type") or "").lower() == "file"


def _wrks_message_parts(obj: Dict[str, Any]) -> Optional[List[Any]]:
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    parts = message.get("parts")
    return parts if isinstance(parts, list) else None


def consent_attachment_state(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    """File refs of the blocked turn. The upload itself already reached wrks."""
    if (req.get("direction") or "").lower() != "request":
        return None
    obj = _parse_request_json(req, payload)
    if not isinstance(obj, dict):
        return None
    lists = {
        key: _consent_copy(obj[key])
        for key in _WRKS_LIST_FILE_KEYS
        if isinstance(obj.get(key), list) and obj[key]
    }
    scalars = {
        key: _consent_copy(obj[key])
        for key in _WRKS_SCALAR_FILE_KEYS
        if isinstance(obj.get(key), (str, int)) and str(obj[key]).strip()
    }
    parts = [_consent_copy(p) for p in (_wrks_message_parts(obj) or []) if _is_wrks_file_part(p)]
    message = obj.get("message")
    tokens = (
        _ANALYZE_IMAGE_URL_IN_PROMPT_RE.findall(message) if isinstance(message, str) else []
    )
    if not lists and not scalars and not parts and not tokens:
        return None
    return {
        "lists": lists,
        "scalars": scalars,
        "parts": parts,
        "message_tokens": tokens,
    }


def restore_consent_attachments(
    req: Dict[str, Any],
    payload: bytes,
    state: Any,
    prompt: str = "",
) -> Optional[bytes]:
    """Re-add the blocked turn's file refs that the rewrite stripped."""
    del prompt
    if not isinstance(state, dict):
        return None
    obj = _parse_request_json(req, payload)
    if not isinstance(obj, dict):
        return None
    restored = 0
    for key, value in (state.get("lists") or {}).items():
        obj[key] = _consent_copy(value)
        restored += 1
    for key, value in (state.get("scalars") or {}).items():
        obj[key] = _consent_copy(value)
        restored += 1
    parts = state.get("parts") or []
    if parts:
        existing = _wrks_message_parts(obj)
        if existing is not None:
            kept = [p for p in existing if not _is_wrks_file_part(p)]
            obj["message"]["parts"] = [_consent_copy(p) for p in parts] + kept
            restored += 1
    tokens = state.get("message_tokens") or []
    if tokens and isinstance(obj.get("message"), str):
        text = obj["message"]
        for token in tokens:
            if str(token).strip() and str(token).strip() not in text:
                text = f"{text}{token}"
                restored += 1
        obj["message"] = text
    if restored <= 0:
        return None
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def rewrite_prompt_for_block(
    req: Dict[str, Any],
    payload: bytes,
    user_prompt: str,
    system_instruction: str,
) -> Optional[Tuple[bytes, str]]:
    obj = _parse_request_json(req, payload)
    if obj is None:
        return None

    sys_t = (system_instruction or "").strip()
    user_t = (user_prompt or "").strip()
    if sys_t and user_t:
        combined = f"{user_t}\n{_BLOCK_SYSINST_VERTICAL_GAP}{sys_t}"
    else:
        combined = user_t or sys_t
    if not combined:
        return None

    out = _rewrite_wrks_payload(obj, combined)
    if out is None:
        return None
    encoded, _ = out
    return encoded, "adapter:wrks_policy_block_sysinstruct"


def rewrite_prompt_masking(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    """MASKING 전용: message 치환 + gptFileIds strip."""
    del source_prompt
    obj = _parse_request_json(req, payload)
    if obj is None:
        return None
    if not replacement_text:
        return None
    out = _rewrite_wrks_payload(obj, replacement_text)
    if out is None:
        return None
    encoded, _ = out
    return encoded, "adapter:wrks_policy_masking"


def rewrite_prompt(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    policy_code: str,
) -> Optional[Tuple[bytes, str]]:
    obj = _parse_request_json(req, payload)
    if obj is None:
        return None

    if not replacement_text:
        return None

    out = _rewrite_wrks_payload(obj, replacement_text)
    if out is None:
        return None
    encoded, _ = out
    code = (policy_code or "masking").strip().lower()
    return encoded, f"adapter:wrks_policy_{code}"


def extract_full_body(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    """Deep INLINE / Suricata full-body: SSE assistant text, or account extract.

    Deep INLINE sets `_eve_full_body=true`, so account responses must be handled
    here (not only in extract()). Uses no process-global SSE buffers.
    """
    direction = (req.get("direction") or "").lower()
    if direction == "response" and not _is_message_stream_request(req):
        if _is_models_response(req):
            _wrks_note_models_response(req, payload)
            req["_extract_final"] = False
            return ""
        # Account /my/info and /api/auth/session — reuse extract() side effects.
        if _is_my_info_response(req) or _is_auth_session_response(req):
            extract(req, payload)
            req["_extract_final"] = False
            return ""
        # Upload responses are handled by normalize_upload, not prompt extract.
        if is_wrks_file_upload_response(req):
            req["_extract_final"] = False
            return ""

    text = _safe_utf8(payload)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    chunks: List[str] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        _, data_text = _parse_sse_payload(block)
        if not data_text or data_text == "[DONE]":
            continue
        try:
            obj = json.loads(data_text)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        completed = _extract_completed_text(obj)
        if completed:
            chunks = [completed]
            message_id = _extract_message_id(obj)
            if message_id:
                req["_wrks_output_message_id"] = message_id
            session_id = _extract_conversation_id(req)
            if session_id:
                req["_wrks_output_session_id"] = session_id
            continue
        delta = _extract_delta_text(obj)
        if delta:
            chunks.append(delta)

    if chunks:
        req["_extract_final"] = True
        return "".join(chunks) if len(chunks) > 1 else chunks[0]
    return None


def extract_session_id(req: Dict[str, Any], payload: bytes) -> str:
    conversation_id = _extract_conversation_id(req)
    if conversation_id:
        return conversation_id

    stream_key = str(req.get("_wrks_stream_key") or "")
    if stream_key and stream_key != _WRKS_FALLBACK_KEY:
        return stream_key

    if _is_sse_response(req):
        obj = _parse_sse_data_json(_safe_utf8(payload))
        if isinstance(obj, dict):
            message_id = _extract_message_id(obj)
            if message_id:
                return message_id
        if len(_WRKS_RESPONSE_BUFFER) == 1:
            return next(iter(_WRKS_RESPONSE_BUFFER))
        if len(_WRKS_RESPONSE_MESSAGE_ID) == 1:
            return next(iter(_WRKS_RESPONSE_MESSAGE_ID))

    return ""


def response_stream_done(req: Dict[str, Any], payload: bytes) -> bool:
    if not _is_sse_response(req):
        return False
    _, data_text = _parse_sse_payload(_safe_utf8(payload))
    if data_text == "[DONE]":
        return True
    obj = _parse_sse_data_json(_safe_utf8(payload))
    if not isinstance(obj, dict):
        return False
    return _extract_event_name(obj) == "thread.message.completed"


def _rewrite_content_blocks(content: Any, replacement_text: str, emitted: bool) -> Tuple[Any, int, bool]:
    if not isinstance(content, list):
        return content, 0, emitted
    changed = 0
    new_content: List[Any] = []
    for block in content:
        if not isinstance(block, dict):
            new_content.append(block)
            continue
        if str(block.get("type") or "") != "text":
            new_content.append(block)
            continue
        new_block = dict(block)
        text_obj = new_block.get("text")
        target = replacement_text if not emitted else ""
        if isinstance(text_obj, dict):
            current = str(text_obj.get("value") or "")
            if current != target:
                text_obj = dict(text_obj)
                text_obj["value"] = target
                new_block["text"] = text_obj
                changed += 1
            emitted = True
        elif isinstance(text_obj, str):
            if text_obj != target:
                new_block["text"] = target
                changed += 1
            emitted = True
        new_content.append(new_block)
    return new_content, changed, emitted


def _rewrite_sse_obj(obj: Dict[str, Any], replacement_text: str, state_key: str) -> int:
    with _WRKS_EMITTED_LOCK:
        emitted = bool(_WRKS_RESPONSE_OVERRIDE_EMITTED.get(state_key, False))
        changed = 0
        event = _extract_event_name(obj)
        data = obj.get("data")
        if not isinstance(data, dict):
            return 0

        if event == "thread.message.delta":
            delta = data.get("delta")
            if isinstance(delta, dict):
                new_content, c, emitted = _rewrite_content_blocks(
                    delta.get("content"), replacement_text, emitted
                )
                if c > 0:
                    delta["content"] = new_content
                    changed += c
        elif event == "thread.message.completed":
            # The final event carries the whole message, so it must hold the block
            # text rather than the blanks used for trailing deltas.
            new_content, c, _ = _rewrite_content_blocks(
                data.get("content"), replacement_text, False
            )
            if c > 0:
                data["content"] = new_content
                changed += c

        _WRKS_RESPONSE_OVERRIDE_EMITTED[state_key] = emitted
        if event == "thread.message.completed" and state_key:
            _WRKS_RESPONSE_OVERRIDE_EMITTED.pop(state_key, None)
        return changed


def _rewrite_sse_block(
    block: str,
    replacement_text: str,
    state_key: str,
) -> Tuple[str, int]:
    """Rewrite one SSE event; returns the (possibly untouched) block and change count."""
    _, data_text = _parse_sse_payload(block)
    if not data_text:
        return block, 0
    if data_text == "[DONE]":
        with _WRKS_EMITTED_LOCK:
            _WRKS_RESPONSE_OVERRIDE_EMITTED.pop(state_key, None)
        return block, 0

    try:
        obj = json.loads(data_text)
    except Exception:
        return block, 0
    if not isinstance(obj, dict):
        return block, 0

    changed = _rewrite_sse_obj(obj, replacement_text, state_key)
    if changed <= 0:
        return block, 0

    encoded_json = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if block.lstrip().startswith("data:"):
        return f"data: {encoded_json}", changed
    return encoded_json, changed


def rewrite_response(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
) -> Optional[Tuple[bytes, str]]:
    if not replacement_text:
        return None
    if not _is_sse_response(req):
        return None

    raw = _safe_utf8(payload).replace("\r\n", "\n").replace("\r", "\n")
    state_key = _resolve_stream_key(req)

    # Inline tees the whole response body, so a payload may hold many events.
    blocks = raw.split("\n\n")
    out_blocks: List[str] = []
    changed_total = 0
    for block in blocks:
        if not block.strip():
            out_blocks.append(block)
            continue
        rewritten, changed = _rewrite_sse_block(block, replacement_text, state_key)
        out_blocks.append(rewritten)
        changed_total += changed

    if changed_total <= 0:
        return None
    return "\n\n".join(out_blocks).encode("utf-8"), "adapter:wrks_policy_block_message"


def clear_response_override_state(session_id: str) -> None:
    key = (session_id or "").strip()
    if not key:
        return
    with _WRKS_EMITTED_LOCK:
        _WRKS_RESPONSE_OVERRIDE_EMITTED.pop(key, None)
    _WRKS_RESPONSE_BUFFER.pop(key, None)
    _WRKS_RESPONSE_MESSAGE_ID.pop(key, None)
