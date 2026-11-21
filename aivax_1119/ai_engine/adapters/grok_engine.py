"""
Grok Adapter
Handles Grok JSON request/response payloads.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple, Union

try:
    import account_lookup as _account_lookup
except ImportError:
    _account_lookup = None

import file_upload_manager as _fum
from ai_service_codes import AI_SERVICE_GROK
from adapters.grok_upload import (
    POLICY_ATTACHMENTS_FINAL,
    build_policy_attachments,
    normalize_upload as _grok_upload_normalize,
    parse_direct_upload_message,
    peek_recent_chat_files,
    strip_chat_attachments,
    unwrap_masked_direct_upload_prompt,
    _collect_attachment_refs,
    _is_grok_direct_upload_request,
    _is_grok_gcs_upload,
)

logger = logging.getLogger(__name__)


def _grok_path(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("path") or "")


def _grok_path_base(req: Dict[str, Any]) -> str:
    return _grok_path(req).split("?", 1)[0].rstrip("/") or "/"


# Only these endpoints are real chat turns (input prompt / streamed assistant).
# List/history APIs (conversations?pageSize, share_links, conversations_v2, …)
# embed the same text and must not re-run input/output filters — except
# load-responses, which is the HTTP fallback after WS chat (2026+).
# /ws/mgw is the live chat transport (plugin must deliver WS frames).
_GROK_CHAT_TURN_PATH_RE = re.compile(
    r"^(/rest/app-chat/conversations(/new|/[0-9a-fA-F-]+/(responses|load-responses))$|/ws/mgw/?)$"
)


def _is_grok_chat_turn_path(req: Dict[str, Any]) -> bool:
    return bool(_GROK_CHAT_TURN_PATH_RE.match(_grok_path_base(req)))


def _parse_json_any(payload: bytes) -> Any:
    try:
        return json.loads(_safe_utf8(payload))
    except Exception:
        return None


def _has_auth_keys(value: Any) -> bool:
    if isinstance(value, list):
        return any(_has_auth_keys(item) for item in value)
    if not isinstance(value, dict):
        return False
    keys = set(value.keys())
    auth_keys = {"anonUserId", "anonUserToken", "userPublicKey", "challenge", "signature"}
    return bool(keys & auth_keys)


def _is_grok_auth_payload(payload: bytes) -> bool:
    obj = _parse_json_any(payload)
    if _has_auth_keys(obj):
        return True
    text = _safe_utf8(payload)
    return (
        "anonUserId" in text
        or "anonUserToken" in text
        or "userPublicKey" in text
        or ("challenge" in text and "signature" in text)
    )


_GROK_AUX_REST_PATHS = frozenset(
    {
        "/rest/suggestions/stream",
        "/rest/rate-limits",
        "/rest/modes",
    }
)


def _is_grok_upload_json(obj: Dict[str, Any]) -> bool:
    if _is_grok_direct_upload_request(obj):
        return True
    return bool(str(obj.get("fileName") or "").strip() and obj.get("content"))


def _is_grok_upload_path(req: Dict[str, Any]) -> bool:
    base = _grok_path_base(req)
    return "/upload-file" in base


def _is_grok_auxiliary_request(req: Dict[str, Any], payload: bytes) -> bool:
    direction = str(req.get("direction") or "").lower()
    if direction != "request":
        return False
    base = _grok_path_base(req)
    if base in _GROK_AUX_REST_PATHS:
        return True
    if re.search(r"/rest/app-chat/conversations/[0-9a-fA-F-]+/load-responses$", base):
        return True
    if base == "/" or re.search(r"/c/[0-9a-fA-F-]+$", base):
        return _is_grok_auth_payload(payload)
    return False


def _is_grok_non_prompt_request(req: Dict[str, Any], payload: bytes) -> bool:
    """대화/업로드가 아닌 REST 호출(예: /rest/modes + {}) — 정책 필터에 본문을 넣지 않는다."""
    direction = str(req.get("direction") or "").lower()
    if direction != "request":
        return False
    if _is_grok_auxiliary_request(req, payload):
        return True
    base = _grok_path_base(req)
    if "/rest/app-chat/" in base:
        if _is_grok_upload_path(req):
            return True
        return False
    if not base.startswith("/rest/"):
        return False
    obj = _parse_json_any(payload)
    if isinstance(obj, dict):
        if _is_grok_upload_json(obj) or _extract_prompt(obj):
            return False
    return True


def is_grok_file_upload_request(req: Dict[str, Any], payload: bytes) -> bool:
    """True for directUpload / legacy upload HTTP — must not hit input_filter."""
    if str(req.get("direction") or "").lower() != "request":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    path = str(meta.get("path") or "")
    if _is_grok_gcs_upload(host, path):
        return True
    if _is_grok_upload_path(req):
        return True
    obj = _parse_json_any(payload)
    return isinstance(obj, dict) and _is_grok_upload_json(obj)


def is_grok_conversation_path(path: str) -> bool:
    """True for live chat turns only: /ws/mgw and conversation responses posts."""
    path_lower = (path or "").split("?", 1)[0].rstrip("/") or "/"
    return bool(_GROK_CHAT_TURN_PATH_RE.match(path_lower))


def grok_should_skip_policy(req: Dict[str, Any], payload: bytes) -> bool:
    """Deny-by-default: uploads, WS control, and non-chat REST skip the policy API.

    Sharing/RSC/list endpoints were creating empty Grok dashboard rows after
    Envoy reconnect (session.create + /sharing PASS with prompt="").
    """
    if is_grok_file_upload_request(req, payload):
        return True
    if req.get("_grok_ws_skip"):
        return True
    if str(req.get("direction") or "").lower() != "request":
        return False
    return not _is_grok_chat_turn_path(req)


def normalize_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    if _is_grok_auxiliary_request(req, payload):
        logger.info("grok auxiliary traffic bypass: path=%s", _grok_path_base(req))
        return _fum.build_upload_result(handled=True)
    if is_grok_file_upload_request(req, payload):
        result = _grok_upload_normalize(req, payload)
        if isinstance(result, dict) and result.get("handled"):
            return result
        logger.info(
            "grok file upload passthrough (skip input_filter): path=%s",
            _grok_path_base(req),
        )
        return _fum.build_upload_result(handled=True)
    return _grok_upload_normalize(req, payload)


_PATH_RE = re.compile(
    r"/rest/app-chat/conversations/(?P<conv>[0-9a-fA-F-]+)/(?:responses|load-responses)(?:[/?].*)?$"
)
_CONVERSATION_PAGE_RE = re.compile(r"/c/(?P<conv>[0-9a-fA-F-]+)(?:[/?].*)?$")
_HTML_USER_ID_RE = re.compile(r'"userId"\s*:\s*"([^"]+)"')
_HTML_EMAIL_RE = re.compile(r'"email"\s*:\s*"([^"]+)"')
_HTML_GIVEN_NAME_RE = re.compile(r'"givenName"\s*:\s*"([^"]*)"')
_HTML_FAMILY_NAME_RE = re.compile(r'"familyName"\s*:\s*"([^"]*)"')
_GROK_NAME_WINDOW = 4000


def _extract_x_user_from_cookie(cookie_str: str) -> Optional[str]:
    """Extract x-user or x-userid from Cookie header (request)."""
    if not cookie_str or not isinstance(cookie_str, str):
        return None
    cookie_str = cookie_str.strip()
    for name in ("x-user", "x-userid"):
        m = re.search(r"%s=([^;\s]+)" % re.escape(name), cookie_str, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _grok_session_keys(req: Dict[str, Any], *, include_ip: bool = True) -> List[str]:
    """Keys for account_lookup: x-user cookie, conversation id, client IP.

    Grok HTTP/2 traffic often has no x-user/x-userid cookie, so HTML profile
    extract must also register under conv:/ip: keys that chat requests share.
    """
    keys: List[str] = []
    seen: set[str] = set()

    def _add(key: str) -> None:
        key = (key or "").strip()
        if not key or key in seen:
            return
        seen.add(key)
        keys.append(key)

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    x_userid = str(meta.get("x_userid") or "").strip()
    if not x_userid:
        cookie_str = str(meta.get("cookie") or "").strip()
        x_userid = _extract_x_user_from_cookie(cookie_str) or ""
    if x_userid:
        _add(x_userid)

    conversation_id = _extract_conversation_page_id(req) or _extract_conversation_id(req)
    if conversation_id:
        _add(f"conv:{conversation_id}")

    if include_ip:
        client_ip = str(meta.get("client_ip") or "").strip()
        if client_ip:
            _add(f"ip:{client_ip}")
    return keys


def _grok_person_name(value: Any) -> str:
    """Display name from the signed-in session. Emails, ids, and URLs are not names."""
    if not isinstance(value, str):
        return ""
    text = " ".join(html.unescape(value).split())
    if not text or len(text) > 80 or "@" in text:
        return ""
    lowered = text.lower()
    if "/" in text or "\\" in text or lowered.startswith("http"):
        return ""
    if re.fullmatch(r"[0-9a-fA-F-]{16,}", text):
        return ""
    return text


def _grok_join_name(given: Any, family: Any) -> str:
    parts = [part for part in (_grok_person_name(given), _grok_person_name(family)) if part]
    return _grok_person_name(" ".join(parts))


def _register_grok_account(
    req: Dict[str, Any], user_id: str, email: str, display_name: str = ""
) -> int:
    if not _account_lookup or not (user_id or email):
        return 0
    keys = _grok_session_keys(req)
    if not keys:
        return 0
    uid = user_id or ""
    em = email or ""
    name = _grok_person_name(display_name)
    for key in keys:
        _account_lookup.set_account("grok", key, uid, em, display_name=name)
    return len(keys)


def _grok_cached_name(req: Dict[str, Any]) -> str:
    if not _account_lookup or not hasattr(_account_lookup, "get_result"):
        return ""
    for key in _grok_session_keys(req, include_ip=False):
        try:
            info = _account_lookup.get_result("grok", key)
        except Exception:
            info = None
        raw = getattr(info, "display_name", "") if info else ""
        if not isinstance(raw, str):
            continue
        name = _grok_person_name(raw)
        if name:
            return name
    return ""


def _lookup_grok_account(req: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    if not _account_lookup:
        return None
    # IP is registration-only evidence.  Never resolve a chat identity from a
    # NAT/shared source IP when stronger x-user/conversation keys are absent.
    for key in _grok_session_keys(req, include_ip=False):
        info = _account_lookup.get("grok", key)
        if info:
            return info
    return None


_JSON_DECODER = json.JSONDecoder()


def _safe_utf8(data: Union[bytes, bytearray, str]) -> str:
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _is_json_request(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "request":
        return False
    return unit_type == "json_body" or "application/json" in content_type


def _is_json_response(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "response":
        return False
    if _is_grok_profile_page_response(req):
        return False
    return unit_type == "json_body" or "application/json" in content_type


def _extract_conversation_id(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    m = _PATH_RE.search(path)
    if not m:
        return ""
    return str(m.group("conv") or "")


def _extract_conversation_page_id(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    m = _CONVERSATION_PAGE_RE.search(path)
    if not m:
        return ""
    return str(m.group("conv") or "")


def _is_grok_root_page(req: Dict[str, Any]) -> bool:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    if not path:
        return False
    base = path.split("?", 1)[0]
    return base == "/"


def _is_grok_profile_page_response(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return False

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    method = str(meta.get("method") or "").upper()
    content_type = str(req.get("content_type") or "").lower()
    if "grok.com" not in host:
        return False
    if method and method not in ("GET", "HEAD"):
        return False
    if not (_extract_conversation_page_id(req) or _is_grok_root_page(req)):
        return False
    # Deep INLINE may omit content_type on some tees; path match is enough.
    if content_type and "text/html" not in content_type and "text/plain" not in content_type:
        # Still allow empty / unknown CT for account HTML pages.
        if "json" in content_type or "javascript" in content_type:
            return False
    return True


def _nearest_json_string(text: str, pattern: re.Pattern[str], anchor: int) -> str:
    best = ""
    best_dist = _GROK_NAME_WINDOW + 1
    for match in pattern.finditer(text):
        dist = abs(match.start() - anchor)
        if dist < best_dist:
            best_dist = dist
            best = match.group(1)
    if best_dist > _GROK_NAME_WINDOW:
        return ""
    return best


def _extract_user_profile_from_html(text: str) -> Tuple[str, str, str]:
    normalized = text.replace('\\"', '"')

    user_id = ""
    email = ""
    name = ""

    m_user = _HTML_USER_ID_RE.search(normalized)
    if m_user:
        user_id = m_user.group(1)

    m_email = _HTML_EMAIL_RE.search(normalized)
    if m_email:
        email = m_email.group(1)
    anchor = m_email.start() if m_email else (m_user.start() if m_user else -1)
    if anchor >= 0:
        name = _grok_join_name(
            _nearest_json_string(normalized, _HTML_GIVEN_NAME_RE, anchor),
            _nearest_json_string(normalized, _HTML_FAMILY_NAME_RE, anchor),
        )

    return user_id, email, name


def _is_grok_auth_session_response(req: Dict[str, Any]) -> bool:
    if str(req.get("direction") or "").lower() != "response":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    path = str(meta.get("path") or "").split("?", 1)[0].rstrip("/").lower()
    return "grok.com" in host and path == "/api/auth/session"


def _note_grok_auth_session(req: Dict[str, Any], payload: bytes) -> None:
    try:
        obj = json.loads(_safe_utf8(payload or b""))
    except Exception:
        return
    if not isinstance(obj, dict):
        return
    session = obj.get("session") if isinstance(obj.get("session"), dict) else obj
    if not isinstance(session, dict):
        return
    user_id = str(session.get("userId") or session.get("user_id") or "").strip()
    email = str(session.get("email") or "").strip()
    if "@" not in email:
        email = ""
    name = _grok_join_name(session.get("givenName"), session.get("familyName"))
    if not (user_id or email):
        return
    registered = _register_grok_account(req, user_id, email, name)
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    logger.info(
        "[Account Info] grok: auth/session user_id=%s email=%s name=%s registered=%d path=%s",
        user_id or "-",
        email or "-",
        name or "-",
        registered,
        str(meta.get("path") or ""),
    )


def _log_conversation_page_profile(req: Dict[str, Any], payload: bytes) -> None:
    text = _safe_utf8(payload)
    user_id, email, name = _extract_user_profile_from_html(text)
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    x_userid = str(meta.get("x_userid") or "").strip()
    if not x_userid:
        cookie_str = str(meta.get("cookie") or "").strip()
        x_userid = _extract_x_user_from_cookie(cookie_str) or ""
    path = str(meta.get("path") or "")
    conversation_id = _extract_conversation_page_id(req)
    session_keys = _grok_session_keys(req)
    registered = _register_grok_account(req, user_id, email, name) if (user_id or email) else 0

    logging.info(
        "[Account Info] grok: conversation_profile path=%s conversation_id=%s x_userid=%s user_id=%s email=%s name=%s keys=%s registered=%d",
        path or "-",
        conversation_id or "-",
        x_userid or "-",
        user_id or "-",
        email or "-",
        name or "-",
        ",".join(session_keys) or "-",
        registered,
    )


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


def _extract_prompt_from_input_chunks(chunks: Any) -> str:
    if not isinstance(chunks, list):
        return ""
    parts: List[str] = []
    for ch in chunks:
        if not isinstance(ch, dict):
            continue
        text_obj = ch.get("text")
        if isinstance(text_obj, dict):
            value = text_obj.get("text")
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        elif isinstance(text_obj, str) and text_obj.strip():
            parts.append(text_obj.strip())
    return " ".join(parts).strip()


def _extract_ws_or_item_prompt(obj: Dict[str, Any]) -> str:
    event = obj.get("event") if isinstance(obj.get("event"), dict) else None
    item = None
    if isinstance(event, dict) and isinstance(event.get("item"), dict):
        item = event["item"]
    elif isinstance(obj.get("item"), dict):
        item = obj["item"]
    if not isinstance(item, dict):
        return ""
    if str(item.get("role") or "").lower() not in ("", "user", "human"):
        # assistant items are not prompts
        if str(item.get("role") or "").lower() in ("assistant", "model"):
            return ""
    xg = item.get("x_grok") if isinstance(item.get("x_grok"), dict) else {}
    prompt = _extract_prompt_from_input_chunks(xg.get("input_chunks"))
    if prompt:
        return prompt
    content = item.get("content")
    if isinstance(content, list):
        parts: List[str] = []
        for row in content:
            if not isinstance(row, dict):
                continue
            if str(row.get("type") or "") in ("input_text", "text", "output_text"):
                value = row.get("text")
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
        return " ".join(parts).strip()
    return ""


def _extract_latest_human_from_load_responses(obj: Dict[str, Any]) -> str:
    responses = obj.get("responses")
    if not isinstance(responses, list):
        return ""
    for row in reversed(responses):
        if not isinstance(row, dict):
            continue
        if str(row.get("sender") or "").lower() not in ("human", "user"):
            continue
        message = row.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return ""


def _walk_prompt_candidates(obj: Any, depth: int = 0) -> str:
    """Best-effort deep search for a user prompt string in nested Grok WS JSON."""
    if depth > 6 or obj is None:
        return ""
    if isinstance(obj, dict):
        for key in (
            "message",
            "prompt",
            "input",
            "query",
            "text",
            "userMessage",
            "user_message",
        ):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                low = value.strip().lower()
                if low in ("ping", "pong", "ok", "true", "false"):
                    continue
                user_text, _ = parse_direct_upload_message(value)
                if user_text.strip():
                    return user_text.strip()
        for key in ("request", "payload", "data", "args", "params", "body", "chat"):
            found = _walk_prompt_candidates(obj.get(key), depth + 1)
            if found:
                return found
        for key, child in obj.items():
            if key in (
                "authorization",
                "cookie",
                "token",
                "signature",
                "fileMetadataId",
                "fileUri",
                "imageUrl",
            ):
                continue
            if isinstance(child, (dict, list)):
                found = _walk_prompt_candidates(child, depth + 1)
                if found:
                    return found
    elif isinstance(obj, list):
        for row in obj:
            found = _walk_prompt_candidates(row, depth + 1)
            if found:
                return found
    return ""


def _extract_conversation_id_from_obj(obj: Any, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(obj, list):
        for row in obj:
            found = _extract_conversation_id_from_obj(row, depth + 1)
            if found:
                return found
        return ""
    if not isinstance(obj, dict):
        return ""
    for key in ("conversationId", "conversation_id", "chatId", "chat_id", "session_id"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    conv = obj.get("conversation")
    if isinstance(conv, dict):
        cid = conv.get("conversationId") or conv.get("id")
        if isinstance(cid, str) and cid.strip():
            return cid.strip()
    for key in ("request", "payload", "data", "result", "event", "item"):
        child = obj.get(key)
        if isinstance(child, (dict, list)):
            found = _extract_conversation_id_from_obj(child, depth + 1)
            if found:
                return found
    return ""


def _dump_grok_ws_payload(kind: str, payload: bytes, meta: Dict[str, Any], note: str = "") -> None:
    try:
        dump_dir = os.environ.get("AIVAX_GROK_WS_DUMP_DIR") or os.path.join(
            tempfile.gettempdir(), "aivax_grok_ws_dump"
        )
        os.makedirs(dump_dir, exist_ok=True)
        ts = int(time.time())
        path_hint = str(meta.get("path") or "ws").split("?", 1)[0].replace("/", "_")
        dump_path = os.path.join(dump_dir, f"{ts}_{kind}_{path_hint}_{len(payload or b'')}.bin")
        with open(dump_path, "wb") as fh:
            fh.write(payload or b"")
        logger.info(
            "grok dump_ws kind=%s path=%s bytes=%d note=%s file=%s",
            kind,
            str(meta.get("path") or ""),
            len(payload or b""),
            note or "-",
            dump_path,
        )
    except Exception:
        logger.exception("grok dump_ws failed")


# Client→server user turns on /ws/mgw (2026). Everything else with an event
# envelope is session/lifecycle/streaming chrome — never a prompt source.
_GROK_WS_USER_EVENT_TYPES = frozenset(
    {
        "response.create",
        "conversation.item.create",
    }
)


def _extract_prompt(obj: Dict[str, Any]) -> str:
    """Extract user prompt text from a Grok request body / WS JSON object.

    When an ``event`` envelope is present, only ``response.create`` /
    ``conversation.item.create`` may yield a prompt (from the user ``item``).
    Mirror SHM often mis-tags server ``response.chunk`` frames as request;
    those must not deep-walk into progress chrome as if it were user text.
    """
    event = obj.get("event") if isinstance(obj.get("event"), dict) else None
    if isinstance(event, dict):
        et = str(event.get("type") or "")
        if et not in _GROK_WS_USER_EVENT_TYPES:
            return ""
        ws_prompt = _extract_ws_or_item_prompt(obj)
        if ws_prompt:
            return ws_prompt
        # Rare shapes: prompt fields on the event itself (no item.x_grok).
        for key in ("message", "prompt", "input", "query", "text"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                user_text, _ = parse_direct_upload_message(value)
                if user_text.strip():
                    return user_text.strip()
        return ""

    for key in ("message", "prompt", "input", "query", "text"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            user_text, _ = parse_direct_upload_message(value)
            if user_text.strip():
                return user_text.strip()
    ws_prompt = _extract_ws_or_item_prompt(obj)
    if ws_prompt:
        return ws_prompt
    load_prompt = _extract_latest_human_from_load_responses(obj)
    if load_prompt:
        return load_prompt
    deep = _walk_prompt_candidates(obj)
    if deep:
        return deep
    return ""


def _is_grok_ws_control_event(obj: Dict[str, Any]) -> bool:
    """True for WS frames that must not become chat turns.

    2026 Grok puts the user prompt (and file_mention chunks) on
    ``event.type=response.create``. Skipping that type wholesale dropped both
    prompt extract and pending-file promote, leaving grok-* blobs unbound.

    Only those client user-turn create events (plus ``conversation.item.create``)
    are eligible; streaming/lifecycle frames are always control — including when
    Mirror SHM tags them as direction=request.
    """
    event = obj.get("event") if isinstance(obj.get("event"), dict) else None
    if not isinstance(event, dict):
        return False
    et = str(event.get("type") or "")
    if et in _GROK_WS_USER_EVENT_TYPES:
        if _extract_prompt(obj) or _collect_attachment_refs(obj):
            return False
        item = event.get("item")
        if isinstance(item, dict) and str(item.get("role") or "").lower() in (
            "user",
            "human",
        ):
            return False
        return True
    # ping/pong/session.* / response.chunk / response.created / …
    return True


def _is_regen_with_empty_message(obj: Dict[str, Any]) -> bool:
    """True if this is a regenerate request with no new user text (don't send full JSON to filters)."""
    if not obj.get("isRegenRequest"):
        return False
    return not (_extract_prompt(obj) or "").strip()


def _extract_request_message_id(obj: Dict[str, Any]) -> str:
    for key in ("parentResponseId", "responseId", "messageId", "message_id", "parent_message_id"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _iter_json_documents(text: str) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = _JSON_DECODER.raw_decode(text, idx)
        except Exception:
            return []
        if isinstance(obj, dict):
            docs.append(obj)
        idx = end
    return docs


def _iter_json_documents_with_separators(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    docs: List[Dict[str, Any]] = []
    separators: List[str] = []
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = _JSON_DECODER.raw_decode(text, idx)
        except Exception:
            return [], []
        if not isinstance(obj, dict):
            return [], []
        docs.append(obj)

        next_idx = end
        while next_idx < n and text[next_idx].isspace():
            next_idx += 1
        if next_idx < n:
            separators.append(text[end:next_idx])
        idx = next_idx
    return docs, separators


def _decode_response_docs(payload: bytes) -> List[Dict[str, Any]]:
    text = _safe_utf8(payload)
    docs = _iter_json_documents(text)
    if docs:
        return docs
    try:
        one = json.loads(text)
    except Exception:
        return []
    if isinstance(one, dict):
        return [one]
    return []


def _decode_response_docs_with_separators(payload: bytes) -> Tuple[List[Dict[str, Any]], List[str]]:
    text = _safe_utf8(payload)
    docs, separators = _iter_json_documents_with_separators(text)
    if docs:
        return docs, separators
    return [], []


def _encode_response_docs(docs: List[Dict[str, Any]], separators: Optional[List[str]] = None) -> bytes:
    serialized = [json.dumps(doc, ensure_ascii=False, separators=(",", ":")) for doc in docs]
    if not serialized:
        return b""
    if separators and len(separators) == len(serialized) - 1:
        out = [serialized[0]]
        for i, part in enumerate(serialized[1:], start=1):
            out.append(separators[i - 1])
            out.append(part)
        return "".join(out).encode("utf-8")
    return "".join(serialized).encode("utf-8")


def _parse_ws_payloads(payload: bytes) -> List[bytes]:
    """Parse WebSocket frames (client masked / server usually unmasked) into payloads."""
    out: List[bytes] = []
    i = 0
    n = len(payload or b"")
    while i + 2 <= n:
        b0 = payload[i]
        b1 = payload[i + 1]
        opcode = b0 & 0x0F
        rsv1 = (b0 & 0x40) != 0
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
            raw = bytes(data)
            if rsv1:
                inflated = _inflate_ws_permessage_deflate(raw)
                if inflated:
                    raw = inflated
            out.append(raw)
        elif opcode == 0x8:  # close
            break
    return out


def _inflate_ws_permessage_deflate(payload: bytes) -> bytes:
    """RFC 7692: RSV1 payload is raw DEFLATE, often without zlib header/trailer."""
    if not payload:
        return b""
    import zlib

    for candidate in (payload + b"\x00\x00\xff\xff", payload):
        try:
            return zlib.decompress(candidate, -zlib.MAX_WBITS)
        except Exception:
            continue
    return b""


def _encode_ws_text_frame(payload: bytes, *, masked: bool = True) -> bytes:
    """Build a single WebSocket text frame (FIN=1, opcode=1) for Inline inject."""
    data = bytearray(payload or b"")
    header = bytearray()
    header.append(0x81)
    n = len(data)
    mask_bit = 0x80 if masked else 0x00
    if n < 126:
        header.append(mask_bit | n)
    elif n < 65536:
        header.append(mask_bit | 126)
        header.extend(int(n).to_bytes(2, "big"))
    else:
        header.append(mask_bit | 127)
        header.extend(int(n).to_bytes(8, "big"))
    if masked:
        mask = os.urandom(4)
        header.extend(mask)
        for j in range(n):
            data[j] ^= mask[j % 4]
    return bytes(header) + bytes(data)


def _ws_json_objects(payload: bytes) -> List[Dict[str, Any]]:
    """Unwrap WS frames (or raw JSON) into dict objects."""
    # Mirror's WebSocket plugin emits an already-decoded synthetic JSON body.
    # Feeding a long JSON string to the frame parser can accidentally interpret
    # arbitrary JSON bytes as valid frame opcodes and suppress the real body.
    stripped = (payload or b"").lstrip()
    frames = [] if stripped[:1] in (b"{", b"[") else _parse_ws_payloads(payload)
    candidates = frames if frames else ([payload] if payload else [])
    docs: List[Dict[str, Any]] = []
    for raw in candidates:
        text = _safe_utf8(raw).strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                docs.append(obj)
                continue
        except Exception:
            pass
        for doc in _iter_json_documents(text):
            docs.append(doc)
    return docs


def _request_obj_for_rewrite(
    req: Dict[str, Any], payload: bytes
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Return (json_obj, needs_ws_rewrap) for prompt MASK/BLOCK rewrite."""
    obj = _parse_request_json(req, payload)
    if obj is not None:
        return obj, False
    if not _is_ws_unit(req):
        return None, False
    for doc in _ws_json_objects(payload or b""):
        if not isinstance(doc, dict):
            continue
        if _is_grok_direct_upload_request(doc) or _is_grok_upload_json(doc):
            continue
        if _is_grok_ws_control_event(doc):
            continue
        if _extract_prompt(doc) or _chat_has_attachments(doc):
            return doc, True
    docs = _ws_json_objects(payload or b"")
    if docs:
        return docs[0], True
    return None, False


def _wrap_rewrite_payload(encoded: bytes, *, needs_ws_rewrap: bool) -> bytes:
    if needs_ws_rewrap:
        # Client→server frames must be masked (RFC 6455).
        return _encode_ws_text_frame(encoded, masked=True)
    return encoded


def _is_ws_unit(req: Dict[str, Any]) -> bool:
    unit = str(req.get("unit_type") or "").lower()
    if unit == "ws_frame":
        return True
    path = _grok_path_base(req)
    return "/ws/mgw" in path


def _extract_prompt_from_ws_docs(docs: List[Dict[str, Any]]) -> str:
    for obj in docs:
        prompt = _extract_prompt(obj)
        if prompt:
            return prompt
        # file-only turn still counts
        if _collect_attachment_refs(obj):
            return ""
    return ""


def _extract_response_text_and_message_id(docs: List[Dict[str, Any]]) -> Tuple[str, str]:
    tokens: List[str] = []
    message_id = ""
    model_text = ""
    openai_done = False
    chunk_tokens: List[str] = []

    for doc in docs:
        event = doc.get("event") if isinstance(doc.get("event"), dict) else None
        et = str((event or {}).get("type") or doc.get("type") or "")
        src = event if isinstance(event, dict) else doc

        # Grok /ws/mgw (2026): response.chunk with CHANNEL_ASSISTANT_RESPONSE tokens.
        if et == "response.chunk":
            rid = src.get("response_id")
            if isinstance(rid, str) and rid:
                message_id = rid
            chunk = src.get("chunk") if isinstance(src.get("chunk"), dict) else None
            if isinstance(chunk, dict):
                text_obj = chunk.get("text") if isinstance(chunk.get("text"), dict) else None
                if isinstance(text_obj, dict):
                    channel = str(text_obj.get("channel") or "")
                    value = text_obj.get("text")
                    if (
                        isinstance(value, str)
                        and value
                        and channel in ("CHANNEL_ASSISTANT_RESPONSE", "", "CHANNEL_ASSISTANT")
                    ):
                        # Skip thinking/notetaker headers.
                        if channel != "CHANNEL_ASSISTANT_NOTETAKER_HEADER":
                            chunk_tokens.append(value)
                elif isinstance(chunk.get("text"), str) and chunk.get("text"):
                    chunk_tokens.append(str(chunk.get("text")))
            continue

        if et in (
            "response.output_text.delta",
            "response.text.delta",
            "response.audio_transcript.delta",
        ):
            delta = src.get("delta")
            if isinstance(delta, str) and delta:
                tokens.append(delta)
            continue
        if et in ("response.output_text.done", "response.text.done"):
            text = src.get("text")
            if isinstance(text, str) and text.strip():
                model_text = text.strip()
            rid = src.get("response_id")
            if isinstance(rid, str) and rid:
                message_id = rid
            openai_done = True
            continue
        if et in ("response.completed", "response.done", "response.persisted"):
            openai_done = True
            resp = src.get("response") if isinstance(src.get("response"), dict) else None
            if isinstance(resp, dict):
                rid = resp.get("id") or resp.get("response_id") or resp.get("responseId")
                if isinstance(rid, str) and rid:
                    message_id = rid
                output = resp.get("output")
                if isinstance(output, list):
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
                    if parts:
                        model_text = "".join(parts)
            rid = src.get("response_id")
            if isinstance(rid, str) and rid:
                message_id = rid
            continue
        if et == "response.output_item.done":
            item = src.get("item") if isinstance(src.get("item"), dict) else None
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip():
                            parts.append(c["text"])
                    if parts:
                        model_text = "".join(parts)
            rid = src.get("response_id")
            if isinstance(rid, str) and rid:
                message_id = rid
            openai_done = True
            continue

        result = doc.get("result")
        if not isinstance(result, dict):
            continue

        # Legacy Grok HTTP/WS: result.response.* (token, messageTag, modelResponse)
        resp = result.get("response") if isinstance(result.get("response"), dict) else result

        response_id = resp.get("responseId") or result.get("responseId")
        if isinstance(response_id, str) and response_id:
            message_id = response_id

        token = resp.get("token")
        tag = str(resp.get("messageTag") or "")
        is_thinking = bool(resp.get("isThinking"))
        if isinstance(token, str) and tag.lower() == "final" and not is_thinking:
            tokens.append(token)

        model_resp = resp.get("modelResponse")
        if isinstance(model_resp, dict):
            rid = model_resp.get("responseId")
            if isinstance(rid, str) and rid:
                message_id = rid
            message = model_resp.get("message")
            if isinstance(message, str) and message:
                model_text = message

    if model_text:
        return model_text, message_id
    if chunk_tokens:
        return "".join(chunk_tokens), message_id
    joined = "".join(tokens)
    if joined or openai_done:
        return joined, message_id
    return "", message_id


# Per-session last prompt UI message_id so response output_filter can join the same row.
# Must NOT use the WS upgrade request_id alone — one /ws/mgw connection spans many turns,
# and output_filter dedupes on (service|session|message_id).
_GROK_SESSION_UI_MID: Dict[str, str] = {}
_GROK_SESSION_UI_MID_MAX = 256
_GROK_SESSION_UI_LOCK = threading.Lock()
_GROK_RESPONSE_OVERRIDE_EMITTED: Dict[str, bool] = {}
_GROK_RESPONSE_OVERRIDE_RID: Dict[str, str] = {}


def _grok_remember_prompt_ui_mid(session_id: str, ui_mid: str) -> None:
    sid = (session_id or "").strip()
    mid = (ui_mid or "").strip()
    if not sid or not mid:
        return
    with _GROK_SESSION_UI_LOCK:
        _GROK_SESSION_UI_MID[sid] = mid
        while len(_GROK_SESSION_UI_MID) > _GROK_SESSION_UI_MID_MAX:
            _GROK_SESSION_UI_MID.pop(next(iter(_GROK_SESSION_UI_MID)), None)


def _grok_lookup_prompt_ui_mid(session_id: str) -> str:
    sid = (session_id or "").strip()
    if not sid:
        return ""
    with _GROK_SESSION_UI_LOCK:
        return str(_GROK_SESSION_UI_MID.get(sid) or "")


# WS vs load-responses dedup across SHM worker processes.
# In-memory alone is wrong: WS may land on W9 and load-responses on W13.
_GROK_TURN_HANDLED: Dict[str, float] = {}
_GROK_TURN_HANDLED_LOCK = threading.Lock()
_GROK_TURN_HANDLED_TTL_SEC = 180.0
_GROK_TURN_HANDLED_MAX = 512
_GROK_TURN_DEDUP_PATH = os.environ.get(
    "AIVAX_GROK_TURN_DEDUP_PATH", "/run/aivax/grok_turn_handled.sqlite"
)
_GROK_TURN_DB_READY = False
_GROK_TURN_DB_LOCK = threading.Lock()
_GROK_TURN_DB_LOCAL = threading.local()


def _grok_turn_key(kind: str, session_id: str, text: str) -> str:
    sid = (session_id or "").strip() or "-"
    digest = hashlib.sha256(
        (text or "").strip().encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    return f"{kind}|{sid}|{digest}"


def _grok_turn_rid_key(kind: str, response_id: str) -> str:
    rid = (response_id or "").strip()
    return f"{kind}|rid|{rid}" if rid else ""


def _grok_turn_dedup_enabled() -> bool:
    return bool((_GROK_TURN_DEDUP_PATH or "").strip())


def _grok_turn_db_ensure() -> bool:
    global _GROK_TURN_DB_READY
    if _GROK_TURN_DB_READY:
        return True
    path = (_GROK_TURN_DEDUP_PATH or "").strip()
    if not path:
        return False
    with _GROK_TURN_DB_LOCK:
        if _GROK_TURN_DB_READY:
            return True
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, mode=0o750, exist_ok=True)
            lock_path = path + ".initlock"
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
                import sqlite3

                conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
                try:
                    conn.execute("PRAGMA busy_timeout=10000")
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS turn_handled (
                            key TEXT PRIMARY KEY,
                            expiry_wall REAL NOT NULL
                        )
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS turn_handled_expiry "
                        "ON turn_handled(expiry_wall)"
                    )
                    conn.commit()
                finally:
                    conn.close()
            finally:
                try:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                os.close(fd)
            _GROK_TURN_DB_READY = True
            return True
        except Exception:
            logger.exception("grok turn dedup sqlite init failed path=%s", path)
            return False


def _grok_turn_db_conn():
    import sqlite3

    if not _grok_turn_db_ensure():
        return None
    conn = getattr(_GROK_TURN_DB_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            _GROK_TURN_DB_LOCAL.conn = None
    path = (_GROK_TURN_DEDUP_PATH or "").strip()
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    _GROK_TURN_DB_LOCAL.conn = conn
    return conn


def _grok_turn_db_put(keys: List[str]) -> None:
    if not keys or not _grok_turn_dedup_enabled():
        return
    conn = _grok_turn_db_conn()
    if conn is None:
        return
    expiry = time.time() + _GROK_TURN_HANDLED_TTL_SEC
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO turn_handled(key, expiry_wall) VALUES (?, ?)",
            [(k, expiry) for k in keys],
        )
        conn.execute(
            "DELETE FROM turn_handled WHERE expiry_wall < ?",
            (time.time(),),
        )
        conn.commit()
    except Exception:
        logger.exception("grok turn dedup sqlite put failed")


def _grok_turn_db_has(keys: List[str]) -> bool:
    if not keys or not _grok_turn_dedup_enabled():
        return False
    conn = _grok_turn_db_conn()
    if conn is None:
        return False
    now = time.time()
    try:
        for key in keys:
            row = conn.execute(
                "SELECT expiry_wall FROM turn_handled WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                continue
            if float(row[0]) < now:
                conn.execute("DELETE FROM turn_handled WHERE key = ?", (key,))
                conn.commit()
                continue
            return True
    except Exception:
        logger.exception("grok turn dedup sqlite get failed")
    return False


def _grok_turn_handled_clear_for_tests() -> None:
    """Test helper: wipe local + shared dedup state."""
    with _GROK_TURN_HANDLED_LOCK:
        _GROK_TURN_HANDLED.clear()
    if not _grok_turn_dedup_enabled():
        return
    conn = _grok_turn_db_conn()
    if conn is None:
        return
    try:
        conn.execute("DELETE FROM turn_handled")
        conn.commit()
    except Exception:
        pass


def _grok_turn_mark_handled(
    kind: str, session_id: str, text: str, response_id: str = ""
) -> None:
    token = (text or "").strip()
    rid = (response_id or "").strip()
    if not token and not rid:
        return
    now = time.monotonic()
    keys: List[str] = []
    if token:
        keys.append(_grok_turn_key(kind, session_id, token))
    rid_key = _grok_turn_rid_key(kind, rid)
    if rid_key:
        keys.append(rid_key)
    with _GROK_TURN_HANDLED_LOCK:
        expired = [
            k
            for k, ts in _GROK_TURN_HANDLED.items()
            if (now - ts) > _GROK_TURN_HANDLED_TTL_SEC
        ]
        for k in expired:
            _GROK_TURN_HANDLED.pop(k, None)
        for key in keys:
            _GROK_TURN_HANDLED[key] = now
        while len(_GROK_TURN_HANDLED) > _GROK_TURN_HANDLED_MAX:
            _GROK_TURN_HANDLED.pop(next(iter(_GROK_TURN_HANDLED)), None)
    _grok_turn_db_put(keys)


def _grok_turn_already_handled(
    kind: str, session_id: str, text: str, response_id: str = ""
) -> bool:
    token = (text or "").strip()
    rid = (response_id or "").strip()
    now = time.monotonic()
    candidates: List[str] = []
    if token:
        candidates.append(_grok_turn_key(kind, session_id, token))
    rid_key = _grok_turn_rid_key(kind, rid)
    if rid_key:
        candidates.append(rid_key)
    if not candidates:
        return False
    with _GROK_TURN_HANDLED_LOCK:
        for key in candidates:
            ts = _GROK_TURN_HANDLED.get(key)
            if ts is None:
                continue
            if (now - ts) > _GROK_TURN_HANDLED_TTL_SEC:
                _GROK_TURN_HANDLED.pop(key, None)
                continue
            return True
    return _grok_turn_db_has(candidates)


def _load_responses_latest_assistant(
    obj: Dict[str, Any],
) -> Tuple[str, str]:
    """Return (assistant_text, response_id) from load-responses body."""
    responses = obj.get("responses")
    if not isinstance(responses, list):
        return "", ""
    for row in reversed(responses):
        if not isinstance(row, dict):
            continue
        if str(row.get("sender") or "").lower() in ("human", "user"):
            continue
        msg = row.get("message")
        if not (isinstance(msg, str) and msg.strip()):
            continue
        rid = str(
            row.get("responseId")
            or row.get("response_id")
            or row.get("id")
            or ""
        ).strip()
        return msg.strip(), rid
    return "", ""


def _grok_ui_message_id_for_prompt(req: Dict[str, Any]) -> str:
    """Unique per prompt tee: keep full `<upgrade>-ws-<hex>` (do not strip)."""
    rid = str(req.get("request_id") or "").strip()
    return rid


def _grok_ui_message_id_for_response(req: Dict[str, Any], session_id: str) -> str:
    """Match the latest prompt for this Grok session; fall back to response_id."""
    stashed = _grok_lookup_prompt_ui_mid(session_id)
    if stashed:
        return stashed
    rid = str(req.get("_grok_output_message_id") or req.get("request_id") or "").strip()
    return rid


# Accumulate CHANNEL_ASSISTANT_RESPONSE chunks until response.persisted (process-local).
# Parallel Envoy UDS tees reorder frames; key by response_id, sort by event_id suffix.
# Flush on persisted (not done): done often races ahead of in-flight chunk tees.
_GROK_WS_RESP_LOCK = threading.Lock()
_GROK_WS_RESP_BUF: Dict[str, List[Tuple[int, str]]] = {}
_GROK_WS_RESP_SEEN: Dict[str, Set[str]] = {}
_GROK_WS_RESP_META: Dict[str, Dict[str, str]] = {}
_GROK_WS_RESP_PENDING_FINAL: Set[str] = set()
_GROK_WS_RESP_READY_FINAL: Set[str] = set()
_GROK_WS_RESP_EMITTED: Set[str] = set()
_GROK_WS_RESP_BUF_MAX_KEYS = 128
_GROK_WS_RESP_SETTLE_SEC = 0.4
_GROK_WS_EVENT_SEQ_RE = re.compile(r"_(\d+)$")


def _grok_ws_resp_buf_key(response_id: str) -> str:
    return (response_id or "").strip() or "-"


def _grok_ws_event_seq(event_id: Any) -> int:
    if not isinstance(event_id, str) or not event_id:
        return 0
    m = _GROK_WS_EVENT_SEQ_RE.search(event_id)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except Exception:
        return 0


def _grok_ws_resp_buf_trim_locked() -> None:
    while len(_GROK_WS_RESP_BUF) > _GROK_WS_RESP_BUF_MAX_KEYS:
        old_key = next(iter(_GROK_WS_RESP_BUF))
        _GROK_WS_RESP_BUF.pop(old_key, None)
        _GROK_WS_RESP_SEEN.pop(old_key, None)
        _GROK_WS_RESP_META.pop(old_key, None)
        _GROK_WS_RESP_PENDING_FINAL.discard(old_key)
        _GROK_WS_RESP_READY_FINAL.discard(old_key)
        _GROK_WS_RESP_EMITTED.discard(old_key)


def _grok_ws_resp_buf_append(
    session_id: str, response_id: str, token: str, event_id: Any = ""
) -> None:
    if not token:
        return
    key = _grok_ws_resp_buf_key(response_id)
    eid = str(event_id or "")
    with _GROK_WS_RESP_LOCK:
        _grok_ws_resp_buf_trim_locked()
        seen = _GROK_WS_RESP_SEEN.setdefault(key, set())
        if eid and eid in seen:
            return
        if eid:
            seen.add(eid)
        _GROK_WS_RESP_BUF.setdefault(key, []).append((_grok_ws_event_seq(event_id), token))
        _GROK_WS_RESP_META[key] = {
            "session_id": session_id or "",
            "response_id": response_id or "",
        }


def _grok_ws_resp_buf_flush(session_id: str, response_id: str) -> str:
    del session_id  # key is response_id only; kept for call-site compatibility
    key = _grok_ws_resp_buf_key(response_id)
    with _GROK_WS_RESP_LOCK:
        parts = _GROK_WS_RESP_BUF.pop(key, [])
        _GROK_WS_RESP_SEEN.pop(key, None)
        _GROK_WS_RESP_META.pop(key, None)
        _GROK_WS_RESP_PENDING_FINAL.discard(key)
        _GROK_WS_RESP_READY_FINAL.discard(key)
    if not parts:
        return ""
    parts.sort(key=lambda row: row[0])
    return "".join(tok for _, tok in parts)


def _grok_ws_resp_mark_pending_final(response_id: str) -> None:
    key = _grok_ws_resp_buf_key(response_id)
    with _GROK_WS_RESP_LOCK:
        if key in _GROK_WS_RESP_EMITTED:
            return
        _GROK_WS_RESP_PENDING_FINAL.add(key)

    def _ready() -> None:
        with _GROK_WS_RESP_LOCK:
            if key in _GROK_WS_RESP_EMITTED:
                return
            if key in _GROK_WS_RESP_PENDING_FINAL:
                _GROK_WS_RESP_READY_FINAL.add(key)

    threading.Timer(_GROK_WS_RESP_SETTLE_SEC, _ready).start()


def _grok_ws_resp_pick_ready_finalize(response_id: str) -> str:
    """Return a response_id that is ready to flush, or empty."""
    key = _grok_ws_resp_buf_key(response_id)
    with _GROK_WS_RESP_LOCK:
        if key and key not in _GROK_WS_RESP_EMITTED:
            if key in _GROK_WS_RESP_READY_FINAL and _GROK_WS_RESP_BUF.get(key):
                return key
        for cand in list(_GROK_WS_RESP_READY_FINAL):
            if cand in _GROK_WS_RESP_EMITTED:
                continue
            if _GROK_WS_RESP_BUF.get(cand):
                return cand
    return ""


def _grok_ws_resp_mark_emitted(response_id: str) -> None:
    key = _grok_ws_resp_buf_key(response_id)
    with _GROK_WS_RESP_LOCK:
        _GROK_WS_RESP_EMITTED.add(key)
        _GROK_WS_RESP_PENDING_FINAL.discard(key)
        _GROK_WS_RESP_READY_FINAL.discard(key)
        if len(_GROK_WS_RESP_EMITTED) > _GROK_WS_RESP_BUF_MAX_KEYS:
            for old in list(_GROK_WS_RESP_EMITTED)[: len(_GROK_WS_RESP_EMITTED) // 2]:
                _GROK_WS_RESP_EMITTED.discard(old)


_GROK_SESSION_MODEL: Dict[str, str] = {}
_GROK_SESSION_MODEL_LOCK = threading.Lock()
_GROK_MODEL_REJECT = frozenset(
    {"user", "assistant", "system", "model", "human", "bot", "tool", "none", "null"}
)


def _grok_conn_key(req: Dict[str, Any]) -> str:
    rid = str(req.get("request_id") or "")
    if "-ws-" in rid:
        return rid.rsplit("-ws-", 1)[0]
    return rid.strip()


def _grok_model_text(value: Any) -> str:
    """Keep the model string Grok sent. Role words are not model names."""
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or len(name) > 80 or name.lower() in _GROK_MODEL_REJECT:
        return ""
    if any(ord(ch) < 32 for ch in name):
        return ""
    return name


def _grok_doc_session_id(doc: Dict[str, Any]) -> str:
    sid = doc.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    event = doc.get("event") if isinstance(doc.get("event"), dict) else None
    session = event.get("session") if isinstance(event, dict) else None
    if isinstance(session, dict):
        for key in ("id", "session_id"):
            value = session.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return _extract_conversation_id_from_obj(doc)


def _grok_model_from_doc(doc: Dict[str, Any]) -> str:
    """Model display value from session.create / response metadata, unchanged."""
    event = doc.get("event") if isinstance(doc.get("event"), dict) else None
    slots: List[Any] = []
    if isinstance(event, dict):
        session = event.get("session")
        if isinstance(session, dict):
            slots.append(session.get("model"))
        response = event.get("response")
        if isinstance(response, dict):
            slots.append(response.get("model"))
        slots.append(event.get("model"))
    session = doc.get("session")
    if isinstance(session, dict):
        slots.append(session.get("model"))
    slots.append(doc.get("model"))
    for value in slots:
        name = _grok_model_text(value)
        if name:
            return name
    return ""


def _grok_remember_model(key: str, model: str) -> None:
    sid = str(key or "").strip()
    name = str(model or "").strip()
    if not sid or not name:
        return
    with _GROK_SESSION_MODEL_LOCK:
        _GROK_SESSION_MODEL[sid] = name
        if len(_GROK_SESSION_MODEL) > 4000:
            for old in list(_GROK_SESSION_MODEL)[:1000]:
                _GROK_SESSION_MODEL.pop(old, None)


def _grok_recall_model(key: str) -> str:
    sid = str(key or "").strip()
    if not sid:
        return ""
    with _GROK_SESSION_MODEL_LOCK:
        return _GROK_SESSION_MODEL.get(sid, "")


def _grok_observe_models(req: Dict[str, Any], docs: List[Dict[str, Any]]) -> str:
    found = ""
    conn = _grok_conn_key(req)
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        name = _grok_model_from_doc(doc)
        if not name:
            continue
        found = name
        _grok_remember_model(_grok_doc_session_id(doc), name)
        _grok_remember_model(conn, name)
    if found:
        req["_grok_model"] = found
    return found


def _grok_bind_model(req: Dict[str, Any], session_id: str = "") -> str:
    current = str(req.get("_grok_model") or "").strip()
    if current:
        _grok_remember_model(session_id, current)
        _grok_remember_model(_grok_conn_key(req), current)
        logger.info(
            "grok_model name=%s session=%s",
            current,
            session_id or _grok_conn_key(req) or "-",
        )
        return current
    found = _grok_recall_model(session_id) or _grok_recall_model(_grok_conn_key(req))
    if found:
        req["_grok_model"] = found
        logger.info(
            "grok_model name=%s session=%s",
            found,
            session_id or _grok_conn_key(req) or "-",
        )
    return found


def extract_model(req: Dict[str, Any], payload: bytes) -> str:
    """Model Grok sent on this socket. session.create carries it ahead of the prompt."""
    current = str(req.get("_grok_model") or "").strip()
    if current:
        return current
    docs = _ws_json_objects(payload or b"")
    if not docs:
        parsed = _parse_json_any(payload or b"")
        if isinstance(parsed, dict):
            docs = [parsed]
    found = _grok_observe_models(req, docs)
    if found:
        return found
    session_id = str(req.get("_grok_output_session_id") or "")
    if not session_id and docs:
        session_id = _grok_doc_session_id(docs[0])
    return _grok_bind_model(req, session_id)


def extract(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    if _is_grok_auth_session_response(req):
        _note_grok_auth_session(req, payload)
        req["_extract_final"] = False
        return ""
    if _is_grok_profile_page_response(req):
        _log_conversation_page_profile(req, payload)
        req["_extract_final"] = False
        return ""

    direction = str(req.get("direction") or "").lower()

    # Deep INLINE tees raw WebSocket frames (opcode + mask). Unwrap before JSON.
    if _is_ws_unit(req) and _is_grok_chat_turn_path(req):
        docs = _ws_json_objects(payload or b"")
        meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        _grok_observe_models(req, docs)
        if not docs:
            if direction == "request" and len(payload or b"") >= 100:
                _dump_grok_ws_payload("req_nodocs", payload or b"", meta, note="no_json_docs")
            if direction == "request":
                req["_grok_ws_skip"] = True
            req["_extract_final"] = False
            return ""
        if direction == "response":
            # Plugin-assembled final envelope (if any).
            for obj in docs:
                if obj.get("aivax_ws_final"):
                    text = str(obj.get("text") or "")
                    session_id = str(obj.get("session_id") or "")
                    response_id = str(obj.get("response_id") or "")
                    if session_id:
                        req["_grok_output_session_id"] = session_id
                    if response_id:
                        req["_grok_output_message_id"] = response_id
                    req["_grok_ui_message_id"] = _grok_ui_message_id_for_response(
                        req, session_id
                    )
                    _grok_bind_model(req, session_id)
                    req["_extract_final"] = bool(text.strip())
                    if text.strip():
                        _grok_turn_mark_handled(
                            "output", session_id, text, response_id=response_id
                        )
                        logger.info(
                            "grok extract_ws aivax_ws_final ok text_len=%d session=%s response_id=%s preview=%r",
                            len(text),
                            session_id or "-",
                            response_id or "-",
                            text[:120],
                        )
                    return text

            session_key = ""
            response_id = ""
            # Finalize only on persisted/completed. response.done often races ahead of
            # in-flight Envoy chunk tees on other UDS connections → empty/partial flush.
            finalize = False
            # Pass 1: ids / done flags (chunk frames may share a tee with created/done).
            for doc in docs:
                if isinstance(doc.get("session_id"), str) and doc["session_id"].strip():
                    session_key = doc["session_id"].strip()
                event = doc.get("event") if isinstance(doc.get("event"), dict) else None
                et = str((event or {}).get("type") or "")
                src = event if isinstance(event, dict) else {}
                rid = src.get("response_id")
                if isinstance(rid, str) and rid:
                    response_id = rid
                if et == "response.created" and isinstance(src.get("response"), dict):
                    rid2 = src["response"].get("id")
                    if isinstance(rid2, str) and rid2:
                        response_id = rid2
                if et == "response.done" and isinstance(src.get("response"), dict):
                    rid2 = src["response"].get("id")
                    if isinstance(rid2, str) and rid2:
                        response_id = rid2
                if et in ("response.persisted", "response.completed"):
                    finalize = True
                    if et == "response.persisted":
                        rid3 = src.get("response_id")
                        if isinstance(rid3, str) and rid3:
                            response_id = rid3

            # Pass 2: buffer assistant tokens.
            for doc in docs:
                event = doc.get("event") if isinstance(doc.get("event"), dict) else None
                if not isinstance(event, dict):
                    continue
                if str(event.get("type") or "") != "response.chunk":
                    continue
                rid = event.get("response_id")
                use_rid = rid if isinstance(rid, str) and rid else response_id
                chunk = event.get("chunk") if isinstance(event.get("chunk"), dict) else None
                if not isinstance(chunk, dict):
                    continue
                text_obj = chunk.get("text") if isinstance(chunk.get("text"), dict) else None
                if not isinstance(text_obj, dict):
                    continue
                if str(text_obj.get("channel") or "") != "CHANNEL_ASSISTANT_RESPONSE":
                    continue
                value = text_obj.get("text")
                if isinstance(value, str) and value:
                    _grok_ws_resp_buf_append(
                        session_key, use_rid, value, event.get("event_id")
                    )

            text, message_id = _extract_response_text_and_message_id(docs)
            if not session_key:
                conversation_id = _extract_conversation_id(req)
                session_key = conversation_id or ""
                if not session_key:
                    for doc in docs:
                        cid = _extract_conversation_id_from_obj(doc)
                        if cid:
                            session_key = cid
                            break
            if not response_id and message_id:
                response_id = message_id
            if session_key:
                req["_grok_output_session_id"] = session_key
            if response_id or message_id:
                req["_grok_output_message_id"] = response_id or message_id
            req["_grok_ui_message_id"] = _grok_ui_message_id_for_response(
                req, session_key
            )

            if finalize:
                buffered = _grok_ws_resp_buf_flush(session_key, response_id)
                final_text = buffered or (text or "").strip()
                if not final_text.strip():
                    # Persisted raced ahead of chunk tees — settle then finalize on a later tee.
                    _grok_ws_resp_mark_pending_final(response_id)
                    req["_extract_final"] = False
                    return ""
                _grok_ws_resp_mark_emitted(response_id)
                _grok_bind_model(req, session_key)
                req["_extract_final"] = True
                _grok_turn_mark_handled(
                    "output", session_key, final_text, response_id=response_id or message_id or ""
                )
                logger.info(
                    "grok extract_ws response ok frames=%d text_len=%d message_id=%s session=%s ui_mid=%s preview=%r",
                    len(docs),
                    len(final_text),
                    response_id or message_id or "-",
                    session_key or "-",
                    req.get("_grok_ui_message_id") or "-",
                    final_text[:120],
                )
                return final_text

            # After empty-persisted settle window: flush on a subsequent tee (follow-up etc).
            ready_rid = _grok_ws_resp_pick_ready_finalize(response_id)
            if ready_rid:
                buffered = _grok_ws_resp_buf_flush(session_key, ready_rid)
                final_text = buffered.strip()
                if final_text:
                    if not response_id:
                        response_id = ready_rid
                        req["_grok_output_message_id"] = ready_rid
                    _grok_ws_resp_mark_emitted(ready_rid)
                    _grok_bind_model(req, session_key)
                    req["_extract_final"] = True
                    _grok_turn_mark_handled(
                        "output", session_key, final_text, response_id=response_id or ready_rid
                    )
                    logger.info(
                        "grok extract_ws response ok frames=%d text_len=%d message_id=%s session=%s ui_mid=%s preview=%r",
                        len(docs),
                        len(final_text),
                        response_id or ready_rid or "-",
                        session_key or "-",
                        req.get("_grok_ui_message_id") or "-",
                        final_text[:120],
                    )
                    return final_text

            # Streaming mid-response (including response.done): keep buffering.
            req["_extract_final"] = False
            return ""

        # request: user prompt on conversation.item.create / response.create / message
        for obj in docs:
            if _is_grok_ws_control_event(obj):
                continue
            if _is_grok_direct_upload_request(obj) or _is_grok_upload_json(obj):
                continue
            prompt = _extract_prompt(obj)
            if prompt:
                cid = _extract_conversation_id_from_obj(obj)
                if not cid and isinstance(obj.get("session_id"), str):
                    cid = obj["session_id"].strip()
                if cid:
                    req["_grok_output_session_id"] = cid
                _grok_bind_model(req, cid or "")
                ui_mid = _grok_ui_message_id_for_prompt(req)
                req["_grok_ui_message_id"] = ui_mid
                _grok_remember_prompt_ui_mid(cid or "", ui_mid)
                att_refs = _collect_attachment_refs(obj) or peek_recent_chat_files(req)
                _grok_turn_mark_handled("prompt", cid or "", prompt)
                logger.info(
                    "grok extract_ws request ok frames=%d prompt_len=%d path=%s session=%s ui_mid=%s attachments=%s preview=%r",
                    len(docs),
                    len(prompt),
                    _grok_path_base(req),
                    cid or "-",
                    ui_mid or "-",
                    att_refs,
                    prompt[:120],
                )
                return prompt
            # In-frame file mentions only. peek_recent_chat_files alone must NOT
            # promote control/ack frames into empty file-only UI rows after a
            # successful text+file turn (OpenSearch blank prompt/response).
            att_only = _collect_attachment_refs(obj)
            if att_only:
                cid = _extract_conversation_id_from_obj(obj)
                if cid:
                    req["_grok_output_session_id"] = cid
                logger.info(
                    "grok extract_ws request file-only frames=%d attachments=%s",
                    len(docs),
                    att_only,
                )
                return ""
        # No user prompt in this frame (control / ack / compressed miss).
        meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        if len(payload or b"") >= 200:
            _dump_grok_ws_payload("req_miss", payload or b"", meta, note=f"docs={len(docs)}")
        req["_grok_ws_skip"] = True
        req["_extract_final"] = False
        return ""

    if _is_json_response(req):
        if not _is_grok_chat_turn_path(req):
            req["_extract_final"] = False
            return ""
        # Plugin-assembled WS assistant final (http_version=ws).
        obj = _parse_json_any(payload)
        if isinstance(obj, dict) and obj.get("aivax_ws_final"):
            text = str(obj.get("text") or "")
            session_id = str(obj.get("session_id") or "")
            response_id = str(obj.get("response_id") or "")
            if session_id:
                req["_grok_output_session_id"] = session_id
            if response_id:
                req["_grok_output_message_id"] = response_id
            req["_extract_final"] = bool(text.strip())
            if text.strip():
                _grok_turn_mark_handled(
                    "output", session_id, text, response_id=response_id
                )
            return text
        # load-responses: HTTP fallback after WS chat — prefer latest assistant message
        # for output filter; human prompt is recoverable via _extract_prompt on same body
        # when build_policy needs attachments (normalize_upload promotes files).
        path_base = _grok_path_base(req)
        if path_base.endswith("/load-responses"):
            obj = _parse_json_any(payload)
            if isinstance(obj, dict):
                conversation_id = _extract_conversation_id(req)
                if conversation_id:
                    req["_grok_output_session_id"] = conversation_id
                human = _extract_latest_human_from_load_responses(obj)
                if human:
                    if _grok_turn_already_handled("prompt", conversation_id, human):
                        logger.info(
                            "grok load-responses skip deferred_human; ws already handled "
                            "session=%s prompt_len=%d",
                            conversation_id or "-",
                            len(human),
                        )
                    else:
                        req["_grok_deferred_human_prompt"] = human
                        _grok_turn_mark_handled("prompt", conversation_id, human)
                assistant, assistant_rid = _load_responses_latest_assistant(obj)
                if assistant and _grok_turn_already_handled(
                    "output", conversation_id, assistant, response_id=assistant_rid
                ):
                    logger.info(
                        "grok load-responses skip output; ws already handled "
                        "session=%s text_len=%d response_id=%s",
                        conversation_id or "-",
                        len(assistant),
                        assistant_rid or "-",
                    )
                    req["_extract_final"] = False
                    return ""
                if assistant:
                    _grok_turn_mark_handled(
                        "output",
                        conversation_id,
                        assistant,
                        response_id=assistant_rid,
                    )
                    if assistant_rid:
                        req["_grok_output_message_id"] = assistant_rid
                req["_extract_final"] = bool(assistant)
                return assistant
        docs = _decode_response_docs(payload)
        if not docs:
            req["_extract_final"] = False
            return ""
        text, message_id = _extract_response_text_and_message_id(docs)
        conversation_id = _extract_conversation_id(req)
        if conversation_id:
            req["_grok_output_session_id"] = conversation_id
        else:
            # POST conversations/new: conversationId is in first chunk (result.conversation)
            for doc in docs:
                cid = _extract_conversation_id_from_obj(doc)
                if cid:
                    req["_grok_output_session_id"] = cid
                    break
        if message_id:
            req["_grok_output_message_id"] = message_id
        req["_extract_final"] = bool(text)
        return text

    if _is_grok_non_prompt_request(req, payload):
        req["_extract_final"] = False
        return ""

    if is_grok_file_upload_request(req, payload):
        req["_extract_final"] = False
        return ""

    # Requests on list/history/aux paths must not become duplicate prompts
    # (also guards H1 body mixups onto GET conversations?pageSize=…).
    chat_turn = _is_grok_chat_turn_path(req)
    if not chat_turn:
        req["_extract_final"] = False
        return ""

    obj = _parse_request_json(req, payload)
    if obj is None:
        # WS frames may arrive as raw JSON without content-type json_body unit.
        obj = _parse_json_any(payload)
        if not isinstance(obj, dict):
            return None
    if _is_grok_direct_upload_request(obj) or _is_grok_upload_json(obj):
        req["_extract_final"] = False
        return ""
    # Ignore WS control/ping frames. Keep response.create when it carries a
    # user item (prompt and/or file mentions).
    event = obj.get("event") if isinstance(obj.get("event"), dict) else None
    if isinstance(event, dict):
        if _is_grok_ws_control_event(obj):
            _grok_observe_models(req, [obj])
            req["_extract_final"] = False
            return ""
        et = str(event.get("type") or "")
        if et == "conversation.item.create" or et == "response.create" or (
            isinstance(event.get("item"), dict)
            and str((event.get("item") or {}).get("role") or "").lower() == "user"
        ):
            prompt = _extract_prompt(obj)
            if prompt:
                _grok_observe_models(req, [obj])
                _grok_bind_model(req, _grok_doc_session_id(obj))
                return prompt
            # file-only turn still counts as chat for attachment policy
            if _collect_attachment_refs(obj):
                return ""
            req["_extract_final"] = False
            return ""
    prompt = _extract_prompt(obj)
    if prompt:
        return prompt
    # Regen with empty message: avoid sending full JSON to filters
    if _is_regen_with_empty_message(obj):
        return ""
    if any(
        isinstance(obj.get(key), str) and str(obj.get(key) or "").strip()
        for key in ("message", "prompt", "input", "query", "text")
    ):
        return ""
    if _collect_attachment_refs(obj):
        req["_extract_final"] = False
        return ""
    return _safe_utf8(payload)


def extract_full_body(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    """Suricata eve / Deep INLINE response body: chat JSON/WS, or profile HTML account extract.

    Deep INLINE sets `_eve_full_body=true`, so account/profile pages must be
    handled here (not only in extract()).
    """
    direction = (req.get("direction") or "").lower()
    if direction == "response" and _is_grok_auth_session_response(req):
        _note_grok_auth_session(req, payload)
        req["_extract_final"] = False
        return ""
    if direction == "response" and _is_grok_profile_page_response(req):
        _log_conversation_page_profile(req, payload)
        req["_extract_final"] = False
        return ""
    return extract(req, payload)


def build_policy_api_payload(
    req: Dict[str, Any],
    payload: bytes,
    extracted_text: str,
) -> Optional[Dict[str, Any]]:
    direction = (req.get("direction") or "").lower()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    conversation_id = _extract_conversation_id(req)

    if direction == "response":
        message_id = str(req.get("_grok_output_message_id") or "")
        session_id = str(req.get("_grok_output_session_id") or conversation_id or "")
        if not message_id:
            docs = _ws_json_objects(payload) if _is_ws_unit(req) else _decode_response_docs(payload)
            _, fallback_message_id = _extract_response_text_and_message_id(docs)
            message_id = fallback_message_id

        user_id = ""
        email = ""
        if _account_lookup:
            info = _account_lookup.wait_for(
                "grok",
                _grok_session_keys(req, include_ip=False),
                env_name="AIVAX_GROK_ACCOUNT_WAIT_MS",
            )
            if info:
                user_id, email = info[0], info[1]
        response_name = _grok_cached_name(req)
        response_payload = {
            "llm_output": extracted_text,
            "user_id": user_id,
            "email": email,
            "ai_service": AI_SERVICE_GROK,
            "client_host": str(meta.get("host") or ""),
            "session_id": session_id,
            "message_id": message_id,
        }
        if response_name:
            response_payload["user_name"] = response_name
        return response_payload

    obj = _parse_request_json(req, payload)
    if obj is None:
        parsed = _parse_json_any(payload)
        obj = parsed if isinstance(parsed, dict) else None
    if obj is None and _is_ws_unit(req):
        for doc in _ws_json_objects(payload):
            if _extract_prompt(doc) or _collect_attachment_refs(doc):
                obj = doc
                break
        if obj is None:
            docs = _ws_json_objects(payload)
            obj = docs[0] if docs else None
    if not isinstance(obj, dict):
        # Still allow policy with extracted_text alone (WS prompt already unwrapped).
        if not (extracted_text or "").strip():
            return None
        obj = {}

    prompt = _extract_prompt(obj) if obj else ""
    if not prompt and obj and _is_regen_with_empty_message(obj):
        prompt = ""
    else:
        prompt = prompt or extracted_text

    attachments = build_policy_attachments(req, obj or {}, payload)
    # WS control frames / empty extracts must not create blank UI prompt rows.
    # Also: empty prompt without *in-frame* file refs (stale peek_recent only)
    # must not create a second OpenSearch row after a real text+file turn.
    if not (prompt or "").strip():
        in_frame_refs = _collect_attachment_refs(obj or {}) if obj else []
        if _is_ws_unit(req) and not in_frame_refs:
            return None
        if not attachments and not in_frame_refs:
            return None
        if in_frame_refs and not (prompt or "").strip():
            prompt = "사용자가 파일을 업로드하였습니다."

    user_id = str(obj.get("user_id") or "") if obj else ""
    email = str(obj.get("email") or "") if obj else ""
    if (not user_id or not email) and _account_lookup:
        info = _lookup_grok_account(req)
        if info:
            if not user_id:
                user_id = info[0]
            if not email:
                email = info[1]
    # x-userid is a rotating cookie correlator, not the account.
    # Identity comes from the profile-page email/user.id mapping.
    user_name = _grok_cached_name(req)
    if (user_id or email or user_name) and (prompt or "").strip():
        logger.info(
            "[Account Info] grok: chat_turn user_id=%s email=%s name=%s path=%s",
            user_id or "-",
            email or "-",
            user_name or "-",
            str(meta.get("path") or ""),
        )

    session_id = (
        str(req.get("_grok_output_session_id") or "")
        or conversation_id
        or _extract_conversation_id_from_obj(obj or {})
        or str((obj or {}).get("conversationId") or (obj or {}).get("session_id") or "")
    )

    chat_payload = {
        "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
        "prompt": prompt,
        "encoding": False,
        "user_id": user_id,
        "email": email,
        "ai_service": AI_SERVICE_GROK,
        "client_host": str(meta.get("host") or ""),
        "session_id": session_id,
        "attachments": attachments,
        "message_id": _extract_request_message_id(obj or {}),
    }
    if user_name:
        chat_payload["user_name"] = user_name
    return chat_payload


_GROK_CONSENT_PHRASES = frozenset({"동의", "동의합니다"})
try:
    from block_copy import (
        GROK_NOTICE_MARKERS as _GROK_NOTICE_MARKERS,
        MASK_TOKEN as _GROK_MASK_TOKEN,
    )
except ImportError:
    from core.block_copy import (
        GROK_NOTICE_MARKERS as _GROK_NOTICE_MARKERS,
        MASK_TOKEN as _GROK_MASK_TOKEN,
    )
_GROK_REWRITE_SKIP_KEYS = frozenset(
    {
        "session_id",
        "response_id",
        "conversation_id",
        "conversationId",
        "id",
        "type",
        "channel",
        "role",
        "user_id",
        "event_id",
        "item_id",
        "status",
        "model",
        "name",
        "castle_request_token",
        "authorization",
        "cookie",
        "signature",
        "file_id",
        "fileUri",
        "imageUrl",
    }
)
_GROK_UUIDISH_RE = re.compile(r"^[0-9a-fA-F-]{20,}$")


def _is_grok_notice_or_consent_text(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if raw.strip(" \t.。!！?？~…,，").strip() in _GROK_CONSENT_PHRASES:
        return True
    return any(marker in raw for marker in _GROK_NOTICE_MARKERS)


def _scrub_grok_notice_user_texts(obj: Any, replacement_text: str, depth: int = 0) -> int:
    """Replace prior BLOCK-notice / 동의 user slots so Grok does not treat them as injection."""
    if depth > 8 or obj is None:
        return 0
    text = (replacement_text or "").strip()
    changed = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _GROK_REWRITE_SKIP_KEYS:
                continue
            if isinstance(value, str) and _is_grok_notice_or_consent_text(value):
                if value != text:
                    obj[key] = text
                    changed += 1
                continue
            if (
                key == "text"
                and isinstance(value, dict)
                and isinstance(value.get("text"), str)
                and _is_grok_notice_or_consent_text(value.get("text") or "")
            ):
                if value["text"] != text:
                    value["text"] = text
                    changed += 1
                continue
            if isinstance(value, (dict, list)):
                changed += _scrub_grok_notice_user_texts(value, text, depth + 1)
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            if isinstance(value, str) and _is_grok_notice_or_consent_text(value):
                if value != text:
                    obj[idx] = text
                    changed += 1
            elif isinstance(value, (dict, list)):
                changed += _scrub_grok_notice_user_texts(value, text, depth + 1)
    return changed


def _original_message_text(obj: Dict[str, Any]) -> str:
    for key in ("message", "prompt", "input", "query", "text"):
        value = obj.get(key)
        if isinstance(value, str):
            return value
    return ""


def _apply_grok_prompt_rewrite(obj: Dict[str, Any], replacement_text: str) -> int:
    original = _original_message_text(obj)
    text = unwrap_masked_direct_upload_prompt(replacement_text, original)
    _, original_du = parse_direct_upload_message(original)
    if original_du:
        text = text or ""
    elif _is_grok_direct_upload_request(obj):
        return 0

    changed = 0
    for key in ("message", "prompt", "input", "query", "text"):
        if isinstance(obj.get(key), str):
            if obj[key] != text:
                obj[key] = text
                changed += 1
            break

    # WS /ws/mgw: rewrite event.item content / input_chunks text.
    event = obj.get("event") if isinstance(obj.get("event"), dict) else None
    item = None
    if isinstance(event, dict) and isinstance(event.get("item"), dict):
        item = event["item"]
    elif isinstance(obj.get("item"), dict):
        item = obj["item"]
    if isinstance(item, dict):
        content = item.get("content")
        if isinstance(content, list):
            for row in content:
                if not isinstance(row, dict):
                    continue
                if str(row.get("type") or "") in ("input_text", "text", "output_text"):
                    if isinstance(row.get("text"), str) and row["text"] != text:
                        row["text"] = text
                        changed += 1
        xg = item.get("x_grok") if isinstance(item.get("x_grok"), dict) else None
        if isinstance(xg, dict):
            chunks = xg.get("input_chunks")
            if isinstance(chunks, list):
                for ch in chunks:
                    if not isinstance(ch, dict):
                        continue
                    text_obj = ch.get("text")
                    if isinstance(text_obj, dict) and isinstance(text_obj.get("text"), str):
                        if text_obj["text"] != text:
                            text_obj["text"] = text
                            changed += 1
                    elif isinstance(text_obj, str) and text_obj != text:
                        ch["text"] = text
                        changed += 1

    if changed <= 0 and text and not isinstance(item, dict):
        obj["message"] = text
        changed += 1
    changed += _scrub_grok_notice_user_texts(obj, text)
    return changed


def _chat_has_attachments(obj: Dict[str, Any]) -> bool:
    if _collect_attachment_refs(obj):
        return True
    if isinstance(obj.get("directUpload"), dict):
        return True
    message = obj.get("message")
    if isinstance(message, str):
        _, du = parse_direct_upload_message(message)
        if du is not None:
            return True
    return False


def _rewrite_grok_payload(
    obj: Dict[str, Any],
    replacement_text: str,
) -> Optional[Tuple[bytes, int]]:
    had_attachments = _chat_has_attachments(obj)
    changed = _apply_grok_prompt_rewrite(obj, replacement_text)
    strip_cnt = strip_chat_attachments(obj)
    if changed <= 0 and strip_cnt <= 0 and not had_attachments:
        return None
    if changed <= 0 and (strip_cnt > 0 or had_attachments):
        changed = 1
    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, changed


_GROK_TOP_FILE_KEYS = (
    "fileAttachments",
    "imageAttachments",
    "attachments",
    "file_attachment_ids",
    "fileUris",
    "fileAttachmentsMetadata",
)
_GROK_ITEM_FILE_KEYS = ("file_attachment_ids", "fileAttachments", "fileUris")


def _consent_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _grok_consent_item(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    event = obj.get("event") if isinstance(obj.get("event"), dict) else None
    if isinstance(event, dict) and isinstance(event.get("item"), dict):
        return event["item"]
    if isinstance(obj.get("item"), dict):
        return obj["item"]
    return None


def _is_file_mention_chunk(chunk: Any) -> bool:
    if not isinstance(chunk, dict):
        return False
    mention = chunk.get("mention") if isinstance(chunk.get("mention"), dict) else {}
    fm = mention.get("file_mention") if isinstance(mention.get("file_mention"), dict) else {}
    return bool(fm.get("file_id") or fm.get("fileId"))


def consent_attachment_state(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    """File refs of the blocked turn. The upload itself already reached Grok."""
    if (req.get("direction") or "").lower() != "request":
        return None
    obj, _ = _request_obj_for_rewrite(req, payload)
    if not isinstance(obj, dict):
        return None
    if _is_grok_direct_upload_request(obj) or _is_grok_upload_path(req):
        return None
    top = {
        key: _consent_copy(obj[key])
        for key in _GROK_TOP_FILE_KEYS
        if isinstance(obj.get(key), list) and obj[key]
    }
    direct_upload = (
        _consent_copy(obj["directUpload"]) if isinstance(obj.get("directUpload"), dict) else None
    )
    item_refs: Dict[str, Any] = {}
    chunks: List[Any] = []
    item = _grok_consent_item(obj)
    if isinstance(item, dict):
        item_refs = {
            key: _consent_copy(item[key])
            for key in _GROK_ITEM_FILE_KEYS
            if isinstance(item.get(key), list) and item[key]
        }
        xg = item.get("x_grok") if isinstance(item.get("x_grok"), dict) else {}
        raw_chunks = xg.get("input_chunks") if isinstance(xg.get("input_chunks"), list) else []
        chunks = [_consent_copy(ch) for ch in raw_chunks if _is_file_mention_chunk(ch)]
    if not top and direct_upload is None and not item_refs and not chunks:
        return None
    return {
        "top": top,
        "direct_upload": direct_upload,
        "item": item_refs,
        "chunks": chunks,
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
    obj, needs_ws = _request_obj_for_rewrite(req, payload)
    if not isinstance(obj, dict):
        return None
    restored = 0
    for key, value in (state.get("top") or {}).items():
        obj[key] = _consent_copy(value)
        restored += 1
    direct_upload = state.get("direct_upload")
    if isinstance(direct_upload, dict):
        obj["directUpload"] = _consent_copy(direct_upload)
        restored += 1
    item_refs = state.get("item") or {}
    chunks = state.get("chunks") or []
    if item_refs or chunks:
        item = _grok_consent_item(obj)
        if isinstance(item, dict):
            for key, value in item_refs.items():
                item[key] = _consent_copy(value)
                restored += 1
            if chunks:
                xg = item.get("x_grok")
                if not isinstance(xg, dict):
                    xg = {}
                    item["x_grok"] = xg
                existing = xg.get("input_chunks")
                kept = [
                    ch
                    for ch in (existing if isinstance(existing, list) else [])
                    if not _is_file_mention_chunk(ch)
                ]
                xg["input_chunks"] = [_consent_copy(ch) for ch in chunks] + kept
                restored += 1
    if restored <= 0:
        return None
    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _wrap_rewrite_payload(encoded, needs_ws_rewrap=needs_ws)


def rewrite_prompt_for_consent(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    """Consent: original prompt in 동의 / prior BLOCK-notice slots. Never keep the notice."""
    del source_prompt
    obj, needs_ws = _request_obj_for_rewrite(req, payload)
    if obj is None:
        return None
    if _is_grok_direct_upload_request(obj) or _is_grok_upload_path(req):
        return None
    text = (replacement_text or "").strip()
    if not text and not _chat_has_attachments(obj):
        return None
    out = _rewrite_grok_payload(obj, text)
    if out is None:
        return None
    encoded, _ = out
    return (
        _wrap_rewrite_payload(encoded, needs_ws_rewrap=needs_ws),
        "adapter:grok_policy_consent",
    )


def rewrite_prompt_for_block(
    req: Dict[str, Any],
    payload: bytes,
    user_prompt: str,
    system_instruction: str,
) -> Optional[Tuple[bytes, str]]:
    """BLOCK 전용: Grok UI는 리라이트된 사용자 칸을 그대로 보여 주므로 차단 안내를 넣는다."""
    del system_instruction
    obj, needs_ws = _request_obj_for_rewrite(req, payload)
    if obj is None:
        return None
    if _is_grok_direct_upload_request(obj) or _is_grok_upload_path(req):
        return None

    combined = (user_prompt or "").strip() or _GROK_MASK_TOKEN
    combined = unwrap_masked_direct_upload_prompt(combined, _original_message_text(obj))
    if not combined and not _chat_has_attachments(obj):
        return None
    if not combined:
        combined = (user_prompt or "").strip() or "blocked by python ai policy engine"

    out = _rewrite_grok_payload(obj, combined)
    if out is None:
        return None
    encoded, _ = out
    return (
        _wrap_rewrite_payload(encoded, needs_ws_rewrap=needs_ws),
        "adapter:grok_policy_block_sysinstruct",
    )


def rewrite_prompt(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    policy_code: str,
) -> Optional[Tuple[bytes, str]]:
    obj, needs_ws = _request_obj_for_rewrite(req, payload)
    if obj is None:
        return None
    if _is_grok_direct_upload_request(obj) or _is_grok_upload_path(req):
        return None
    if not replacement_text and not _chat_has_attachments(obj):
        return None

    out = _rewrite_grok_payload(obj, replacement_text or "")
    if out is None:
        return None
    encoded, _ = out
    code = (policy_code or "masking").strip().lower()
    return (
        _wrap_rewrite_payload(encoded, needs_ws_rewrap=needs_ws),
        f"adapter:grok_policy_{code}",
    )


def rewrite_prompt_masking(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    """MASKING 전용: message/prompt 치환 + fileAttachments strip."""
    del source_prompt
    obj, needs_ws = _request_obj_for_rewrite(req, payload)
    if obj is None:
        return None
    if _is_grok_direct_upload_request(obj) or _is_grok_upload_path(req):
        return None
    if not replacement_text and not _chat_has_attachments(obj):
        return None
    out = _rewrite_grok_payload(obj, replacement_text or "")
    if out is None:
        return None
    encoded, _ = out
    return (
        _wrap_rewrite_payload(encoded, needs_ws_rewrap=needs_ws),
        "adapter:grok_policy_masking",
    )


def extract_session_id(req: Dict[str, Any], payload: bytes) -> str:
    conversation_id = _extract_conversation_id(req)
    if conversation_id:
        return conversation_id

    output_session_id = str(req.get("_grok_output_session_id") or "")
    if output_session_id:
        return output_session_id

    if _is_ws_unit(req):
        for doc in _ws_json_objects(payload or b""):
            cid = _extract_conversation_id_from_obj(doc)
            if cid:
                return cid

    if _is_json_request(req):
        obj = _parse_request_json(req, payload)
        if isinstance(obj, dict):
            value = obj.get("conversationId") or obj.get("session_id")
            if isinstance(value, str) and value:
                return value
            cid = _extract_conversation_id_from_obj(obj)
            if cid:
                return cid
    return ""


def response_stream_done(req: Dict[str, Any], payload: bytes) -> bool:
    if not _is_json_response(req):
        return False
    # Grok response is delivered as chunked JSON objects but inspected as a complete json_body.
    return True


def _rewrite_grok_response_doc(
    doc: Dict[str, Any], replacement_text: str, emitted: List[bool]
) -> int:
    changed = 0
    event = doc.get("event") if isinstance(doc.get("event"), dict) else None
    et = str((event or {}).get("type") or doc.get("type") or "")
    src = event if isinstance(event, dict) else doc

    def take(current: str) -> str:
        if not current:
            return current
        if emitted[0]:
            return ""
        emitted[0] = True
        return replacement_text

    def put_str(holder: Dict[str, Any], key: str, current: str) -> None:
        nonlocal changed
        if not isinstance(current, str) or not current:
            return
        new = take(current)
        if new != current:
            holder[key] = new
            changed += 1

    def blank_str(holder: Dict[str, Any], key: str, current: str) -> None:
        nonlocal changed
        if not isinstance(current, str) or not current:
            return
        holder[key] = ""
        changed += 1

    # New assistant turn on the same WS connection. Previous emit flag must not
    # empty this turn's tokens (conversation_id and session_id often differ).
    if et in (
        "response.created",
        "response.in_progress",
        "conversation.item.added",
    ):
        emitted[0] = False
        return 0

    if et == "response.chunk":
        chunk = src.get("chunk") if isinstance(src.get("chunk"), dict) else None
        if isinstance(chunk, dict):
            text_obj = chunk.get("text")
            if isinstance(text_obj, dict) and isinstance(text_obj.get("text"), str):
                channel = str(text_obj.get("channel") or "")
                if channel in ("CHANNEL_ASSISTANT_RESPONSE", "", "CHANNEL_ASSISTANT"):
                    put_str(text_obj, "text", text_obj["text"])
                elif (
                    text_obj["text"]
                    and "PROGRESS" not in channel
                    and channel != "CHANNEL_ASSISTANT_NOTETAKER_HEADER"
                ):
                    text_obj["text"] = ""
                    changed += 1
            elif isinstance(text_obj, str) and text_obj:
                put_str(chunk, "text", text_obj)
        return changed

    if et in (
        "response.output_text.delta",
        "response.text.delta",
        "response.content_part.added",
        "response.content_part.done",
    ):
        part = src.get("part") if isinstance(src.get("part"), dict) else None
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
            put_str(part, "text", part["text"])
        delta = src.get("delta")
        if isinstance(delta, str) and delta:
            put_str(src, "delta", delta)
        elif isinstance(delta, dict) and isinstance(delta.get("text"), str) and delta["text"]:
            put_str(delta, "text", delta["text"])
        return changed

    if et in ("response.output_text.done", "response.text.done"):
        text = src.get("text")
        if isinstance(text, str) and text.strip():
            src["text"] = replacement_text
            emitted[0] = True
            changed += 1
        return changed

    if et in ("response.output_item.added", "response.output_item.done"):
        item = src.get("item") if isinstance(src.get("item"), dict) else None
        if isinstance(item, dict):
            content = item.get("content")
            if isinstance(content, list):
                for row in content:
                    if not isinstance(row, dict):
                        continue
                    if isinstance(row.get("text"), str) and row["text"]:
                        row["text"] = replacement_text
                        emitted[0] = True
                        changed += 1
            elif isinstance(item.get("text"), str) and item["text"]:
                item["text"] = replacement_text
                emitted[0] = True
                changed += 1
        return changed

    if et in ("response.completed", "response.done", "response.persisted"):
        resp = src.get("response") if isinstance(src.get("response"), dict) else None
        if isinstance(resp, dict):
            output = resp.get("output")
            if isinstance(output, list):
                for row in output:
                    if not isinstance(row, dict):
                        continue
                    content = row.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                                part["text"] = replacement_text
                                emitted[0] = True
                                changed += 1
                    elif isinstance(row.get("text"), str) and row["text"]:
                        row["text"] = replacement_text
                        emitted[0] = True
                        changed += 1
        if changed:
            return changed

    result = doc.get("result")
    if isinstance(result, dict):
        resp = result.get("response") if isinstance(result.get("response"), dict) else result
        token = resp.get("token")
        if isinstance(token, str) and token and not bool(resp.get("isThinking")):
            put_str(resp, "token", token)
        model_resp = resp.get("modelResponse")
        if isinstance(model_resp, dict):
            message = model_resp.get("message")
            if isinstance(message, str) and message and message != replacement_text:
                model_resp["message"] = replacement_text
                emitted[0] = True
                changed += 1
        if changed:
            return changed

    if changed <= 0:
        changed += _rewrite_grok_response_text_fallback(src, replacement_text, emitted)
        if changed <= 0 and src is not doc:
            changed += _rewrite_grok_response_text_fallback(doc, replacement_text, emitted)
    return changed


def _rewrite_grok_response_text_fallback(
    node: Dict[str, Any], replacement_text: str, emitted: List[bool]
) -> int:
    """Blank unknown assistant strings. The visible notice belongs on CHANNEL_ASSISTANT_RESPONSE."""
    del replacement_text, emitted
    changed = 0

    def take(current: str) -> str:
        if not current:
            return current
        return ""

    def walk(obj: Any, depth: int) -> None:
        nonlocal changed
        if changed or depth > 6 or obj is None:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                if changed:
                    return
                if key in _GROK_REWRITE_SKIP_KEYS:
                    continue
                if key in (
                    "text",
                    "delta",
                    "token",
                    "message",
                    "content",
                    "output_text",
                    "value",
                ) and isinstance(value, str):
                    raw = value.strip()
                    if len(raw) < 2 or _GROK_UUIDISH_RE.match(raw):
                        continue
                    new = take(value)
                    if new != value:
                        obj[key] = new
                        changed += 1
                        return
                elif isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)
                if changed:
                    return

    walk(node, 0)
    return changed


_GROK_CONTENT_EVENT_TYPES = frozenset(
    {
        "response.chunk",
        "response.output_text.delta",
        "response.text.delta",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.done",
        "response.text.done",
        "response.output_item.added",
        "response.output_item.done",
    }
)


def _grok_response_id_from_doc(doc: Dict[str, Any]) -> str:
    event = doc.get("event") if isinstance(doc.get("event"), dict) else None
    src = event if isinstance(event, dict) else doc
    rid = src.get("response_id")
    if isinstance(rid, str) and rid.strip():
        return rid.strip()
    resp = src.get("response") if isinstance(src.get("response"), dict) else None
    if isinstance(resp, dict):
        rid2 = resp.get("id") or resp.get("response_id") or resp.get("responseId")
        if isinstance(rid2, str) and rid2.strip():
            return rid2.strip()
    rid3 = doc.get("response_id")
    if isinstance(rid3, str) and rid3.strip():
        return rid3.strip()
    return ""


def _grok_synth_notice_frame(
    session_id: str, notice: str, response_id: str = ""
) -> bytes:
    event: Dict[str, Any] = {
        "type": "response.chunk",
        "chunk": {
            "text": {
                "text": notice,
                "channel": "CHANNEL_ASSISTANT_RESPONSE",
            }
        },
    }
    if response_id:
        event["response_id"] = response_id
    body: Dict[str, Any] = {"session_id": session_id, "event": event}
    if response_id:
        body["response_id"] = response_id
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _encode_ws_text_frame(encoded, masked=False)


_GROK_LIFECYCLE_EVENT_TYPES = frozenset(
    {
        "response.created",
        "response.in_progress",
        "conversation.item.added",
        "session.updated",
        "session.created",
    }
)


def rewrite_response(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
) -> Optional[Tuple[bytes, str]]:
    if not replacement_text:
        return None
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return None

    session_id = (
        extract_session_id(req, payload)
        or str(req.get("request_id") or "")
    ).strip()
    emitted = [bool(_GROK_RESPONSE_OVERRIDE_EMITTED.get(session_id))]
    response_id = str(_GROK_RESPONSE_OVERRIDE_RID.get(session_id) or "")
    is_ws = _is_ws_unit(req)

    raw_docs: List[bytes]
    stripped = (payload or b"").lstrip()
    if stripped[:1] in (b"{", b"["):
        raw_docs = [payload] if payload else []
    elif is_ws:
        frames = _parse_ws_payloads(payload)
        raw_docs = frames if frames else ([payload] if payload else [])
    elif _is_json_response(req):
        raw_docs = [payload]
    else:
        return None

    def take_notice_frame(text: str) -> bytes:
        emitted[0] = True
        return _grok_synth_notice_frame(session_id, text, response_id)

    empty_ws = _encode_ws_text_frame(b"", masked=False)
    out_parts: List[bytes] = []
    total = 0
    for raw in raw_docs:
        docs, seps = _decode_response_docs_with_separators(raw)
        if not docs:
            docs = _decode_response_docs(raw)
            seps = None
        if not docs:
            if is_ws and len(raw) > 120:
                if not emitted[0]:
                    out_parts.append(take_notice_frame(replacement_text))
                else:
                    out_parts.append(empty_ws)
                total += 1
                continue
            if is_ws:
                out_parts.append(_encode_ws_text_frame(raw, masked=False))
                total += 1
            else:
                return None
            continue
        changed = 0
        event_types: List[str] = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            rid = _grok_response_id_from_doc(doc)
            if rid:
                response_id = rid
            event = doc.get("event") if isinstance(doc.get("event"), dict) else None
            event_types.append(str((event or {}).get("type") or doc.get("type") or ""))
            changed += _rewrite_grok_response_doc(doc, replacement_text, emitted)
        encoded = _encode_response_docs(docs, seps)
        contentish = any(et in _GROK_CONTENT_EVENT_TYPES for et in event_types)
        lifecycle_only = bool(event_types) and all(
            et in _GROK_LIFECYCLE_EVENT_TYPES for et in event_types
        )
        if changed > 0:
            total += changed
            if is_ws:
                out_parts.append(_encode_ws_text_frame(encoded, masked=False))
            else:
                out_parts.append(encoded)
            if not emitted[0] and contentish:
                out_parts.append(take_notice_frame(replacement_text))
                total += 1
            continue
        if contentish:
            if not emitted[0]:
                if is_ws:
                    out_parts.append(take_notice_frame(replacement_text))
                else:
                    out_parts.append(encoded)
                    emitted[0] = True
                total += 1
            elif is_ws:
                out_parts.append(empty_ws)
                total += 1
            continue
        if is_ws:
            out_parts.append(_encode_ws_text_frame(encoded, masked=False))
        else:
            out_parts.append(encoded)
        if lifecycle_only or encoded:
            total += 1

    # Lifecycle-only units must pass through. Injecting a chunk before
    # response.created makes Grok UI drop the notice, then later tokens are emptied.
    if total <= 0 and is_ws and not emitted[0] and not out_parts:
        out_parts.append(take_notice_frame(replacement_text))
        total += 1

    if session_id:
        _GROK_RESPONSE_OVERRIDE_EMITTED[session_id] = emitted[0]
        if response_id:
            _GROK_RESPONSE_OVERRIDE_RID[session_id] = response_id
    if total <= 0:
        return None
    payload_out = b"".join(out_parts)
    if not payload_out and is_ws:
        payload_out = empty_ws
    return payload_out, "adapter:grok_policy_block_message"


def clear_response_override_state(session_id: str) -> None:
    key = (session_id or "").strip()
    if key:
        _GROK_RESPONSE_OVERRIDE_EMITTED.pop(key, None)
        _GROK_RESPONSE_OVERRIDE_RID.pop(key, None)
