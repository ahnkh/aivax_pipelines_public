"""
Timely AI adapter (timelyai.io).

Account:
  GET /proxy/api/backend/v2/space/{space_id}/member/{member_id}

Chat turn:
  POST /timely/api/stream  (multipart form, or the same fields as JSON)
    url  /api-ai/v3/llm-completion/stream/start
    body {"messages":[{"role":"user","content":"..."}], "model":"solar-mini4", ...}
  POST /timely/api/stream
    url  /api-ai/v3/llm-completion/stream/subscribe/{id}
    body {"lastEventId":"0"}

The start body is the prompt and the model name. The subscribe call must not
open another message id. edit_chat_title is only a fallback when the start
body had no user text (a file-only send). Assistant text arrives as
token.content pieces and is committed by final_response.message.
Stream end is {"type":"end"}.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import account_lookup as _account_lookup
except ImportError:
    _account_lookup = None

from ai_service_codes import AI_SERVICE_TIMELY

try:
    from adapters.timely_upload import (
        POLICY_ATTACHMENTS_FINAL,
        apply_turn_request,
        claim_policy_attachments,
        has_pending_files,
        mark_turn_prompt_logged,
        normalize_upload,
        note_sse_prompt,
        pending_turn_prompt,
        remember_turn_attachments,
        remember_turn_model,
        remember_turn_request,
    )
except ImportError:
    from timely_upload import (  # type: ignore
        POLICY_ATTACHMENTS_FINAL,
        apply_turn_request,
        claim_policy_attachments,
        has_pending_files,
        mark_turn_prompt_logged,
        normalize_upload,
        note_sse_prompt,
        pending_turn_prompt,
        remember_turn_attachments,
        remember_turn_model,
        remember_turn_request,
    )

logger = logging.getLogger(__name__)

_MEMBER_PATH_MARK = "/proxy/api/backend/v2/space/"
_STREAM_PATH_MARK = "/timely/api/stream"
_COMPLETION_PATH_MARK = "/api-ai/v3/llm-completion"
_SUBSCRIBE_MARK = "/api-ai/v3/llm-completion/stream/subscribe/"

_PROMPT_KEYS = (
    "prompt",
    "query",
    "question",
    "input",
    "user_input",
    "userInput",
    "inputText",
    "input_text",
    "user_message",
    "userMessage",
    "message",
    "text",
    "content",
)
_MESSAGE_LIST_KEYS = ("messages", "history", "chat_history", "chatHistory", "conversation", "contents")
_NESTED_KEYS = ("data", "payload", "request", "params", "inputs", "chat", "input_data")
_SKIP_DEEP_KEYS = frozenset(
    {
        "system",
        "system_prompt",
        "systemprompt",
        "instructions",
        "files",
        "attachments",
        "metadata",
        "tools",
        "headers",
    }
)
_NON_USER_ROLES = frozenset({"assistant", "system", "ai", "bot", "model", "tool"})
_TEXT_PART_TYPES = frozenset({"", "text", "input_text"})
_SUBSCRIBE_ONLY_KEYS = frozenset({"lasteventid", "last_event_id"})
_SEND_DUMP_LIMIT = 50
_REQUEST_PROMPT_TTL_SEC = 120.0
# File references are separate from the user text in Timely start requests.
# A consent replay is a new start request, so it does not naturally carry them.
_CONSENT_ATTACHMENT_KEYS = frozenset(
    {
        "attachment",
        "attachments",
        "asset",
        "assets",
        "document",
        "documents",
        "file",
        "fileid",
        "fileids",
        "file_id",
        "file_ids",
        "files",
        "image",
        "images",
        "image_url",
        "image_urls",
    }
)

_TURNS: Dict[str, Dict[str, Any]] = {}
_EMITTED: Dict[str, bool] = {}
_REQUEST_PROMPTS: Dict[str, float] = {}


def _safe_utf8(data: Union[bytes, bytearray, str]) -> str:
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _meta(req: Dict[str, Any]) -> Dict[str, Any]:
    meta = req.get("meta")
    return meta if isinstance(meta, dict) else {}


def _path_base(req: Dict[str, Any]) -> str:
    return str(_meta(req).get("path") or "").split("?", 1)[0]


def _host(req: Dict[str, Any]) -> str:
    return str(_meta(req).get("host") or "").split(":", 1)[0].lower()


def _is_timely_host(req: Dict[str, Any]) -> bool:
    return "timelyai.io" in _host(req)


def _client_ip(req: Dict[str, Any]) -> str:
    meta = _meta(req)
    return str(meta.get("client_ip") or meta.get("clientIp") or "").strip()


def _bearer(req: Dict[str, Any]) -> str:
    meta = _meta(req)
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


def _member_ids(path: str) -> Tuple[str, str]:
    """Return (space_id, member_id) from the account URL, or empty strings."""
    base = (path or "").split("?", 1)[0]
    marker = _MEMBER_PATH_MARK
    idx = base.find(marker)
    if idx < 0:
        return "", ""
    rest = base[idx + len(marker) :].strip("/")
    parts = [part for part in rest.split("/") if part]
    if len(parts) < 3 or parts[1] != "member":
        return "", ""
    return parts[0], parts[2]


def _is_member_path(path: str) -> bool:
    space_id, member_id = _member_ids(path)
    return bool(space_id and member_id)


def _is_stream_path(path: str) -> bool:
    base = (path or "").split("?", 1)[0].lower()
    return _STREAM_PATH_MARK in base or _COMPLETION_PATH_MARK in base


def _parse_json_obj(payload: bytes) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _as_dict(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            obj = json.loads(value)
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None
    return None


def _subscribe_id_from_text(text: str) -> str:
    raw = text or ""
    idx = raw.find(_SUBSCRIBE_MARK)
    if idx < 0:
        return ""
    rest = raw[idx + len(_SUBSCRIBE_MARK) :]
    sid = []
    for ch in rest:
        if ch.isalnum() or ch in ("_", "-"):
            sid.append(ch)
            continue
        break
    return "".join(sid)


_Step = Tuple[str, Any]
_Located = Tuple[List[List[_Step]], str]


def _role(obj: Dict[str, Any]) -> str:
    for key in ("role", "sender", "author", "type"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            role = value.strip().lower()
            if key != "type" or role in _NON_USER_ROLES or role in ("user", "human"):
                return role
    return ""


def _text_parts(value: List[Any]) -> Tuple[List[List[_Step]], List[str]]:
    paths: List[List[_Step]] = []
    texts: List[str] = []
    for idx, part in enumerate(value[:64]):
        if isinstance(part, str) and part.strip():
            paths.append([("i", idx)])
            texts.append(part.strip())
            continue
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "").strip().lower()
        if kind not in _TEXT_PART_TYPES:
            continue
        for key in ("text", "content", "value"):
            text = part.get(key)
            if isinstance(text, str) and text.strip():
                paths.append([("i", idx), ("k", key)])
                texts.append(text.strip())
                break
    return paths, texts


def _prefixed(prefix: List[_Step], found: Optional[_Located]) -> Optional[_Located]:
    if not found:
        return None
    paths, text = found
    return [prefix + path for path in paths], text


_ATTACHMENT_TOKEN = re.compile(r"\[attachment:[^\]]*\]\([^)]*\)", re.IGNORECASE)
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,79}$")


def _visible_user_text(key: str, text: str) -> str:
    """Drop the attachment markdown Timely prepends to the typed message."""
    if key != "content":
        return text
    return _ATTACHMENT_TOKEN.sub("", text).strip()


def _model_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in _NON_USER_ROLES or text.lower() in {"user", "human", "none", "null"}:
        return ""
    if not _MODEL_NAME.fullmatch(text):
        return ""
    return text


def _model_from_obj(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    found = _model_name(obj.get("model"))
    if found:
        return found
    body = _as_dict(obj.get("body"))
    if isinstance(body, dict):
        return _model_name(body.get("model"))
    return ""


def _form_fields(req: Dict[str, Any], payload: bytes) -> Dict[str, str]:
    """Read url/body fields from a Timely multipart stream post."""
    raw = payload or b""
    ctype = str(req.get("content_type") or "")
    if "multipart/form-data" not in ctype.lower() and not raw.startswith(b"------"):
        return {}
    try:
        from file_upload_manager import parse_multipart
    except ImportError:
        from core.file_upload_manager import parse_multipart  # type: ignore
    fields: Dict[str, str] = {}
    for part in parse_multipart(raw, ctype):
        name = str(part.get("name") or "")
        if not name or part.get("filename"):
            continue
        body = part.get("body") or b""
        if isinstance(body, bytes):
            fields[name] = body.decode("utf-8", "replace")
    return fields


def _stream_envelope(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    """JSON body, or multipart url + body, as one object the prompt walker understands."""
    obj = _parse_json_obj(payload)
    if isinstance(obj, dict):
        return obj
    fields = _form_fields(req, payload)
    if not fields:
        return None
    envelope: Dict[str, Any] = {}
    if fields.get("url"):
        envelope["url"] = fields["url"].strip()
    body_text = fields.get("body") or ""
    if body_text:
        try:
            parsed = json.loads(body_text)
        except Exception:
            parsed = None
        envelope["body"] = parsed if isinstance(parsed, dict) else body_text
    return envelope or None


def _locate_value(key: str, value: Any, depth: int) -> Optional[_Located]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if key == "content" and text.startswith("/"):
            return None
        text = _visible_user_text(key, text)
        if not text:
            return None
        return [[("k", key)]], text
    if isinstance(value, list):
        paths, texts = _text_parts(value)
        if texts:
            return [[("k", key)] + path for path in paths], "\n".join(texts)
        return None
    if isinstance(value, dict):
        return _prefixed([("k", key)], _locate_prompt(value, depth + 1))
    return None


def _locate_prompt(obj: Any, depth: int = 0) -> Optional[_Located]:
    """Find the user text in a Timely send body and the JSON paths holding it."""
    if not isinstance(obj, dict) or depth > 8:
        return None
    if _role(obj) in _NON_USER_ROLES:
        return None
    raw_body = obj.get("body")
    body = _as_dict(raw_body)
    if isinstance(body, dict) and body is not obj:
        keys = {str(key).lower() for key in body.keys()}
        if not (keys and keys <= _SUBSCRIBE_ONLY_KEYS):
            step = ("k", "body") if isinstance(raw_body, dict) else ("j", "body")
            found = _prefixed([step], _locate_prompt(body, depth + 1))
            if found:
                return found
    for key in _PROMPT_KEYS:
        if key in obj:
            found = _locate_value(key, obj.get(key), depth)
            if found:
                return found
    for key in _MESSAGE_LIST_KEYS:
        items = obj.get(key)
        if not isinstance(items, list):
            continue
        for idx in range(len(items) - 1, -1, -1):
            item = items[idx]
            if not isinstance(item, dict):
                continue
            if _role(item) not in ("", "user", "human"):
                continue
            found = _prefixed([("k", key), ("i", idx)], _locate_prompt(item, depth + 1))
            if found:
                return found
    for key in _NESTED_KEYS:
        value = obj.get(key)
        if isinstance(value, dict):
            found = _prefixed([("k", key)], _locate_prompt(value, depth + 1))
            if found:
                return found
    for key, value in obj.items():
        name = str(key)
        if name.lower() in _SKIP_DEEP_KEYS or name in _PROMPT_KEYS or name in _NESTED_KEYS:
            continue
        if name in _MESSAGE_LIST_KEYS or name == "body":
            continue
        if isinstance(value, dict):
            found = _prefixed([("k", name)], _locate_prompt(value, depth + 1))
            if found:
                return found
    return None


def _prompt_from_obj(obj: Any, depth: int = 0) -> str:
    found = _locate_prompt(obj, depth)
    return found[1] if found else ""


def _set_path(node: Any, steps: List[_Step], value: str) -> bool:
    kind, key = steps[0]
    try:
        if kind == "j":
            inner = json.loads(node[key])
            if len(steps) == 1 or not _set_path(inner, steps[1:], value):
                return False
            node[key] = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
            return True
        if len(steps) == 1:
            node[key] = value
            return True
        return _set_path(node[key], steps[1:], value)
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def _shape(obj: Any, depth: int = 0, prefix: str = "") -> List[str]:
    """Key paths with types and string lengths. No values."""
    if depth > 6:
        return []
    out: List[str] = []
    if isinstance(obj, dict):
        for key, value in list(obj.items())[:40]:
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                out.extend(_shape(value, depth + 1, path))
            elif isinstance(value, str):
                out.append(f"{path}:str{len(value)}")
            else:
                out.append(f"{path}:{type(value).__name__}")
    elif isinstance(obj, list):
        out.append(f"{prefix}[]:len{len(obj)}")
        for idx, value in enumerate(obj[:3]):
            out.extend(_shape(value, depth + 1, f"{prefix}[{idx}]"))
    return out[:80]


def _dump_send_body(req: Dict[str, Any], payload: bytes) -> str:
    dump_dir = os.environ.get("AIVAX_TIMELY_SEND_DUMP_DIR", "/home1/aivax/ai_engine/var/timely_send")
    try:
        os.makedirs(dump_dir, mode=0o700, exist_ok=True)
        if len(os.listdir(dump_dir)) >= _SEND_DUMP_LIMIT:
            return ""
        rid = "".join(c for c in str(req.get("request_id") or "unknown") if c.isalnum() or c == "-")
        path = os.path.join(dump_dir, f"{rid[:36]}_{len(payload)}.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload[: 1024 * 1024])
        return path
    except Exception:
        logger.debug("timely send dump failed", exc_info=True)
        return ""


def _note_unparsed_send(req: Dict[str, Any], obj: Dict[str, Any], payload: bytes) -> None:
    logger.info(
        "timely send no_prompt request_id=%s bytes=%d shape=%s dump=%s",
        req.get("request_id") or "-",
        len(payload),
        _shape(obj),
        _dump_send_body(req, payload) or "-",
    )


def _remember_request_prompt(prompt: str) -> None:
    text = (prompt or "").strip()
    if not text:
        return
    now = time.monotonic()
    _REQUEST_PROMPTS[text] = now
    if len(_REQUEST_PROMPTS) > 256:
        cutoff = now - _REQUEST_PROMPT_TTL_SEC
        stale = [key for key, seen in _REQUEST_PROMPTS.items() if seen < cutoff]
        for key in stale:
            _REQUEST_PROMPTS.pop(key, None)


def _recent_request_prompt(prompt: str) -> bool:
    text = (prompt or "").strip()
    if not text:
        return False
    seen = _REQUEST_PROMPTS.get(text)
    if seen is None:
        return False
    if time.monotonic() - seen > _REQUEST_PROMPT_TTL_SEC:
        _REQUEST_PROMPTS.pop(text, None)
        return False
    return True


_NAME_KEYS = (
    "name",
    "username",
    "user_name",
    "displayname",
    "display_name",
    "fullname",
    "full_name",
    "realname",
    "real_name",
    "nickname",
    "chatnickname",
)
_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{2,5}$")


def _person_name(value: Any, member_id: str = "") -> str:
    text = str(value or "").strip()
    if not text or len(text) > 40:
        return ""
    if any(mark in text for mark in ("@", "/", "\\")) or "http" in text.lower():
        return ""
    if _FILE_EXT_RE.search(text):
        return ""
    if member_id and text == member_id:
        return ""
    lowered = text.lower()
    if lowered in {"user", "admin", "member", "null", "none", "true", "false"}:
        return ""
    if text.startswith(("sm_", "p_", "ROLE_")):
        return ""
    if re.fullmatch(r"[0-9a-fA-F-]{16,}", text):
        return ""
    return text


def _name_in_dict(obj: Dict[str, Any], member_id: str) -> str:
    ranked = {str(key).lower(): value for key, value in obj.items()}
    for key in _NAME_KEYS:
        found = _person_name(ranked.get(key), member_id)
        if found:
            return found
    return ""


def _email_in_dict(obj: Dict[str, Any]) -> str:
    for key, value in obj.items():
        if str(key).lower() in ("email", "mail", "user_email", "useremail") and isinstance(value, str):
            email = value.strip()
            if "@" in email:
                return email
    return ""


def _find_profile(obj: Any, member_id: str = "", depth: int = 0) -> Tuple[str, str]:
    """Email and display name from the member object that holds the address."""
    if depth > 6:
        return "", ""
    if isinstance(obj, dict):
        email = _email_in_dict(obj)
        name = _name_in_dict(obj, member_id)
        for value in list(obj.values())[:40]:
            child_email, child_name = _find_profile(value, member_id, depth + 1)
            email = email or child_email
            name = name or child_name
        return email, name
    if isinstance(obj, list):
        for item in obj[:20]:
            email, name = _find_profile(item, member_id, depth + 1)
            if email or name:
                return email, name
    return "", ""


def _find_email(obj: Any, depth: int = 0) -> str:
    email, _name = _find_profile(obj, "", depth)
    return email


def _register_account(req: Dict[str, Any], user_id: str, email: str, name: str = "") -> None:
    user_id = (user_id or "").strip()
    email = (email or "").strip()
    name = _person_name(name, user_id)
    if user_id:
        req["_timely_user_id"] = user_id
    if email:
        req["_timely_email"] = email
    if name:
        req["_timely_name"] = name
    if not _account_lookup or not (user_id or email or name):
        return
    ip = _client_ip(req)
    key = user_id or email
    _account_lookup.set_account(
        "timely", key, user_id, email, client_ip=ip, display_name=name
    )
    token = _bearer(req)
    if token and token != key:
        _account_lookup.set_account(
            "timely", token, user_id, email, client_ip=ip, display_name=name
        )


def _lookup_account(req: Dict[str, Any]) -> Tuple[str, str, str]:
    user_id = str(req.get("_timely_user_id") or "").strip()
    email = str(req.get("_timely_email") or "").strip()
    name = _person_name(req.get("_timely_name"), user_id)
    if not _account_lookup:
        return user_id, email, name
    keys = []
    token = _bearer(req)
    if token:
        keys.append(token)
    if user_id:
        keys.append(user_id)
    ip = _client_ip(req)
    if ip:
        keys.append(f"ip:{ip}")
    for key in keys:
        info = _account_lookup.get_result("timely", key) if hasattr(_account_lookup, "get_result") else None
        if info is None:
            pair = _account_lookup.get("timely", key)
            if not pair:
                continue
            found_id, found_email = pair[0], pair[1]
            found_name = ""
        else:
            found_id, found_email = info.user_id, info.email
            found_name = _person_name(getattr(info, "display_name", ""), found_id or user_id)
        if found_id or found_email or found_name:
            return found_id or user_id, found_email or email, found_name or name
    return user_id, email, name


def _register_member(req: Dict[str, Any], payload: bytes) -> None:
    space_id, member_id = _member_ids(_path_base(req))
    if not member_id:
        return
    req["_timely_space_id"] = space_id
    req["_timely_member_id"] = member_id
    email = ""
    name = ""
    obj = _parse_json_obj(payload)
    if isinstance(obj, dict):
        email, name = _find_profile(obj, member_id)
    _register_account(req, member_id, email, name)
    logger.info(
        "[Account Info] timely: member user_id=%s email=%s name=%s space=%s client=%s",
        member_id,
        email or "-",
        name or "-",
        space_id or "-",
        _client_ip(req) or "-",
    )


def _new_turn() -> Dict[str, Any]:
    return {
        "user_prompt": "",
        "assistant": "",
        "tokens": "",
        "session_id": "",
        "message_id": "",
        "user_message_id": "",
        "model": "",
        "subscribe_id": "",
        "done": False,
    }


def _apply_event(turn: Dict[str, Any], obj: Dict[str, Any]) -> None:
    kind = str(obj.get("type") or "").strip().lower()
    if kind == "edit_chat_title":
        message = obj.get("message")
        if isinstance(message, str) and message.strip() and not turn["user_prompt"]:
            turn["user_prompt"] = message.strip()
        return
    if kind == "message_created":
        message_id = str(obj.get("message_id") or "").strip()
        user_message_id = str(obj.get("user_message_id") or "").strip()
        if message_id:
            turn["message_id"] = message_id
        if user_message_id:
            turn["user_message_id"] = user_message_id
        return
    if kind == "token":
        content = obj.get("content")
        if isinstance(content, str) and content:
            turn["tokens"] += content
        return
    if kind == "final_response":
        message = obj.get("message")
        if isinstance(message, str):
            turn["assistant"] = message
        session_id = str(obj.get("session_id") or "").strip()
        model = str(obj.get("model") or "").strip()
        if session_id:
            turn["session_id"] = session_id
        if model:
            turn["model"] = model
        turn["done"] = True
        return
    if kind == "end":
        turn["done"] = True
        if not turn["assistant"]:
            turn["assistant"] = turn["tokens"]


def _iter_sse_json(payload: bytes) -> List[Dict[str, Any]]:
    text = _safe_utf8(payload).replace("\r\n", "\n").replace("\r", "\n")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("{") and "data:" not in stripped and "\n{" not in stripped:
        obj = _parse_json_obj(stripped.encode("utf-8"))
        return [obj] if obj else []

    objs: List[Dict[str, Any]] = []
    data_lines: List[str] = []

    def flush() -> None:
        if not data_lines:
            return
        raw = "\n".join(data_lines).strip()
        data_lines.clear()
        if not raw or raw == "[DONE]":
            return
        try:
            obj = json.loads(raw)
        except Exception:
            return
        if isinstance(obj, dict):
            objs.append(obj)

    for line in text.splitlines():
        item = line.strip()
        if not item:
            flush()
            continue
        if item.startswith("id:"):
            if data_lines:
                flush()
            continue
        if item.startswith("event:") or item.startswith(":"):
            continue
        if item.startswith("data:"):
            data_lines.append(item[5:].lstrip())
            continue
        if item.startswith("{"):
            if data_lines:
                flush()
            data_lines.append(item)
    flush()
    return objs


def parse_timely_sse(payload: bytes) -> Dict[str, Any]:
    """Parse one Timely SSE event or a full event-stream body."""
    turn = _new_turn()
    for obj in _iter_sse_json(payload):
        _apply_event(turn, obj)
    if turn["done"] and not turn["assistant"]:
        turn["assistant"] = turn["tokens"]
    return {
        "user_prompt": turn["user_prompt"],
        "assistant": turn["assistant"] if turn["done"] else "",
        "tokens": turn["tokens"],
        "session_id": turn["session_id"],
        "message_id": turn["message_id"],
        "user_message_id": turn["user_message_id"],
        "model": turn["model"],
        "done": bool(turn["done"]),
    }


def _stamp_turn(req: Dict[str, Any], turn: Dict[str, Any]) -> None:
    if turn.get("user_prompt"):
        req["_timely_user_prompt"] = turn["user_prompt"]
    if turn.get("session_id"):
        req["_timely_session_id"] = turn["session_id"]
    if turn.get("subscribe_id"):
        req["_timely_subscribe_id"] = turn["subscribe_id"]
    if turn.get("message_id"):
        req["_timely_message_id"] = turn["message_id"]
    if turn.get("user_message_id"):
        req["_timely_user_message_id"] = turn["user_message_id"]
    if turn.get("model") and not req.get("_llm_model"):
        req["_timely_model"] = turn["model"]
        req["_llm_model"] = turn["model"]


def _note_deferred_prompt(req: Dict[str, Any], prompt: str) -> None:
    text = (prompt or "").strip()
    if not text:
        return
    req["_timely_user_prompt"] = text
    if req.get("_timely_input_already_logged") or _recent_request_prompt(text):
        return
    owner = note_sse_prompt(req, text)
    if not owner:
        return
    if owner != str(req.get("request_id") or ""):
        req["_timely_ui_message_id"] = owner
    req["_timely_deferred_human_prompt"] = text


def _turn_key(req: Dict[str, Any]) -> str:
    rid = str(req.get("request_id") or "").strip()
    if rid:
        return rid
    subscribe_id = str(req.get("_timely_subscribe_id") or "").strip()
    if subscribe_id:
        return subscribe_id
    return "__fallback__"


def _answer_text(turn: Dict[str, Any]) -> str:
    if turn.get("assistant"):
        return str(turn["assistant"])
    if turn.get("done"):
        return str(turn.get("tokens") or "")
    return ""


def _apply_events(turn: Dict[str, Any], events: List[Dict[str, Any]]) -> Dict[str, Any]:
    for obj in events:
        _apply_event(turn, obj)
    if turn["done"] and not turn["assistant"]:
        turn["assistant"] = turn["tokens"]
    return turn


def _extract_response(req: Dict[str, Any], payload: bytes) -> str:
    events = _iter_sse_json(payload)
    if not events:
        req["_extract_final"] = False
        return ""
    subscribe_id = _subscribe_id_from_text(_path_base(req)) or _subscribe_id_from_text(_safe_utf8(payload))
    if len(events) == 1 and not req.get("_eve_full_body"):
        key = _turn_key(req)
        turn = _TURNS.setdefault(key, _new_turn())
        if subscribe_id:
            turn["subscribe_id"] = subscribe_id
        before_prompt = turn["user_prompt"]
        _apply_event(turn, events[0])
        _stamp_turn(req, turn)
        if turn["user_prompt"] and turn["user_prompt"] != before_prompt:
            _note_deferred_prompt(req, turn["user_prompt"])
        if not turn["done"]:
            req["_extract_final"] = False
            return ""
        answer = _answer_text(turn)
        if answer:
            apply_turn_request(req)
        _TURNS.pop(key, None)
        req["_extract_final"] = bool(answer)
        return answer

    turn = _new_turn()
    if subscribe_id:
        turn["subscribe_id"] = subscribe_id
    _apply_events(turn, events)
    if not turn["assistant"] and turn["tokens"]:
        turn["assistant"] = turn["tokens"]
        turn["done"] = True
    _stamp_turn(req, turn)
    answer = _answer_text(turn)
    _note_deferred_prompt(req, turn["user_prompt"])
    if answer:
        apply_turn_request(req)
    req["_extract_final"] = bool(answer)
    return answer


def _extract_request(req: Dict[str, Any], payload: bytes) -> str:
    obj = _stream_envelope(req, payload)
    subscribe_id = _subscribe_id_from_text(_path_base(req))
    if isinstance(obj, dict):
        url = obj.get("url")
        if isinstance(url, str):
            subscribe_id = subscribe_id or _subscribe_id_from_text(url)
        subscribe_id = subscribe_id or _subscribe_id_from_text(_safe_utf8(payload))
    if subscribe_id:
        req["_timely_subscribe_id"] = subscribe_id
        req["_timely_session_id"] = req.get("_timely_session_id") or subscribe_id
    if _is_subscribe_body(req, obj if isinstance(obj, dict) else None):
        req["_extract_final"] = False
        return ""
    if _is_stream_path(_path_base(req)):
        remember_turn_request(req)
    model = _model_from_obj(obj)
    if model:
        remember_turn_model(req, model)
    if not isinstance(obj, dict):
        req["_extract_final"] = False
        prompt = pending_turn_prompt(req)
        if prompt:
            req["_timely_user_prompt"] = prompt
            _remember_request_prompt(prompt)
            return prompt
        if _is_stream_path(_path_base(req)) and len(payload or b"") > 200:
            _note_unparsed_send(req, {"_non_json_bytes": len(payload or b"")}, payload)
        return ""
    prompt = _prompt_from_obj(obj) or pending_turn_prompt(req)
    req["_extract_final"] = False
    if prompt:
        req["_timely_user_prompt"] = prompt
        _remember_request_prompt(prompt)
        return prompt
    if not subscribe_id and _is_stream_path(_path_base(req)):
        _note_unparsed_send(req, obj, payload)
    return ""


def timely_should_skip_policy(req: Dict[str, Any], payload: bytes = b"") -> bool:
    """Skip subscribe/account traffic. Inspect a chat send that has text or a file."""
    if str(req.get("direction") or "").lower() != "request":
        return False
    if not _is_timely_host(req):
        return False
    path = _path_base(req)
    if _is_member_path(path):
        return True
    obj = _stream_envelope(req, payload)
    if isinstance(obj, dict) and _prompt_from_obj(obj):
        return False
    if _is_subscribe_body(req, obj):
        return True
    if _is_stream_path(path) and has_pending_files(req):
        return False
    return True


def _is_subscribe_body(req: Dict[str, Any], obj: Optional[Dict[str, Any]]) -> bool:
    if _subscribe_id_from_text(_path_base(req)):
        return True
    if not isinstance(obj, dict):
        return False
    url = obj.get("url")
    if isinstance(url, str) and _SUBSCRIBE_MARK in url:
        return True
    body = _as_dict(obj.get("body"))
    if isinstance(body, dict):
        keys = {str(key).lower() for key in body.keys()}
        return bool(keys) and keys <= _SUBSCRIBE_ONLY_KEYS
    return False


def extract(req: Dict[str, Any], payload: bytes) -> str:
    req.pop("_timely_deferred_human_prompt", None)
    if not _is_timely_host(req):
        req["_extract_final"] = False
        return ""
    path = _path_base(req)
    direction = str(req.get("direction") or "").lower()
    if _is_member_path(path):
        if direction == "response":
            _register_member(req, payload)
        req["_extract_final"] = False
        return ""
    if direction == "response" and (_is_stream_path(path) or _iter_sse_json(payload)):
        return _extract_response(req, payload)
    if direction == "request":
        return _extract_request(req, payload)
    req["_extract_final"] = False
    return ""


def extract_full_body(req: Dict[str, Any], payload: bytes) -> str:
    """Suricata/Deep full-body path. Account responses stay on extract()."""
    req.pop("_timely_deferred_human_prompt", None)
    direction = str(req.get("direction") or "").lower()
    path = _path_base(req)
    if direction == "response" and _is_member_path(path):
        _register_member(req, payload)
        req["_extract_final"] = False
        return ""
    if direction != "response":
        return extract(req, payload)
    if not _is_timely_host(req):
        req["_extract_final"] = False
        return ""
    req["_eve_full_body"] = True
    return _extract_response(req, payload)


_CHAT_ID_KEYS = ("chat_id", "chatId")


def _chat_id_in(node: Any, depth: int = 0) -> str:
    """chat_id the client sends on every completion, including 동의합니다."""
    if depth > 6:
        return ""
    if isinstance(node, str):
        parsed = _as_dict(node)
        return _chat_id_in(parsed, depth + 1) if parsed else ""
    if isinstance(node, list):
        for item in node[:32]:
            found = _chat_id_in(item, depth + 1)
            if found:
                return found
        return ""
    if not isinstance(node, dict):
        return ""
    for key in _CHAT_ID_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key, value in node.items():
        if str(key) == "url":
            continue
        found = _chat_id_in(value, depth + 1)
        if found:
            return found
    return ""


def collect_consent_session_ids(req: Dict[str, Any], payload: bytes) -> List[str]:
    """Per-chat consent key. Subscribe ids and SSE session_id are not this id."""
    chat_id = _chat_id_in(_stream_envelope(req, payload))
    return [chat_id] if chat_id else []


def extract_session_id(req: Dict[str, Any], payload: bytes) -> str:
    session_id = str(req.get("_timely_session_id") or "").strip()
    if session_id:
        return session_id
    subscribe_id = str(req.get("_timely_subscribe_id") or "").strip()
    if not subscribe_id:
        subscribe_id = _subscribe_id_from_text(_path_base(req))
        if not subscribe_id:
            obj = _parse_json_obj(payload)
            if isinstance(obj, dict) and isinstance(obj.get("url"), str):
                subscribe_id = _subscribe_id_from_text(obj["url"])
            subscribe_id = subscribe_id or _subscribe_id_from_text(_safe_utf8(payload))
    if subscribe_id:
        return subscribe_id
    parsed = parse_timely_sse(payload)
    return str(parsed.get("session_id") or "")


def response_stream_done(req: Dict[str, Any], payload: bytes) -> bool:
    if str(req.get("direction") or "").lower() != "response":
        return False
    for obj in _iter_sse_json(payload):
        kind = str(obj.get("type") or "").strip().lower()
        if kind in ("final_response", "end"):
            return True
    return False


def build_policy_api_payload(
    req: Dict[str, Any],
    payload: bytes,
    extracted_text: str,
) -> Optional[Dict[str, Any]]:
    direction = str(req.get("direction") or "").lower()
    user_id, email, user_name = _lookup_account(req)
    session_id = extract_session_id(req, payload)
    meta = _meta(req)
    client_host = str(meta.get("client_ip") or meta.get("host") or "")
    model = str(req.get("_llm_model") or req.get("_timely_model") or "").strip()
    if direction == "response":
        if not (extracted_text or "").strip():
            return None
        payload_obj = {
            "llm_output": extracted_text,
            "user_id": user_id,
            "email": email,
            "ai_service": AI_SERVICE_TIMELY,
            "client_host": client_host,
            "session_id": session_id,
            "message_id": str(req.get("_timely_message_id") or ""),
        }
        if model:
            payload_obj["model"] = model
        if user_name:
            payload_obj["user_name"] = user_name
        return payload_obj

    prompt = (extracted_text or "").strip()
    if not prompt:
        obj = _stream_envelope(req, payload)
        if isinstance(obj, dict):
            prompt = _prompt_from_obj(obj)
            if not model:
                model = _model_from_obj(obj)
                if model:
                    remember_turn_model(req, model)
    if not prompt:
        prompt = str(req.get("_timely_user_prompt") or "").strip()
    attachments = claim_policy_attachments(req, payload)
    if attachments:
        remember_turn_attachments(req, attachments)
    elif isinstance(req.get("_timely_turn_attachments"), list):
        attachments = [
            dict(row)
            for row in req["_timely_turn_attachments"]
            if isinstance(row, dict) and row.get("id") and row.get("name")
        ]
    if prompt:
        mark_turn_prompt_logged(req, prompt)
    if not prompt and not attachments:
        return None
    if not attachments:
        attachments = [{"id": "", "size": 0, "name": "", "mime_type": ""}]
    built = {
        "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
        "prompt": prompt,
        "encoding": False,
        "user_id": user_id,
        "email": email,
        "ai_service": AI_SERVICE_TIMELY,
        "client_host": client_host,
        "session_id": session_id,
        "attachments": attachments,
        "message_id": str(req.get("_timely_user_message_id") or ""),
    }
    if model:
        built["model"] = model
    if user_name:
        built["user_name"] = user_name
    return built


def _rewrite_prompt_obj(obj: Dict[str, Any], replacement: str) -> int:
    found = _locate_prompt(obj)
    if not found:
        return 0
    paths, _text = found
    changed = 0
    for idx, path in enumerate(paths):
        if _set_path(obj, path, replacement if idx == 0 else ""):
            changed += 1
    return changed


def _rewrite_request_json(payload: bytes, replacement: str) -> Optional[Tuple[bytes, str]]:
    obj = _parse_json_obj(payload)
    if not isinstance(obj, dict) or not replacement:
        return None
    if _rewrite_prompt_obj(obj, replacement) <= 0:
        return None
    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, "adapter:timely_prompt"


def _rewrite_request_payload(
    req: Dict[str, Any],
    payload: bytes,
    replacement: str,
) -> Optional[Tuple[bytes, str]]:
    """Rewrite a JSON send or the JSON body part inside a multipart start."""
    direct = _rewrite_request_json(payload, replacement)
    if direct is not None:
        return direct
    if not replacement:
        return None
    ctype = str(req.get("content_type") or "")
    try:
        from file_upload_manager import parse_multipart
    except ImportError:
        from core.file_upload_manager import parse_multipart  # type: ignore
    parts = parse_multipart(payload or b"", ctype)
    if not parts:
        return None
    boundary = ""
    for seg in ctype.split(";"):
        seg = seg.strip()
        if seg.lower().startswith("boundary="):
            boundary = seg.split("=", 1)[1].strip().strip('"')
            break
    if not boundary and (payload or b"").startswith(b"--"):
        boundary = (payload or b"").split(b"\r\n", 1)[0][2:].decode("utf-8", "replace")
    if not boundary:
        return None
    changed = False
    chunks: List[bytes] = []
    for part in parts:
        body = part.get("body") or b""
        if not isinstance(body, bytes):
            body = str(body).encode("utf-8")
        if str(part.get("name") or "") == "body":
            rewritten = _rewrite_request_json(body, replacement)
            if rewritten is not None:
                body = rewritten[0]
                changed = True
        header = f'Content-Disposition: form-data; name="{part.get("name") or ""}"'
        filename = str(part.get("filename") or "")
        if filename:
            header += f'; filename="{filename}"'
        content_type = str(part.get("content_type") or "")
        if content_type:
            header += f"\r\nContent-Type: {content_type}"
        chunks.append(f"--{boundary}\r\n{header}\r\n\r\n".encode("utf-8") + body)
    if not changed:
        return None
    encoded = b"\r\n".join(chunks) + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return encoded, "adapter:timely_prompt"


def _consent_attachment_fields(
    node: Any, path: Tuple[Union[str, int], ...] = (), depth: int = 0
) -> List[Dict[str, Any]]:
    """Capture Timely file-reference fields without retaining local file bytes."""
    if depth > 8:
        return []
    fields: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key)
            child_path = path + (name,)
            if name.lower() in _CONSENT_ATTACHMENT_KEYS and value not in (None, "", [], {}):
                fields.append({"path": list(child_path), "value": copy.deepcopy(value)})
            if isinstance(value, (dict, list)):
                fields.extend(_consent_attachment_fields(value, child_path, depth + 1))
    elif isinstance(node, list):
        for index, value in enumerate(node[:64]):
            if isinstance(value, (dict, list)):
                fields.extend(_consent_attachment_fields(value, path + (index,), depth + 1))
    return fields


def consent_attachment_state(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    """Save the original Timely turn's file references for a consent replay."""
    envelope = _stream_envelope(req, payload)
    if not isinstance(envelope, dict):
        return None
    fields = _consent_attachment_fields(envelope)
    if not fields:
        return None
    return {"kind": "timely_file_refs_v1", "fields": fields}


def _restore_attachment_fields(
    node: Any,
    fields: Any,
    *,
    strip_prefix: bool = False,
) -> bool:
    if not isinstance(node, dict) or not isinstance(fields, list):
        return False
    changed = False
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("path"), list):
            continue
        path = list(field["path"])
        if strip_prefix:
            if not path or path[0] != "body":
                continue
            path = path[1:]
        if not path:
            continue
        parent: Any = node
        valid = True
        for part in path[:-1]:
            if isinstance(part, int):
                if not isinstance(parent, list) or part < 0 or part >= len(parent):
                    valid = False
                    break
                parent = parent[part]
            elif isinstance(parent, dict) and part in parent:
                parent = parent[part]
            else:
                valid = False
                break
        if not valid or not isinstance(parent, dict) or not isinstance(path[-1], str):
            continue
        value = copy.deepcopy(field.get("value"))
        if parent.get(path[-1]) != value:
            parent[path[-1]] = value
            changed = True
    return changed


def _restore_consent_json(payload: bytes, fields: Any, *, strip_prefix: bool = False) -> Optional[bytes]:
    obj = _parse_json_obj(payload)
    if not isinstance(obj, dict):
        return None
    body_was_json = isinstance(obj.get("body"), str) and isinstance(_as_dict(obj.get("body")), dict)
    if body_was_json:
        obj["body"] = _as_dict(obj["body"])
    if not _restore_attachment_fields(obj, fields, strip_prefix=strip_prefix):
        return None
    if body_was_json:
        obj["body"] = json.dumps(obj["body"], ensure_ascii=False, separators=(",", ":"))
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def restore_consent_attachments(
    req: Dict[str, Any],
    payload: bytes,
    state: Any,
    prompt: str = "",
) -> Optional[bytes]:
    """Reapply the blocked Timely turn's file references to its consent replay."""
    del prompt
    if not isinstance(state, dict) or state.get("kind") != "timely_file_refs_v1":
        return None
    fields = state.get("fields")
    direct = _restore_consent_json(payload, fields)
    if direct is not None:
        return direct

    content_type = str(req.get("content_type") or "")
    try:
        from file_upload_manager import parse_multipart
    except ImportError:
        from core.file_upload_manager import parse_multipart  # type: ignore
    parts = parse_multipart(payload or b"", content_type)
    if not parts:
        return None
    boundary = ""
    for segment in content_type.split(";"):
        segment = segment.strip()
        if segment.lower().startswith("boundary="):
            boundary = segment.split("=", 1)[1].strip().strip('"')
            break
    if not boundary and (payload or b"").startswith(b"--"):
        boundary = (payload or b"").split(b"\r\n", 1)[0][2:].decode("utf-8", "replace")
    if not boundary:
        return None

    changed = False
    chunks: List[bytes] = []
    for part in parts:
        body = part.get("body") or b""
        if not isinstance(body, bytes):
            body = str(body).encode("utf-8")
        if str(part.get("name") or "") == "body":
            restored = _restore_consent_json(body, fields, strip_prefix=True)
            if restored is not None:
                body = restored
                changed = True
        header = f'Content-Disposition: form-data; name="{part.get("name") or ""}"'
        filename = str(part.get("filename") or "")
        if filename:
            header += f'; filename="{filename}"'
        part_content_type = str(part.get("content_type") or "")
        if part_content_type:
            header += f"\r\nContent-Type: {part_content_type}"
        chunks.append(f"--{boundary}\r\n{header}\r\n\r\n".encode("utf-8") + body)
    if not changed:
        return None
    return b"\r\n".join(chunks) + f"\r\n--{boundary}--\r\n".encode("utf-8")


def rewrite_prompt_masking(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    del source_prompt
    out = _rewrite_request_payload(req, payload, replacement_text)
    if out is None:
        return None
    return out[0], "adapter:timely_policy_masking"


def rewrite_prompt_for_block(
    req: Dict[str, Any],
    payload: bytes,
    user_prompt: str,
    system_instruction: str,
) -> Optional[Tuple[bytes, str]]:
    user_t = (user_prompt or "").strip()
    sys_t = (system_instruction or "").strip()
    combined = user_t
    if sys_t:
        combined = f"{user_t}\n{sys_t}" if user_t else sys_t
    out = _rewrite_request_payload(req, payload, combined)
    if out is None:
        return None
    return out[0], "adapter:timely_policy_block_sysinstruct"


def rewrite_prompt(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    policy_code: str,
) -> Optional[Tuple[bytes, str]]:
    out = _rewrite_request_payload(req, payload, replacement_text)
    if out is None:
        return None
    code = (policy_code or "masking").strip().lower()
    return out[0], f"adapter:timely_policy_{code}"


def _state_key(req: Dict[str, Any]) -> str:
    return _turn_key(req)


def _rewrite_event_obj(obj: Dict[str, Any], replacement: str, emitted: bool) -> Tuple[int, bool]:
    kind = str(obj.get("type") or "").strip().lower()
    changed = 0
    if kind == "token":
        target = replacement if not emitted else ""
        if obj.get("content") != target:
            obj["content"] = target
            changed += 1
        return changed, True
    if kind == "final_response":
        if obj.get("message") != replacement:
            obj["message"] = replacement
            changed += 1
        return changed, True
    return 0, emitted


def rewrite_response(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
) -> Optional[Tuple[bytes, str]]:
    replacement = replacement_text or ""
    if not replacement:
        return None
    text = _safe_utf8(payload).replace("\r\n", "\n").replace("\r", "\n")
    stripped = text.strip()
    key = _state_key(req)
    emitted = bool(_EMITTED.get(key, False))
    if stripped.startswith("{") and "data:" not in stripped:
        obj = _parse_json_obj(stripped.encode("utf-8"))
        if not isinstance(obj, dict):
            return None
        changed, emitted = _rewrite_event_obj(obj, replacement, emitted)
        _EMITTED[key] = emitted
        if changed <= 0:
            return None
        encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return encoded, "adapter:timely_response_override"

    lines = text.splitlines()
    changed = 0
    out_lines: List[str] = []
    for line in lines:
        item = line.strip()
        raw_json = ""
        prefix = ""
        if item.startswith("data:"):
            colon = line.find(":")
            prefix = line[: colon + 1]
            raw_json = line[colon + 1 :].strip()
        elif item.startswith("{") and item.endswith("}"):
            raw_json = item
        if raw_json.startswith("{"):
            try:
                obj = json.loads(raw_json)
            except Exception:
                out_lines.append(line)
                continue
            if isinstance(obj, dict):
                delta, emitted = _rewrite_event_obj(obj, replacement, emitted)
                changed += delta
                rendered = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                if prefix:
                    spacer = " " if line[len(prefix) :].startswith(" ") else ""
                    out_lines.append(f"{prefix}{spacer}{rendered}")
                else:
                    out_lines.append(rendered)
                continue
        out_lines.append(line)
    _EMITTED[key] = emitted
    if changed <= 0:
        return None
    encoded = ("\n".join(out_lines) + ("\n" if text.endswith("\n") else "")).encode("utf-8")
    return encoded, "adapter:timely_response_override"


def clear_response_override_state(session_id: str) -> None:
    sid = (session_id or "").strip()
    if not sid:
        return
    _EMITTED.pop(sid, None)
    _TURNS.pop(sid, None)
