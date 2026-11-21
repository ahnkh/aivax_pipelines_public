"""
Genspark AI Chat adapter (www.genspark.ai).

Chat turn:
  POST /api/agent/ask_proxy
  body.type ai_chat, super_agent, or a slides agent (including Ultra),
  messages[-1] user content, user_s_input fallback, project_id session,
  models[0] or ai_chat_model.

Answer:
  SSE message_field_delta where field_name is content. Tool, thinking, and
  empty deltas are not the answer. message_done commits the buffer.

Account:
  GET /api/user JSON, including a nested cogen/user object.
  Docs, sheets, and other non-chat agents stay out.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

try:
    import account_lookup as _account_lookup
except ImportError:
    _account_lookup = None

from ai_service_codes import AI_SERVICE_GENSPARK
from adapters.genspark_upload import (
    POLICY_ATTACHMENTS_FINAL,
    build_policy_attachments,
    normalize_upload,
)

logger = logging.getLogger(__name__)

_CHAT_PATH = "/api/agent/ask_proxy"
_ACCOUNT_PATH = "/api/user"
_CHAT_TYPES = frozenset({"", "ai_chat", "super_agent"})
_USER_ROLES = frozenset({"user", "human"})
_TEXT_PART_TYPES = frozenset({"", "text", "input_text"})
_CONTENT_FIELDS = frozenset({"content", "text"})
_DONE_TYPES = frozenset(
    {
        "message_done",
        "message_result",
        "message_stop",
        "done",
        "end",
        "complete",
        "completed",
        "final",
    }
)
_SESSION_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "next-auth.session-token",
    "session_token",
    "genspark_session",
    "sid",
)
_ID_KEYS = ("id", "user_id", "userId", "cogen_id", "cogenId", "uuid")
_EMAIL_KEYS = ("email", "user_email", "userEmail")
_NAME_KEYS = (
    "name",
    "display_name",
    "displayName",
    "nickname",
    "username",
    "full_name",
    "fullName",
)

_TURNS: Dict[str, Dict[str, str]] = {}
_SSE_BUFFERS: Dict[str, str] = {}


def _safe_utf8(data: Any) -> str:
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace")
    return str(data or "")


def _meta(req: Dict[str, Any]) -> Dict[str, Any]:
    meta = req.get("meta")
    return meta if isinstance(meta, dict) else {}


def _host(req: Dict[str, Any]) -> str:
    return str(_meta(req).get("host") or "").split(":", 1)[0].lower().rstrip(".")


def _path_base(req: Dict[str, Any]) -> str:
    path = str(_meta(req).get("path") or "").split("?", 1)[0].rstrip("/")
    return path or "/"


def is_genspark_host(host: str) -> bool:
    raw = (host or "").split(":", 1)[0].lower().strip().rstrip(".")
    return raw == "genspark.ai" or raw.endswith(".genspark.ai")


def is_genspark_chat_path(path: str) -> bool:
    base = (path or "").split("?", 1)[0].rstrip("/").lower()
    return base == _CHAT_PATH


def is_genspark_account_path(path: str) -> bool:
    base = (path or "").split("?", 1)[0].rstrip("/").lower()
    return base == _ACCOUNT_PATH


def _is_genspark(req: Dict[str, Any]) -> bool:
    return is_genspark_host(_host(req))


def _parse_json_obj(payload: bytes) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _text_piece(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: List[str] = []
        for item in value[:32]:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
                continue
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").strip().lower()
            if kind not in _TEXT_PART_TYPES:
                continue
            for key in ("text", "content"):
                text = item.get(key)
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
                    break
        return "".join(parts).strip()
    if isinstance(value, dict):
        kind = str(value.get("type") or "").strip().lower()
        if kind and kind not in _TEXT_PART_TYPES:
            return ""
        for key in ("text", "content"):
            found = _text_piece(value.get(key))
            if found:
                return found
    return ""


def _last_user_message(obj: Dict[str, Any]) -> Tuple[str, str]:
    messages = obj.get("messages")
    if not isinstance(messages, list):
        return "", ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role not in _USER_ROLES:
            continue
        return _text_piece(message.get("content")), str(message.get("id") or "").strip()
    return "", ""


def _chat_type(obj: Dict[str, Any]) -> str:
    return str(obj.get("type") or "").strip().lower()


def _is_slides_agent(obj: Dict[str, Any]) -> bool:
    return "slide" in _chat_type(obj)


def _is_ai_chat(obj: Dict[str, Any]) -> bool:
    return _chat_type(obj) in _CHAT_TYPES or _is_slides_agent(obj)


def _prompt_from_obj(obj: Dict[str, Any]) -> str:
    if not _is_ai_chat(obj):
        return ""
    text, _ = _last_user_message(obj)
    if text:
        return text
    return _text_piece(obj.get("user_s_input"))


def _model_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or len(name) > 80:
        return ""
    if name.lower() in _USER_ROLES or name.lower() in {"assistant", "system", "tool"}:
        return ""
    if any(ord(ch) < 32 for ch in name):
        return ""
    return name


def _model_from_obj(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    named = _model_text(obj.get("ai_chat_model") or obj.get("model") or obj.get("_llm_model"))
    if named:
        return named
    session = obj.get("session_state")
    if isinstance(session, dict):
        named = _model_text(session.get("_llm_model") or session.get("model"))
        if named:
            return named
    models = obj.get("models")
    if isinstance(models, list):
        for item in models:
            named = _model_text(item)
            if named:
                return named
    return ""


def _note_model(req: Dict[str, Any], obj: Any) -> str:
    name = _model_from_obj(obj)
    if not name:
        return ""
    req["_genspark_model"] = name
    req["_llm_model"] = name
    return name


def _project_id(obj: Dict[str, Any]) -> str:
    for key in ("project_id", "projectId", "session_id", "sessionId", "conversation_id"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _request_id(req: Dict[str, Any]) -> str:
    return str(req.get("request_id") or "").strip()


def _remember_turn(req: Dict[str, Any], project_id: str, message_id: str) -> None:
    rid = _request_id(req)
    if not rid:
        return
    row = _TURNS.get(rid) or {}
    if project_id:
        row["project_id"] = project_id
    if message_id:
        row["message_id"] = message_id
    model = str(req.get("_genspark_model") or "").strip()
    if model:
        row["model"] = model
    if not row:
        return
    _TURNS[rid] = row
    while len(_TURNS) > 256:
        _TURNS.pop(next(iter(_TURNS)))


def _recall_turn(req: Dict[str, Any]) -> Dict[str, str]:
    return dict(_TURNS.get(_request_id(req)) or {})


def _stamp_ids(req: Dict[str, Any], project_id: str, message_id: str) -> None:
    if project_id:
        req["_genspark_session_id"] = project_id
    if message_id:
        req["_genspark_message_id"] = message_id
        req["_genspark_ui_message_id"] = message_id


def _named_cookie(cookie_str: str, name: str) -> str:
    if not cookie_str or not name:
        return ""
    match = re.search(
        r"(?:^|;\s*)" + re.escape(name) + r"=([^;\s]+)",
        cookie_str,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return unquote(match.group(1).strip())


def _session_keys(req: Dict[str, Any]) -> List[str]:
    cookie = str(_meta(req).get("cookie") or "")
    keys: List[str] = []
    seen: set[str] = set()
    for name in _SESSION_COOKIE_NAMES:
        token = _named_cookie(cookie, name)
        if token and token not in seen:
            seen.add(token)
            keys.append(token)
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        lowered = name.strip().lower()
        if not any(token in lowered for token in ("session", "token", "auth", "sid", "user", "cogen")):
            continue
        token = unquote(value.strip())
        if len(token) < 8 or token in seen:
            continue
        seen.add(token)
        keys.append(token)
    return keys


def _person_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or len(text) > 80 or "@" in text:
        return ""
    lowered = text.lower()
    if "/" in text or lowered.startswith("http"):
        return ""
    return text


def _first_text(obj: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _id_text(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > 128 or "@" in text or any(ord(ch) < 32 for ch in text):
        return ""
    return text


def _email_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if "@" not in text or len(text) > 200 or any(ord(ch) < 32 for ch in text):
        return ""
    return text


def _record_score(found: Dict[str, str]) -> int:
    return (10 if found.get("email") else 0) + (5 if found.get("user_id") else 0) + (1 if found.get("name") else 0)


def _record_from_node(node: Dict[str, Any]) -> Dict[str, str]:
    user_id = ""
    email = ""
    for key, value in node.items():
        lowered = str(key).lower()
        if not user_id and lowered in {item.lower() for item in _ID_KEYS}:
            user_id = _id_text(value)
        if not email and (lowered in {item.lower() for item in _EMAIL_KEYS} or lowered.endswith("email")):
            email = _email_text(value)
    return {
        "user_id": user_id,
        "email": email,
        "name": _person_name(_first_text(node, _NAME_KEYS)),
    }


def _user_record(obj: Dict[str, Any]) -> Dict[str, str]:
    best = {"user_id": "", "email": "", "name": ""}

    def consider(node: Any) -> None:
        nonlocal best
        if isinstance(node, dict):
            found = _record_from_node(node)
            if _record_score(found) > _record_score(best):
                best = found
            for value in node.values():
                consider(value)
        elif isinstance(node, list):
            for item in node[:40]:
                consider(item)

    consider(obj)
    return best


def _register_account(req: Dict[str, Any], record: Dict[str, str]) -> None:
    user_id = record.get("user_id") or ""
    email = record.get("email") or ""
    name = record.get("name") or ""
    if not _account_lookup or not (user_id or email):
        return
    keys = _session_keys(req)
    if user_id and user_id not in keys:
        keys.append(user_id)
    for key in keys:
        _account_lookup.set_account(
            "genspark",
            key,
            user_id,
            email,
            display_name=name,
        )


def _lookup_account(req: Dict[str, Any]) -> Tuple[str, str, str]:
    if not _account_lookup:
        return "", "", ""
    for key in _session_keys(req):
        try:
            info = _account_lookup.get_result("genspark", key)
        except Exception:
            info = _account_lookup.get("genspark", key)
        if not info:
            continue
        if isinstance(info, tuple):
            user_id = str(info[0] or "")
            email = str(info[1] or "") if len(info) > 1 else ""
            name = ""
        else:
            user_id = str(getattr(info, "user_id", "") or "")
            email = str(getattr(info, "email", "") or "")
            name = _person_name(getattr(info, "display_name", ""))
        if user_id or email or name:
            return user_id, email, name
    return "", "", ""


def _json_shape(value: Any, depth: int = 0) -> str:
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        parts = [f"{key}:{_json_shape(item, depth + 1)}" for key, item in list(value.items())[:12]]
        extra = "" if len(value) <= 12 else f",+{len(value) - 12}"
        return "{" + ",".join(parts) + extra + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return f"[{len(value)}]{_json_shape(value[0], depth + 1)}"
    if isinstance(value, str):
        return f"str:{len(value)}"
    if value is None:
        return "null"
    return type(value).__name__


def _dump_unparsed(req: Dict[str, Any], payload: bytes, kind: str) -> None:
    try:
        dump_dir = "/home1/aivax/ai_engine/var/failed_extract"
        os.makedirs(dump_dir, exist_ok=True)
        rid = _request_id(req)
        if rid.startswith("req-"):
            return
        name = f"genspark_{kind}_{rid[:36] or 'unknown'}_{len(payload)}.bin"
        path = os.path.join(dump_dir, name)
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(payload[: 1024 * 1024])
        logger.info("genspark unparsed %s bytes=%d path=%s", kind, len(payload), path)
    except Exception:
        logger.debug("genspark unparsed dump failed", exc_info=True)


_CAPTURE_SECRET_KEYS = frozenset(
    {
        "g_recaptcha_token",
        "recaptcha_token",
        "captcha_token",
        "authorization",
        "access_token",
        "refresh_token",
        "session_token",
    }
)


def _redact_capture_secrets(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_capture_secrets(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        str(key): (
            "" if str(key).lower() in _CAPTURE_SECRET_KEYS else _redact_capture_secrets(item)
        )
        for key, item in value.items()
    }


def _redact_inline_payloads(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_inline_payloads(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_inline_payloads(item) for key, item in value.items()}
    if isinstance(value, str) and value.lower().startswith("data:") and "," in value:
        header, _, _body = value.partition(",")
        return f"{header},<{len(value)} chars>"
    return value


def _capture_slides_request(req: Dict[str, Any], obj: Dict[str, Any]) -> None:
    """Keep the slides request schema, with secrets and inline bytes removed."""
    try:
        capture_dir = "/home1/aivax/ai_engine/var/genspark_capture"
        os.makedirs(capture_dir, exist_ok=True)
        request_id = _request_id(req)[:36] or "unknown"
        path = os.path.join(capture_dir, f"ask_slides_{request_id}.json")
        if os.path.exists(path):
            return
        sanitized = _redact_inline_payloads(_redact_capture_secrets(obj))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sanitized, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        logger.info(
            "genspark slides schema captured type=%s path=%s request_id=%s",
            _chat_type(obj) or "-",
            path,
            request_id,
        )
    except Exception:
        logger.exception("genspark slides schema capture failed")


def _capture_attachment_request(req: Dict[str, Any], obj: Dict[str, Any]) -> None:
    """Save the exact attachment schema once, excluding bearer/captcha secrets.

    The capture is deliberately structural: inline attachment bytes and their
    containing fields are retained so a newly observed Genspark protocol can be
    reproduced in a fixture. It is not a generic request archive.
    """
    serialized = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if "data:" not in serialized and "/api/files/" not in serialized:
        return
    try:
        capture_dir = "/home1/aivax/ai_engine/var/genspark_capture"
        os.makedirs(capture_dir, exist_ok=True)
        request_id = _request_id(req)[:36] or "unknown"
        path = os.path.join(capture_dir, f"ask_attachment_{request_id}.json")
        if os.path.exists(path):
            return
        sanitized = _redact_capture_secrets(obj)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sanitized, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        logger.info(
            "genspark attachment schema captured path=%s request_id=%s",
            path,
            request_id,
        )
    except Exception:
        logger.exception("genspark attachment schema capture failed")


def _log_account(req: Dict[str, Any], payload: bytes) -> None:
    obj = _parse_json_obj(payload)
    if obj is None:
        logger.info(
            "[Account Info] genspark: unparsed bytes=%d path=%s",
            len(payload),
            _path_base(req),
        )
        _dump_unparsed(req, payload, "account")
        return
    record = _user_record(obj)
    req["_genspark_user_id"] = record["user_id"]
    req["_genspark_email"] = record["email"]
    req["_genspark_name"] = record["name"]
    cookie = str(_meta(req).get("cookie") or "")
    cookie_names = [
        part.split("=", 1)[0].strip()
        for part in cookie.split(";")
        if "=" in part and part.split("=", 1)[0].strip()
    ]
    logger.info(
        "[Account Info] genspark: user_id=%s email=%s name=%s path=%s shape=%s cookies=%s",
        record["user_id"] or "-",
        record["email"] or "-",
        record["name"] or "-",
        _path_base(req),
        _json_shape(obj) if not (record["user_id"] or record["email"] or record["name"]) else "-",
        ",".join(cookie_names) or "-",
    )
    if not (record["user_id"] or record["email"] or record["name"]):
        _dump_unparsed(req, payload, "account")
    _register_account(req, record)


def _iter_sse_objects(payload: bytes) -> List[Dict[str, Any]]:
    text = _safe_utf8(payload).replace("\r\n", "\n").replace("\r", "\n")
    objs: List[Dict[str, Any]] = []
    seen: set[int] = set()

    def push(raw: str) -> None:
        body = (raw or "").strip()
        if not body or body == "[DONE]":
            return
        try:
            obj = json.loads(body)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        key = hash(body)
        if key in seen:
            return
        seen.add(key)
        objs.append(obj)

    for block in text.split("\n\n"):
        data_lines = []
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            push("\n".join(data_lines))
    if not objs:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                push(line[5:].strip())
            elif line.startswith("{"):
                push(line)
    return objs


def _event_type(obj: Dict[str, Any]) -> str:
    return str(obj.get("type") or obj.get("event") or "").strip().lower()


def _content_delta(obj: Dict[str, Any]) -> str:
    if _event_type(obj) not in ("", "message_field_delta", "delta"):
        return ""
    field = str(obj.get("field_name") or obj.get("field") or "content").strip().lower()
    if field not in _CONTENT_FIELDS:
        return ""
    delta = obj.get("delta")
    if not isinstance(delta, str) or not delta:
        return ""
    return delta


def _final_content(obj: Dict[str, Any]) -> str:
    if _event_type(obj) not in _DONE_TYPES:
        return ""
    for key in ("content", "text", "message", "answer"):
        text = _text_piece(obj.get(key))
        if text:
            return text
    return ""


def _is_done_event(obj: Dict[str, Any]) -> bool:
    return _event_type(obj) in _DONE_TYPES


def _stream_key(req: Dict[str, Any]) -> str:
    rid = _request_id(req)
    return rid or str(req.get("_genspark_session_id") or "") or "__fallback__"


def _consume_sse(req: Dict[str, Any], payload: bytes, *, final_buffer: bool) -> str:
    key = _stream_key(req)
    accumulated = "" if final_buffer else _SSE_BUFFERS.get(key, "")
    completed = ""
    done = False
    for obj in _iter_sse_objects(payload):
        _note_model(req, obj)
        project_id = _project_id(obj)
        if project_id:
            _stamp_ids(req, project_id, "")
        delta = _content_delta(obj)
        if delta:
            accumulated += delta
        final_text = _final_content(obj)
        if final_text:
            completed = final_text
        if _is_done_event(obj):
            done = True
    answer = completed if completed else accumulated
    recalled = _recall_turn(req)
    if not str(req.get("_genspark_model") or "").strip() and recalled.get("model"):
        req["_genspark_model"] = recalled["model"]
        req["_llm_model"] = recalled["model"]
    # The SSE message id is the assistant row. Prompt and answer merge on the
    # user message id captured with the request.
    _stamp_ids(
        req,
        str(req.get("_genspark_session_id") or recalled.get("project_id") or ""),
        str(recalled.get("message_id") or req.get("_genspark_message_id") or ""),
    )
    if final_buffer or done:
        _SSE_BUFFERS.pop(key, None)
        req["_extract_final"] = bool(answer.strip())
        return answer.strip()
    if accumulated:
        _SSE_BUFFERS[key] = accumulated
    else:
        _SSE_BUFFERS.pop(key, None)
    req["_extract_final"] = False
    return ""


def genspark_should_skip_policy(req: Dict[str, Any], payload: bytes = b"") -> bool:
    if str(req.get("direction") or "").lower() != "request":
        return False
    path = _path_base(req)
    if is_genspark_account_path(path) or not is_genspark_chat_path(path):
        return True
    obj = _parse_json_obj(payload)
    if obj is None:
        return False
    return not _is_ai_chat(obj)


def extract_model(req: Dict[str, Any], payload: bytes) -> str:
    current = str(req.get("_genspark_model") or req.get("_llm_model") or "").strip()
    if current:
        return current
    obj = _parse_json_obj(payload)
    if obj is None:
        for event in _iter_sse_objects(payload):
            name = _note_model(req, event)
            if name:
                return name
        return ""
    return _note_model(req, obj)


def extract(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    direction = str(req.get("direction") or "").lower()
    path = _path_base(req)
    if direction == "response" and is_genspark_account_path(path):
        _log_account(req, payload)
        req["_extract_final"] = False
        return ""
    if direction == "response" and is_genspark_chat_path(path):
        return _consume_sse(req, payload, final_buffer=False)
    if direction != "request" or not is_genspark_chat_path(path):
        req["_extract_final"] = False
        return ""
    obj = _parse_json_obj(payload)
    if obj is None or not _is_ai_chat(obj):
        logger.info(
            "genspark ask_proxy empty shape=%s bytes=%d",
            _json_shape(obj) if obj is not None else "not_json",
            len(payload),
        )
        _dump_unparsed(req, payload, "ask")
        req["_extract_final"] = False
        return ""
    _capture_attachment_request(req, obj)
    if _is_slides_agent(obj):
        _capture_slides_request(req, obj)
    _note_model(req, obj)
    prompt = _prompt_from_obj(obj)
    _, message_id = _last_user_message(obj)
    project_id = _project_id(obj)
    _stamp_ids(req, project_id, message_id)
    _remember_turn(req, project_id, message_id)
    req["_extract_final"] = bool(prompt)
    return prompt


def extract_full_body(req: Dict[str, Any], payload: bytes) -> str:
    direction = str(req.get("direction") or "").lower()
    path = _path_base(req)
    if direction == "response" and is_genspark_account_path(path):
        _log_account(req, payload)
        req["_extract_final"] = False
        return ""
    if direction != "response" or not is_genspark_chat_path(path):
        text = extract(req, payload)
        return text or ""
    return _consume_sse(req, payload, final_buffer=True)


def extract_session_id(req: Dict[str, Any], payload: bytes) -> str:
    current = str(req.get("_genspark_session_id") or "").strip()
    if current:
        return current
    recalled = _recall_turn(req).get("project_id") or ""
    if recalled:
        return recalled
    obj = _parse_json_obj(payload)
    if isinstance(obj, dict):
        return _project_id(obj)
    for event in _iter_sse_objects(payload):
        project_id = _project_id(event)
        if project_id:
            return project_id
    return ""


def response_stream_done(req: Dict[str, Any], payload: bytes) -> bool:
    raw = _safe_utf8(payload).strip()
    if raw == "[DONE]":
        return True
    return any(_is_done_event(obj) for obj in _iter_sse_objects(payload))


def build_policy_api_payload(
    req: Dict[str, Any],
    payload: bytes,
    extracted_text: str,
) -> Optional[Dict[str, Any]]:
    direction = str(req.get("direction") or "").lower()
    path = _path_base(req)
    meta = _meta(req)
    if is_genspark_account_path(path):
        return None
    if not is_genspark_chat_path(path):
        return None

    user_id, email, name = _lookup_account(req)
    session_id = extract_session_id(req, payload)
    message_id = str(req.get("_genspark_message_id") or _recall_turn(req).get("message_id") or "")
    model = extract_model(req, payload)
    if direction == "response":
        if not str(extracted_text or "").strip():
            return None
        body: Dict[str, Any] = {
            "llm_output": extracted_text,
            "user_id": user_id,
            "email": email,
            "ai_service": AI_SERVICE_GENSPARK,
            "client_host": str(meta.get("host") or ""),
            "session_id": session_id,
            "message_id": message_id,
        }
        if name:
            body["user_name"] = name
        if model:
            body["model"] = model
        return body

    obj = _parse_json_obj(payload)
    if obj is None or not _is_ai_chat(obj):
        return None
    prompt = _prompt_from_obj(obj) or extracted_text
    if not str(prompt or "").strip():
        return None
    body = {
        "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
        "prompt": prompt,
        "encoding": False,
        "user_id": user_id,
        "email": email,
        "ai_service": AI_SERVICE_GENSPARK,
        "client_host": str(meta.get("host") or ""),
        "session_id": session_id,
        "attachments": build_policy_attachments(obj, req),
        "message_id": message_id,
    }
    if name:
        body["user_name"] = name
    if model:
        body["model"] = model
    return body
