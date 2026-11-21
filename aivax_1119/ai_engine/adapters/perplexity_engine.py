"""
Perplexity Adapter
Handles Perplexity request JSON and response SSE events.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import unquote

try:
    import account_lookup as _account_lookup
except ImportError:
    _account_lookup = None

from ai_service_codes import AI_SERVICE_PERPLEXITY
from adapters.perplexity_upload import (
    POLICY_ATTACHMENTS_FINAL,
    build_policy_attachments,
    normalize_upload,
    strip_chat_attachments,
    _collect_attachment_refs,
)

logger = logging.getLogger(__name__)


_PPLX_RESPONSE_BUFFER: Dict[str, str] = {}
_PPLX_RESPONSE_MESSAGE_ID: Dict[str, str] = {}
_PPLX_RESPONSE_OVERRIDE_EMITTED: Dict[str, bool] = {}
_PPLX_FALLBACK_KEY = "__fallback__"


def _is_auth_session_response(req: Dict[str, Any]) -> bool:
    """True for GET .../api/auth/session response. Do not run Input/Output filter."""
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    method = str(meta.get("method") or "").upper()
    path = str(meta.get("path") or "")
    if "perplexity.ai" not in host or method != "GET":
        return False
    base_path = path.split("?", 1)[0]
    return base_path.rstrip("/") == "/api/auth/session"


def _pplx_path_base(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("path") or "").split("?", 1)[0]


def is_perplexity_ask_path(path: str) -> bool:
    """True for real chat-turn POSTs only (deny-by-default allowlist).

    Allow:
      /rest/sse/perplexity_ask  (and trailing-segment perplexity_ask)
    Deny:
      /rest/event/analytics, /rest/thread/mark_viewed, /ces/*, telemetry,
      /rest/perplexity_ask/graphql (Relay sidebar; no query_str).
    """
    path_lower = (path or "").split("?", 1)[0].lower().rstrip("/")
    if not path_lower:
        return False
    if path_lower.endswith("/graphql") or "/graphql/" in path_lower:
        return False
    return path_lower.endswith("perplexity_ask") or "/sse/perplexity_ask" in path_lower


def is_perplexity_chat_turn_path(req: Dict[str, Any]) -> bool:
    return is_perplexity_ask_path(_pplx_path_base(req))


def perplexity_should_skip_policy(req: Dict[str, Any], payload: bytes = b"") -> bool:
    """Deny-by-default: only evaluate allowlisted ask turns.

    Deep INLINE tags every perplexity.ai host as service=perplexity. Without this
    gate, /rest/event/analytics and UI telemetry become 사용자 프롬프트 garbage.
    """
    del payload  # unused; signature matches other *_should_skip_policy adapters
    if (req.get("direction") or "").lower() != "request":
        return False
    return not is_perplexity_chat_turn_path(req)


# Per-user browser correlator (distinct per visitor; not shared across users like JWE alg header).
_PPLX_VISITOR_ID_COOKIE = "pplx.visitor-id"


def _extract_named_cookie(cookie_str: str, name: str) -> Optional[str]:
    if not cookie_str or not isinstance(cookie_str, str) or not name:
        return None
    cookie_str = cookie_str.strip()
    m = re.search(r"(?:^|;\s*)" + re.escape(name) + r"=([^;\s]+)", cookie_str)
    if m:
        return unquote(m.group(1).strip())
    return None


def _extract_visitor_id_from_cookie(cookie_str: str) -> str:
    return str(_extract_named_cookie(cookie_str, _PPLX_VISITOR_ID_COOKIE) or "").strip()


def _extract_session_keys_from_cookie(cookie_str: str) -> List[str]:
    visitor_id = _extract_visitor_id_from_cookie(cookie_str)
    return [visitor_id] if visitor_id else []


def _pplx_person_name(value: Any) -> str:
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


def _pplx_display_name(user: Dict[str, Any]) -> str:
    for key in ("name", "username"):
        name = _pplx_person_name(user.get(key))
        if name:
            return name
    return ""


def _register_perplexity_account(
    session_keys: List[str], user_id: str, email: str, display_name: str = ""
) -> None:
    if not _account_lookup or not (user_id or email):
        return
    uid = user_id or ""
    em = email or ""
    name = _pplx_person_name(display_name)
    keys = list(session_keys or [])
    if uid and uid not in keys:
        keys.append(uid)
    for key in keys:
        if key:
            _account_lookup.set_account("perplexity", key, uid, em, display_name=name)


def _pplx_cached_name(meta: Dict[str, Any], hint_user_id: str = "") -> str:
    if not _account_lookup or not hasattr(_account_lookup, "get_result"):
        return ""
    keys = _session_keys_from_meta(meta)
    hint = (hint_user_id or "").strip()
    if hint and hint not in keys:
        keys.append(hint)
    for key in keys:
        try:
            info = _account_lookup.get_result("perplexity", key)
        except Exception:
            info = None
        raw = getattr(info, "display_name", "") if info else ""
        if not isinstance(raw, str):
            continue
        name = _pplx_person_name(raw)
        if name:
            return name
    return ""


def _session_keys_from_meta(meta: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    seen: set[str] = set()

    def push(value: str) -> None:
        token = str(value or "").strip()
        if token and token not in seen:
            seen.add(token)
            keys.append(token)

    for key in _extract_session_keys_from_cookie(str(meta.get("cookie") or "").strip()):
        push(key)

    push(str(meta.get("pplx_visitor_id") or ""))
    # Some Deep INLINE tees omit cookie parsing helpers; header may also carry visitor id.
    push(str(meta.get("pplx-visitor-id") or ""))

    return keys


def _lookup_perplexity_account(
    meta: Dict[str, Any],
    hint_user_id: str = "",
) -> Optional[Tuple[str, str]]:
    if not _account_lookup:
        return None
    for key in _session_keys_from_meta(meta):
        info = _account_lookup.get("perplexity", key)
        if info:
            return info
    hint = (hint_user_id or "").strip()
    if hint:
        info = _account_lookup.get("perplexity", hint)
        if info:
            return info
    return None


def _log_auth_session(req: Dict[str, Any], obj: Dict[str, Any]) -> None:
    """Extract user id/email from GET /api/auth/session JSON and register for later filter lookups."""
    user = obj.get("user")
    if not isinstance(user, dict):
        return
    user_id = str(user.get("id") or "").strip()
    email = str(user.get("email") or "").strip()
    name = _pplx_display_name(user)
    req["_pplx_session_user_id"] = user_id
    req["_pplx_session_email"] = email
    req["_pplx_session_name"] = name

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    session_keys = _session_keys_from_meta(meta)

    logger.info(
        "[Account Info] perplexity: auth/session user_id=%s email=%s name=%s visitor_id=%s path=%s",
        user_id or "-",
        email or "-",
        name or "-",
        session_keys[0] if session_keys else "-",
        str(meta.get("path") or ""),
    )
    _register_perplexity_account(session_keys, user_id, email, name)


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


def _is_sse_response(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "response":
        return False
    return unit_type == "sse_event" or "text/event-stream" in content_type


def _parse_request_json(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    if not _is_json_request(req):
        return None
    text = _safe_utf8(payload)
    try:
        obj = json.loads(text)
    except Exception:
        _, data_text = _parse_sse_payload(text)
        if not data_text or data_text == "[DONE]":
            return None
        try:
            obj = json.loads(data_text)
        except Exception:
            return None
    if not isinstance(obj, dict):
        return None
    return obj


def _extract_prompt(obj: Dict[str, Any]) -> str:
    for key in ("query_str", "query", "message", "prompt", "input", "text"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    params = obj.get("params")
    if isinstance(params, dict):
        for key in ("query_str", "query", "dsl_query", "message", "prompt", "input", "text"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                return value
    # Rare nested shapes: queries[0].query / messages[-1].content
    queries = obj.get("queries")
    if isinstance(queries, list):
        for q in queries:
            if isinstance(q, dict):
                for key in ("query", "query_str", "text"):
                    value = q.get(key)
                    if isinstance(value, str) and value.strip():
                        return value
    return ""


_CONSENT_SESSION_KEYS = (
    "backend_uuid",
    "backendUuid",
    "last_backend_uuid",
    "lastBackendUuid",
    "thread_url_slug",
    "threadUrlSlug",
    "frontend_context_uuid",
    "frontendContextUuid",
    "context_uuid",
    "contextUuid",
    "read_write_token",
    "readWriteToken",
    "parent_backend_uuid",
    "parentBackendUuid",
)
_CONSENT_KEY_HINTS = (
    "backend",
    "thread",
    "context",
    "parent",
    "conversation",
    "last_",
    "read_write",
)
_CONSENT_SKIP_KEYS = {
    "frontend_uuid",
    "frontenduuid",
    "visitor_id",
    "visitorid",
    "user_id",
    "userid",
    "email",
}
# visitor_id -> thread ids seen on recent ask/SSE (follow-up asks omit backend_uuid).
_PPLX_VISITOR_THREAD_IDS: Dict[str, List[str]] = {}


def _remember_visitor_thread_ids(visitor_id: str, ids: List[str]) -> None:
    vid = str(visitor_id or "").strip()
    if not vid:
        return
    merged: List[str] = []
    seen: set[str] = set()
    for item in list(ids or []) + list(_PPLX_VISITOR_THREAD_IDS.get(vid) or []):
        text = str(item or "").strip()
        if not text or text == vid or text in seen:
            continue
        seen.add(text)
        merged.append(text)
        if len(merged) >= 16:
            break
    _PPLX_VISITOR_THREAD_IDS[vid] = merged


def _walk_consent_session_ids(node: Any, add, depth: int = 0) -> None:
    if depth > 6 or node is None:
        return
    if isinstance(node, str):
        raw = node.strip()
        if raw.startswith("{") or raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
            if parsed is not None:
                _walk_consent_session_ids(parsed, add, depth + 1)
        return
    if isinstance(node, list):
        for item in node[:32]:
            _walk_consent_session_ids(item, add, depth + 1)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        key_l = str(key or "").strip()
        key_norm = key_l.lower().replace("-", "_")
        if key_norm in _CONSENT_SKIP_KEYS:
            continue
        if key_l in _CONSENT_SESSION_KEYS or any(h in key_norm for h in _CONSENT_KEY_HINTS):
            if isinstance(value, str) and value.strip():
                add(value)
        _walk_consent_session_ids(value, add, depth + 1)


def collect_consent_session_ids(req: Dict[str, Any], payload: bytes) -> List[str]:
    """Thread ids that must share one consent pending slot.

    Follow-up asks mint a new frontend_context_uuid and often omit backend_uuid.
    Walk nested JSON, cookie visitor-id, and recent SSE thread ids for this visitor.
    """
    ids: List[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            ids.append(text)

    obj: Optional[Dict[str, Any]] = None
    if _is_json_request(req):
        obj = _parse_request_json(req, payload)
    elif _is_sse_response(req):
        parsed = _parse_sse_data_json(_safe_utf8(payload))
        obj = parsed if isinstance(parsed, dict) else None
    else:
        obj = _parse_request_json(req, payload)
        if obj is None:
            parsed = _parse_sse_data_json(_safe_utf8(payload))
            obj = parsed if isinstance(parsed, dict) else None
    if isinstance(obj, dict):
        _walk_consent_session_ids(obj, _add)

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    visitor = (
        str(meta.get("pplx_visitor_id") or "").strip()
        or _extract_visitor_id_from_cookie(str(meta.get("cookie") or ""))
    )
    if visitor:
        _add(visitor)
        for sid in _PPLX_VISITOR_THREAD_IDS.get(visitor) or []:
            _add(sid)
        _remember_visitor_thread_ids(visitor, ids)

    params = obj.get("params") if isinstance(obj, dict) else None
    logger.info(
        "[Perplexity] consent_session_ids=%s keys=%s param_type=%s param_keys=%s visitor=%s",
        ids[:10],
        list(obj.keys())[:24] if isinstance(obj, dict) else [],
        type(params).__name__,
        list(params.keys())[:24] if isinstance(params, dict) else "-",
        (visitor[:36] if visitor else "-"),
    )
    return ids


def _extract_request_session_id(obj: Dict[str, Any]) -> str:
    for key in (
        "frontend_context_uuid",
        "frontendContextUuid",
        "context_uuid",
        "contextUuid",
        "backend_uuid",
        "backendUuid",
        "thread_url_slug",
        "threadUrlSlug",
        "uuid",
        "conversation_id",
        "conversationId",
        "session_id",
        "sessionId",
    ):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    params = obj.get("params")
    if isinstance(params, dict):
        for key in (
            "frontend_context_uuid",
            "frontendContextUuid",
            "context_uuid",
            "last_backend_uuid",
            "backend_uuid",
            "thread_url_slug",
        ):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    user = obj.get("user")
    if isinstance(user, dict):
        for uk in ("uuid", "id", "session_id", "sessionId"):
            v = user.get(uk)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _extract_request_message_id(obj: Dict[str, Any]) -> str:
    for key in (
        "frontend_uuid",
        "frontendUuid",
        "uuid",
        "message_id",
        "messageId",
        "parent_message_id",
        "parentMessageId",
    ):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    params = obj.get("params")
    if isinstance(params, dict):
        for key in ("frontend_uuid", "frontendUuid", "uuid", "message_id"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


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


def _extract_response_session_id_obj(obj: Dict[str, Any]) -> str:
    # Prefer frontend_context_uuid so BLOCK override / UI merge match the request side.
    for key in (
        "frontend_context_uuid",
        "frontendContextUuid",
        "context_uuid",
        "contextUuid",
        "thread_url_slug",
        "threadUrlSlug",
        "backend_uuid",
        "backendUuid",
        "uuid",
    ):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_response_message_id_obj(obj: Dict[str, Any]) -> str:
    for key in ("frontend_uuid", "frontendUuid", "uuid", "message_id", "messageId"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _resolve_stream_key(req: Dict[str, Any], obj: Optional[Dict[str, Any]] = None) -> str:
    if isinstance(obj, dict):
        session_id = _extract_response_session_id_obj(obj)
        if session_id:
            return session_id
        message_id = _extract_response_message_id_obj(obj)
        if message_id:
            return message_id

    req_key = str(req.get("_pplx_stream_key") or "")
    if req_key:
        return req_key

    if len(_PPLX_RESPONSE_BUFFER) == 1:
        return next(iter(_PPLX_RESPONSE_BUFFER))
    if len(_PPLX_RESPONSE_MESSAGE_ID) == 1:
        return next(iter(_PPLX_RESPONSE_MESSAGE_ID))
    return _PPLX_FALLBACK_KEY


def _set_output_ctx(req: Dict[str, Any], key: str) -> None:
    session_id = key if key != _PPLX_FALLBACK_KEY else ""
    req["_pplx_output_session_id"] = session_id
    req["_pplx_output_message_id"] = str(_PPLX_RESPONSE_MESSAGE_ID.get(key, "") or "")


def _set_extract_final(req: Dict[str, Any], is_final: bool) -> None:
    req["_extract_final"] = bool(is_final)


def _patch_answer_text(value: Any, path: str = "") -> str:
    """Pull assistant text from one JSON-patch value (schematized + legacy)."""
    path = str(path or "")
    if path == "/progress" or path.startswith("/goals"):
        return ""
    # Live web (2026): workflow_block text_payload.chunks / text_payload.text
    is_text_payload = "/text_payload/" in path or path.endswith("/text_payload/text")
    is_chunk = "/chunks/" in path or path.startswith("/chunks/")
    is_answer = (
        path in ("/answer", "", "/markdown_block/answer")
        or path.endswith("/answer")
        or path.endswith("/text")
        or "answer" in path
    )
    if isinstance(value, str) and value.strip():
        if is_text_payload or is_chunk or is_answer:
            return value
        return ""
    if isinstance(value, dict):
        for key in ("answer", "text"):
            ans = value.get(key)
            if isinstance(ans, str) and ans.strip():
                return ans
        v_chunks = value.get("chunks")
        if isinstance(v_chunks, list):
            merged = "".join((c for c in v_chunks if isinstance(c, str)))
            if merged.strip():
                return merged
    return ""


def _extract_diff_patch_answer(block: Dict[str, Any]) -> str:
    """Best (usually cumulative) markdown/workflow answer from diff_block patches."""
    diff = block.get("diff_block")
    if not isinstance(diff, dict):
        return ""
    patches = diff.get("patches")
    if not isinstance(patches, list):
        return ""

    field = str(diff.get("field") or "")
    best = ""
    chunk_parts: List[str] = []
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        path = str(patch.get("path") or "")
        if path == "/progress" or path.startswith("/goals"):
            continue
        value = patch.get("value")
        text = _patch_answer_text(value, path)
        if not text and field in ("markdown_block", "workflow_block"):
            if isinstance(value, str) and value.strip():
                text = value
            elif isinstance(value, dict):
                for key in ("answer", "text"):
                    ans = value.get(key)
                    if isinstance(ans, str) and ans.strip():
                        text = ans
                        break
        if not text:
            continue
        # Prefer full cumulative text_payload.text over chunk concat.
        if path.endswith("/text_payload/text") or path.endswith("/answer"):
            if len(text) > len(best):
                best = text
            continue
        if "/chunks/" in path or path.startswith("/chunks/"):
            chunk_parts.append(text)
            continue
        if len(text) > len(best):
            best = text
    if best:
        return best
    return "".join(chunk_parts)


def _extract_diff_patch_chunks(block: Dict[str, Any]) -> str:
    """Incremental chunk text only (for streaming delta concat)."""
    diff = block.get("diff_block")
    if not isinstance(diff, dict):
        return ""
    patches = diff.get("patches")
    if not isinstance(patches, list):
        return ""

    chunks: List[str] = []
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        path = str(patch.get("path") or "")
        value = patch.get("value")
        if ("/chunks/" in path or path.startswith("/chunks/")) and isinstance(value, str):
            chunks.append(value)
            continue
        if isinstance(value, dict):
            v_chunks = value.get("chunks")
            if isinstance(v_chunks, list):
                for c in v_chunks:
                    if isinstance(c, str):
                        chunks.append(c)
    return "".join(chunks)


_ANSWER_WORKFLOW_ITEM_TYPES = frozenset(
    {
        "WORKFLOW_ITEM_TEXT",
        "WORKFLOW_ITEM_MARKDOWN",
        "WORKFLOW_ITEM_ANSWER",
    }
)


def _is_sse_final_event(obj: Dict[str, Any]) -> bool:
    """True for the completed Perplexity SSE envelope (user-visible answer is here)."""
    if bool(obj.get("final_sse_message") or obj.get("final")):
        return True
    if bool(obj.get("text_completed")):
        return True
    status = str(obj.get("status") or "").strip().upper()
    return status in ("COMPLETED", "COMPLETE", "DONE")


def _workflow_item_is_user_answer(item: Dict[str, Any]) -> bool:
    """Structural gate — only `variant=answer` text items are the chat reply.

    Planner/CoT and tool steps also appear as WORKFLOW_ITEM_TEXT sometimes, but
    the user-visible reply is tagged `variant: "answer"` (and usually
    `display_mode: WORKFLOW_ITEM_ALWAYS_EXPAND`) on the final envelope.
    Do not use English/Hangul heuristics — CoT wording changes every turn.
    """
    item_type = str(item.get("type") or "")
    if item_type.startswith("WORKFLOW_ITEM_") and item_type not in _ANSWER_WORKFLOW_ITEM_TYPES:
        return False
    variant = str(item.get("variant") or "").strip().lower()
    return variant == "answer"


def _workflow_item_payload_text(item: Dict[str, Any]) -> str:
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return ""
    text_payload = payload.get("text_payload")
    if not isinstance(text_payload, dict):
        return ""
    text = text_payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    chunks = text_payload.get("chunks")
    if isinstance(chunks, list):
        merged = "".join((c for c in chunks if isinstance(c, str)))
        if merged.strip():
            return merged.strip()
    return ""


def _workflow_answer_from_block(block: Dict[str, Any]) -> str:
    """User-visible answer from workflow_block: variant=answer TEXT items only."""
    workflow = block.get("workflow_block")
    if not isinstance(workflow, dict):
        return ""
    best = ""
    for step in workflow.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for item in step.get("items") or []:
            if not isinstance(item, dict):
                continue
            if not _workflow_item_is_user_answer(item):
                continue
            text = _workflow_item_payload_text(item)
            if text and len(text) >= len(best):
                best = text
    return best


def _markdown_answer_from_block(block: Dict[str, Any], *, allow_diff: bool = False) -> str:
    # Schematized API: only variant=answer workflow text (not planner CoT).
    workflow_answer = _workflow_answer_from_block(block)
    if workflow_answer:
        return workflow_answer

    markdown = block.get("markdown_block")
    if isinstance(markdown, dict):
        answer = markdown.get("answer")
        if isinstance(answer, str) and answer.strip():
            return answer
        chunks = markdown.get("chunks")
        if isinstance(chunks, list):
            merged = "".join((c for c in chunks if isinstance(c, str)))
            if merged.strip():
                return merged
    # Diff patches are incremental and can include non-answer steps — only when
    # the caller already knows this is the final SSE envelope.
    if allow_diff:
        diff_answer = _extract_diff_patch_answer(block)
        if diff_answer:
            return diff_answer
    for nest_key in ("text_block", "response_block", "answer_block", "sources_mode_block"):
        nest = block.get(nest_key)
        if not isinstance(nest, dict):
            continue
        for key in ("answer", "text", "markdown", "content"):
            value = nest.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _extract_stream_delta_text(obj: Dict[str, Any]) -> str:
    blocks = obj.get("blocks")
    if not isinstance(blocks, list):
        return ""

    for usage in (
        "ask_text_0_markdown",
        "ask_text",
        "assistant_text",
        "answer",
        "workflow_root",
    ):
        parts: List[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if str(block.get("intended_usage") or "") != usage:
                continue
            chunk = _extract_diff_patch_chunks(block)
            if chunk:
                parts.append(chunk)
        if parts:
            return "".join(parts)

    # Fallback: incremental /chunks only (never cumulative answers — avoids dupes).
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        chunk = _extract_diff_patch_chunks(block)
        if chunk:
            parts.append(chunk)
    return "".join(parts)


def _extract_final_text(obj: Dict[str, Any], *, final_event: bool = False) -> str:
    """Extract user-visible assistant text from one SSE JSON object.

    Prefer structural `variant=answer` workflow items. Diff/legacy paths are
    allowed only on the final envelope so mid-stream planner text is ignored.
    """
    blocks = obj.get("blocks")
    best = ""
    if isinstance(blocks, list):
        for usage in (
            "ask_text_0_markdown",
            "ask_text",
            "assistant_text",
            "answer",
            "workflow_root",
        ):
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if str(block.get("intended_usage") or "") != usage:
                    continue
                answer = _markdown_answer_from_block(block, allow_diff=final_event)
                if answer and len(answer) >= len(best):
                    best = answer
        if not best:
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                answer = _markdown_answer_from_block(block, allow_diff=final_event)
                if answer and len(answer) >= len(best):
                    best = answer
        if best:
            return best

    # Top-level query echo is the prompt — never treat as assistant output.
    # Only consider on final events, and never short junk.
    if final_event:
        for key in ("answer", "text", "output", "message", "response"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip() and len(value.strip()) > 8:
                if len(value.strip()) > len(best):
                    best = value.strip()
    return best


def _pick_last_nonempty(candidates: List[str]) -> str:
    for text in reversed(candidates):
        if (text or "").strip():
            return text.strip()
    return ""


def _iter_sse_json_objects(text: str) -> List[Dict[str, Any]]:
    """Parse SSE (\\n\\n framed) and also bare data: lines / raw JSON objects."""
    objs: List[Dict[str, Any]] = []
    seen: set[int] = set()

    def push(data_text: str) -> None:
        raw = (data_text or "").strip()
        if not raw or raw == "[DONE]" or raw == "{}":
            return
        try:
            obj = json.loads(raw)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        h = hash(raw)
        if h in seen:
            return
        seen.add(h)
        objs.append(obj)

    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for block in normalized.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        _, data_text = _parse_sse_payload(block)
        push(data_text)

    # Some tees lose blank-line framing; scan every data: line.
    if len(objs) < 2:
        for line in normalized.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                push(line[5:].strip())

    return objs


def _dump_pplx_payload(kind: str, payload: bytes, meta: Dict[str, Any]) -> None:
    try:
        dump_dir = "/home/juyonglee/AIVAX/inline_engine/aivax/evidence/PROVE/I350_BYPASS_1G"
        os.makedirs(dump_dir, exist_ok=True)
        stamp = int(time.time())
        path_hint = str(meta.get("path") or "unknown").replace("/", "_")[:48]
        dump_path = os.path.join(dump_dir, f"perplexity_{kind}_{stamp}{path_hint}.dump")
        with open(dump_path, "wb") as f:
            f.write(payload[:2_000_000] if payload else b"")
        # Stable latest pointer for quick inspection.
        latest = os.path.join(dump_dir, f"perplexity_{kind}_latest.dump")
        with open(latest, "wb") as f:
            f.write(payload[:2_000_000] if payload else b"")
        logger.warning("[Perplexity] wrote %s dump path=%s bytes=%d", kind, dump_path, len(payload or b""))
    except Exception:
        logger.exception("[Perplexity] %s dump failed", kind)


_PPLX_MODEL_REJECT = frozenset(
    {"user", "assistant", "system", "model", "human", "bot", "tool", "none", "null"}
)


def _pplx_model_text(value: Any) -> str:
    """Keep the model string Perplexity sent."""
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or len(name) > 80 or name.lower() in _PPLX_MODEL_REJECT:
        return ""
    if any(ord(ch) < 32 for ch in name):
        return ""
    return name


def _pplx_model_from_obj(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    for source in (params, obj):
        name = _pplx_model_text(source.get("model_preference"))
        if name:
            return name
    return ""


def _pplx_note_model(req: Dict[str, Any], obj: Any) -> str:
    name = _pplx_model_from_obj(obj)
    if not name:
        return ""
    req["_perplexity_model"] = name
    logger.info("perplexity_model name=%s", name)
    return name


def extract_model(req: Dict[str, Any], payload: bytes) -> str:
    """Selected model from params.model_preference, stored as sent."""
    current = str(req.get("_perplexity_model") or "").strip()
    if current:
        return current
    obj = _parse_request_json(req, payload)
    if obj is None:
        obj = _parse_sse_data_json(_safe_utf8(payload or b""))
    name = _pplx_note_model(req, obj)
    return name


def extract(req: Dict[str, Any], payload: bytes) -> Optional[str]:
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
        # Only collect assistant SSE from ask turns (not other event-stream endpoints).
        if not is_perplexity_chat_turn_path(req):
            req["_extract_final"] = False
            return ""
        raw = _safe_utf8(payload)
        _, data_text = _parse_sse_payload(raw)
        if not data_text:
            _set_extract_final(req, False)
            return ""

        if data_text == "[DONE]":
            key = _resolve_stream_key(req)
            merged = _PPLX_RESPONSE_BUFFER.pop(key, "")
            _PPLX_RESPONSE_OVERRIDE_EMITTED.pop(key, None)
            _set_output_ctx(req, key)
            _set_extract_final(req, bool(merged))
            return merged

        # end_of_stream payload can arrive as "{}"
        if data_text.strip() == "{}":
            key = _resolve_stream_key(req)
            merged = _PPLX_RESPONSE_BUFFER.pop(key, "")
            _PPLX_RESPONSE_OVERRIDE_EMITTED.pop(key, None)
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

        _pplx_note_model(req, obj)
        key = _resolve_stream_key(req, obj)
        req["_pplx_stream_key"] = key
        message_id = _extract_response_message_id_obj(obj)
        if message_id:
            _PPLX_RESPONSE_MESSAGE_ID[key] = message_id

        final_text = _extract_final_text(obj)
        if final_text:
            _PPLX_RESPONSE_BUFFER[key] = final_text
        else:
            delta = _extract_stream_delta_text(obj)
            if delta:
                _PPLX_RESPONSE_BUFFER[key] = _PPLX_RESPONSE_BUFFER.get(key, "") + delta

        is_final = bool(obj.get("final_sse_message"))
        if is_final:
            merged = _PPLX_RESPONSE_BUFFER.pop(key, "")
            _PPLX_RESPONSE_OVERRIDE_EMITTED.pop(key, None)
            _set_output_ctx(req, key)
            _set_extract_final(req, bool(merged))
            return merged

        _set_extract_final(req, False)
        return ""

    # Request: deny-by-default — only ask turns extract prompts.
    if not is_perplexity_chat_turn_path(req):
        req["_extract_final"] = False
        return ""

    obj = _parse_request_json(req, payload)
    if obj is None:
        meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        logger.warning(
            "[Perplexity] ask JSON parse failed path=%s payload_len=%d head=%r",
            str(meta.get("path") or ""),
            len(payload or b""),
            _safe_utf8(payload)[:240],
        )
        _dump_pplx_payload("ask_parse_fail", payload or b"", meta)
        return None
    _pplx_note_model(req, obj)
    prompt = _extract_prompt(obj)
    if prompt:
        return prompt
    # Do not fall back to raw JSON (analytics-shaped bodies look like prompts).
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    logger.warning(
        "[Perplexity] ask empty prompt keys=%s param_keys=%s query_str_type=%s dsl_type=%s head=%r",
        list(obj.keys())[:24],
        list(params.keys())[:24],
        type(obj.get("query_str")).__name__,
        type(params.get("dsl_query")).__name__,
        _safe_utf8(payload)[:320],
    )
    _dump_pplx_payload("ask_empty_prompt", payload or b"", meta)
    req["_extract_final"] = False
    return ""


def build_policy_api_payload(
    req: Dict[str, Any],
    payload: bytes,
    extracted_text: str,
) -> Optional[Dict[str, Any]]:
    direction = (req.get("direction") or "").lower()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}

    if direction == "response":
        if not is_perplexity_chat_turn_path(req):
            return None
        session_id = str(req.get("_pplx_output_session_id") or "")
        message_id = str(req.get("_pplx_output_message_id") or "")
        if not session_id or not message_id:
            obj = _parse_sse_data_json(_safe_utf8(payload))
            if isinstance(obj, dict):
                if not session_id:
                    session_id = _extract_response_session_id_obj(obj)
                if not message_id:
                    message_id = _extract_response_message_id_obj(obj)

        user_id = ""
        email = ""
        session_keys = _session_keys_from_meta(meta)
        if session_keys and _account_lookup:
            info = _account_lookup.wait_for(
                "perplexity",
                session_keys,
                env_name="AIVAX_PERPLEXITY_ACCOUNT_WAIT_MS",
            )
            if info:
                user_id, email = info[0], info[1]
        response_name = _pplx_cached_name(meta, user_id)
        response_payload = {
            "llm_output": extracted_text,
            "user_id": user_id,
            "email": email,
            "ai_service": AI_SERVICE_PERPLEXITY,
            "client_host": str(meta.get("host") or ""),
            "session_id": session_id,
            "message_id": message_id,
        }
        if response_name:
            response_payload["user_name"] = response_name
        return response_payload

    if not is_perplexity_chat_turn_path(req):
        return None

    obj = _parse_request_json(req, payload)
    if obj is None:
        return None

    prompt = _extract_prompt(obj) or extracted_text

    # user_id/email can be at top level or under "user"
    user_id = str(obj.get("user_id") or "")
    email = str(obj.get("email") or "")
    if not user_id or not email:
        u = obj.get("user")
        if isinstance(u, dict):
            if not user_id:
                user_id = str(u.get("id") or u.get("uuid") or "")
            if not email:
                email = str(u.get("email") or "")

    if not user_id or not email:
        info = _lookup_perplexity_account(meta, user_id)
        if info:
            if not user_id:
                user_id = info[0]
            if not email:
                email = info[1]
    # visitor-id is a rotating cookie correlator, not the account.
    # Identity comes from /api/auth/session email/user.id.

    user_name = _pplx_cached_name(meta, user_id)
    if user_id or email or user_name:
        logger.info(
            "[Account Info] perplexity: chat_turn user_id=%s email=%s name=%s path=%s",
            user_id or "-",
            email or "-",
            user_name or "-",
            str(meta.get("path") or ""),
        )

    session_id = _extract_request_session_id(obj)
    message_id = _extract_request_message_id(obj)
    attachments = build_policy_attachments(req, obj, payload)
    named = [
        str((a or {}).get("name") or "")
        for a in attachments
        if isinstance(a, dict) and str((a or {}).get("name") or "").strip()
    ]
    on_disk = sum(
        1
        for a in attachments
        if isinstance(a, dict)
        and str((a or {}).get("id") or "").startswith("/run/aivax/attachments/")
    )
    logger.info(
        "[Perplexity] ask prompt_len=%d session_id=%s message_id=%s "
        "attachments=%d on_disk=%d names=%s preview=%r",
        len(prompt or ""),
        session_id or "-",
        message_id or "-",
        len([a for a in attachments if isinstance(a, dict) and str((a or {}).get("id") or "").strip()]),
        on_disk,
        named[:8],
        (prompt or "")[:80],
    )

    ask_payload = {
        "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
        "prompt": prompt,
        "encoding": False,
        "user_id": user_id,
        "email": email,
        "ai_service": AI_SERVICE_PERPLEXITY,
        "client_host": str(meta.get("host") or ""),
        "session_id": session_id,
        "attachments": attachments,
        "message_id": message_id,
    }
    if user_name:
        ask_payload["user_name"] = user_name
    return ask_payload


_BLOCK_SYSINST_VERTICAL_GAP = "\n" * 1


def _apply_perplexity_prompt_rewrite(obj: Dict[str, Any], replacement_text: str) -> int:
    changed = 0
    for key in ("query_str", "query", "message", "prompt", "input", "text"):
        if isinstance(obj.get(key), str):
            if obj[key] != replacement_text:
                obj[key] = replacement_text
                changed += 1
            break
    else:
        if replacement_text:
            obj["query_str"] = replacement_text
            changed += 1

    params = obj.get("params")
    if isinstance(params, dict):
        for key in ("query_str", "query", "dsl_query", "message", "prompt", "input", "text"):
            if isinstance(params.get(key), str):
                if params[key] != replacement_text:
                    params[key] = replacement_text
                    changed += 1

    queries = obj.get("queries")
    if isinstance(queries, list):
        for q in queries:
            if not isinstance(q, dict):
                continue
            for key in ("query", "query_str", "text"):
                if isinstance(q.get(key), str) and q[key] != replacement_text:
                    q[key] = replacement_text
                    changed += 1
                    break

    return changed


def _encode_perplexity_request(raw_text: str, obj: Dict[str, Any]) -> bytes:
    encoded_json = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    event_name, data_text = _parse_sse_payload(raw_text)
    is_enveloped = bool(data_text and data_text != "[DONE]" and not raw_text.lstrip().startswith("{"))
    if is_enveloped:
        ev = event_name or "message"
        return f"event: {ev}\ndata: {encoded_json}\n\n".encode("utf-8")
    return encoded_json.encode("utf-8")


def _chat_has_attachments(obj: Dict[str, Any]) -> bool:
    return bool(_collect_attachment_refs(obj))


def _rewrite_perplexity_payload(
    raw_text: str,
    obj: Dict[str, Any],
    replacement_text: str,
) -> Optional[Tuple[bytes, int]]:
    had_attachments = _chat_has_attachments(obj)
    changed = _apply_perplexity_prompt_rewrite(obj, replacement_text)
    strip_cnt = strip_chat_attachments(obj)
    if changed <= 0 and strip_cnt <= 0 and not had_attachments:
        return None
    if changed <= 0 and (strip_cnt > 0 or had_attachments):
        changed = 1
    return _encode_perplexity_request(raw_text, obj), changed


def rewrite_prompt_for_block(
    req: Dict[str, Any],
    payload: bytes,
    user_prompt: str,
    system_instruction: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    """BLOCK 전용: 지시문을 안내문 앞에 두고 query_str 등을 치환한다.

    지시문을 뒤에 붙이면 모델이 마지막 줄([AIVAX 정책 응답 처리] 등)을 그대로 출력한다.
    요청 rewrite만 하고 응답 overlay는 쓰지 않는다(overlay 실패 시 답이 비기 때문).
    """
    del source_prompt
    if not is_perplexity_chat_turn_path(req):
        return None
    raw_text = _safe_utf8(payload)
    obj = _parse_request_json(req, payload)
    if obj is None:
        return None

    sys_t = (system_instruction or "").strip()
    user_t = (user_prompt or "").strip()
    if sys_t and user_t:
        combined = f"{sys_t}\n{_BLOCK_SYSINST_VERTICAL_GAP}{user_t}"
    else:
        combined = user_t or sys_t
    if not combined:
        return None

    out = _rewrite_perplexity_payload(raw_text, obj, combined)
    if out is None:
        return None
    encoded, _ = out
    return encoded, "adapter:perplexity_policy_block_sysinstruct"


_PPLX_TOP_FILE_KEYS = ("attachments", "files", "file_attachments", "fileAttachments")


def _consent_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def consent_attachment_state(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    """File refs of the blocked ask. The S3 upload itself already succeeded."""
    if (req.get("direction") or "").lower() != "request":
        return None
    if not is_perplexity_chat_turn_path(req):
        return None
    obj = _parse_request_json(req, payload)
    if not isinstance(obj, dict):
        return None
    top = {
        key: _consent_copy(obj[key])
        for key in _PPLX_TOP_FILE_KEYS
        if isinstance(obj.get(key), list) and obj[key]
    }
    params = obj.get("params")
    params_attachments = None
    if isinstance(params, dict):
        att = params.get("attachments")
        if isinstance(att, list) and att:
            params_attachments = _consent_copy(att)
    if not top and params_attachments is None:
        return None
    return {"top": top, "params_attachments": params_attachments}


def restore_consent_attachments(
    req: Dict[str, Any],
    payload: bytes,
    state: Any,
    prompt: str = "",
) -> Optional[bytes]:
    """Re-add the blocked ask's file refs that the rewrite stripped."""
    del prompt
    if not isinstance(state, dict):
        return None
    if not is_perplexity_chat_turn_path(req):
        return None
    raw_text = _safe_utf8(payload)
    obj = _parse_request_json(req, payload)
    if not isinstance(obj, dict):
        return None
    restored = 0
    for key, value in (state.get("top") or {}).items():
        obj[key] = _consent_copy(value)
        restored += 1
    params_attachments = state.get("params_attachments")
    if isinstance(params_attachments, list):
        params = obj.get("params")
        if not isinstance(params, dict):
            params = {}
            obj["params"] = params
        params["attachments"] = _consent_copy(params_attachments)
        restored += 1
    if restored <= 0:
        return None
    return _encode_perplexity_request(raw_text, obj)


def rewrite_prompt_for_consent(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    """Consent: restore the original blocked prompt in query_str (and params)."""
    del source_prompt
    if not is_perplexity_chat_turn_path(req):
        return None
    raw_text = _safe_utf8(payload)
    obj = _parse_request_json(req, payload)
    if obj is None:
        return None
    if not replacement_text:
        return None
    out = _rewrite_perplexity_payload(raw_text, obj, replacement_text)
    if out is None:
        return None
    encoded, _ = out
    return encoded, "adapter:perplexity_policy_consent"


def rewrite_prompt_masking(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    """MASKING 전용: query_str 등 치환 + attachments strip."""
    del source_prompt
    if not is_perplexity_chat_turn_path(req):
        return None
    raw_text = _safe_utf8(payload)
    obj = _parse_request_json(req, payload)
    if obj is None:
        return None
    if not replacement_text:
        return None
    out = _rewrite_perplexity_payload(raw_text, obj, replacement_text)
    if out is None:
        return None
    encoded, _ = out
    return encoded, "adapter:perplexity_policy_masking"


def rewrite_prompt(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    policy_code: str,
) -> Optional[Tuple[bytes, str]]:
    if not is_perplexity_chat_turn_path(req):
        return None
    raw_text = _safe_utf8(payload)
    obj = _parse_request_json(req, payload)
    if obj is None:
        return None
    if not replacement_text:
        return None

    out = _rewrite_perplexity_payload(raw_text, obj, replacement_text)
    if out is None:
        return None
    encoded, _ = out
    code = (policy_code or "masking").strip().lower()
    return encoded, f"adapter:perplexity_policy_{code}"


def extract_full_body(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    """Suricata eve / Deep INLINE response body: SSE assistant text, or auth session account extract.

    Deep INLINE sets `_eve_full_body=true`, so /api/auth/session must be handled here
    (not only in extract()).
    """
    if _is_auth_session_response(req):
        try:
            obj = json.loads(_safe_utf8(payload))
            if isinstance(obj, dict):
                _log_auth_session(req, obj)
        except Exception:
            pass
        req["_extract_final"] = False
        return ""

    # Prefer ask-path gate; also accept event-stream on perplexity hosts when path
    # is missing/mis-tagged on Deep INLINE tees.
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    ct = str(req.get("content_type") or meta.get("content_type") or "").lower()
    is_event_stream = "event-stream" in ct or (req.get("unit_type") or "").lower() == "sse_event"
    if not is_perplexity_chat_turn_path(req):
        if not is_event_stream:
            req["_extract_final"] = False
            return ""
        host = str(meta.get("host") or "").lower()
        if "perplexity.ai" not in host:
            req["_extract_final"] = False
            return ""

    text = _safe_utf8(payload)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    final_answers: List[str] = []
    variant_answers: List[str] = []
    event_count = 0
    usage_counts: Dict[str, int] = {}
    for obj in _iter_sse_json_objects(text):
        event_count += 1
        is_final = _is_sse_final_event(obj)
        blocks = obj.get("blocks")
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict):
                    usage = str(block.get("intended_usage") or "-")
                    usage_counts[usage] = usage_counts.get(usage, 0) + 1
        ft = _extract_final_text(obj, final_event=is_final)
        if not ft:
            # Mid-stream deltas are not trusted for OpenSearch output — only
            # variant=answer / final envelope text is.
            continue
        message_id = _extract_response_message_id_obj(obj)
        if message_id:
            req["_pplx_output_message_id"] = message_id
        session_id = _extract_response_session_id_obj(obj)
        if session_id:
            req["_pplx_output_session_id"] = session_id
        if is_final:
            final_answers.append(ft)
        else:
            variant_answers.append(ft)

    result = _pick_last_nonempty(final_answers) or _pick_last_nonempty(variant_answers)
    if result:
        req["_extract_final"] = True
        logger.info(
            "[Perplexity] extract_full_body ok path=%s events=%d text_len=%d session_id=%s "
            "final_hits=%d variant_hits=%d usages=%s",
            str(meta.get("path") or ""),
            event_count,
            len(result),
            str(req.get("_pplx_output_session_id") or "-"),
            len(final_answers),
            len(variant_answers),
            usage_counts,
        )
        return result

    logger.warning(
        "[Perplexity] extract_full_body empty path=%s payload_len=%d events=%d usages=%s head=%r",
        str(meta.get("path") or ""),
        len(payload or b""),
        event_count,
        usage_counts,
        text[:240],
    )
    _dump_pplx_payload("ask_sse_empty", payload or b"", meta)
    return None


def extract_session_id(req: Dict[str, Any], payload: bytes) -> str:
    stream_key = str(req.get("_pplx_stream_key") or "")
    if stream_key and stream_key != _PPLX_FALLBACK_KEY:
        return stream_key

    if _is_json_request(req):
        obj = _parse_request_json(req, payload)
        if isinstance(obj, dict):
            sid = _extract_request_session_id(obj)
            if sid:
                return sid
            mid = _extract_request_message_id(obj)
            if mid:
                return mid

    if _is_sse_response(req):
        obj = _parse_sse_data_json(_safe_utf8(payload))
        if isinstance(obj, dict):
            sid = _extract_response_session_id_obj(obj)
            if sid:
                return sid
            mid = _extract_response_message_id_obj(obj)
            if mid:
                return mid
        if len(_PPLX_RESPONSE_BUFFER) == 1:
            return next(iter(_PPLX_RESPONSE_BUFFER))
        if len(_PPLX_RESPONSE_MESSAGE_ID) == 1:
            return next(iter(_PPLX_RESPONSE_MESSAGE_ID))

    return ""


def response_stream_done(req: Dict[str, Any], payload: bytes) -> bool:
    if not _is_sse_response(req):
        return False

    raw = _safe_utf8(payload)
    _, data_text = _parse_sse_payload(raw)
    if data_text in ("[DONE]", ""):
        return data_text == "[DONE]"
    if data_text.strip() == "{}":
        return True

    obj = _parse_sse_data_json(raw)
    if not isinstance(obj, dict):
        return False
    return bool(obj.get("final_sse_message"))


def _rewrite_diff_block(
    diff_block: Dict[str, Any],
    replacement_text: str,
    emitted: bool,
) -> Tuple[int, bool]:
    changed = 0
    patches = diff_block.get("patches")
    if not isinstance(patches, list):
        return 0, emitted

    for patch in patches:
        if not isinstance(patch, dict):
            continue
        path = patch.get("path")
        value = patch.get("value")

        if isinstance(path, str) and path.startswith("/chunks/") and isinstance(value, str):
            target = replacement_text if not emitted else ""
            if value != target:
                patch["value"] = target
                changed += 1
            emitted = True
            continue

        if isinstance(value, dict):
            v = dict(value)
            if isinstance(v.get("chunks"), list):
                # Keep patch shape intact and only rewrite chunk values.
                new_chunks = [replacement_text] if not emitted else [""]
                if v["chunks"] != new_chunks:
                    v["chunks"] = new_chunks
                    changed += 1
                emitted = True
            if v != value:
                patch["value"] = v
            continue

        if path == "" and isinstance(value, str):
            target = replacement_text if not emitted else ""
            if value != target:
                patch["value"] = target
                changed += 1
            emitted = True

    return changed, emitted


def _rewrite_markdown_block(markdown: Dict[str, Any], replacement_text: str, emitted: bool) -> int:
    changed = 0
    if isinstance(markdown.get("chunks"), list):
        # For Perplexity UI stability, rewrite only chunk list values.
        new_chunks = [replacement_text] if not emitted else [""]
        if markdown.get("chunks") != new_chunks:
            markdown["chunks"] = new_chunks
            changed += 1
    return changed


def rewrite_response(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
) -> Optional[Tuple[bytes, str]]:
    if not replacement_text or not _is_sse_response(req):
        return None

    raw = _safe_utf8(payload)
    _, data_text = _parse_sse_payload(raw)
    if data_text == "[DONE]":
        session_id = extract_session_id(req, payload)
        if session_id:
            _PPLX_RESPONSE_OVERRIDE_EMITTED.pop(session_id, None)
        return None
    if data_text.strip() == "{}":
        session_id = extract_session_id(req, payload)
        if session_id:
            _PPLX_RESPONSE_OVERRIDE_EMITTED.pop(session_id, None)
        return None

    try:
        obj = json.loads(data_text)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None

    state_key = _resolve_stream_key(req, obj)
    req["_pplx_stream_key"] = state_key
    message_id = _extract_response_message_id_obj(obj)
    if message_id:
        _PPLX_RESPONSE_MESSAGE_ID[state_key] = message_id

    stream_emitted = bool(_PPLX_RESPONSE_OVERRIDE_EMITTED.get(state_key, False))
    emitted_this_event = False
    changed = 0

    blocks = obj.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            usage = str(block.get("intended_usage") or "")
            if usage not in ("ask_text_0_markdown", "ask_text"):
                continue

            diff_block = block.get("diff_block")
            if isinstance(diff_block, dict):
                c, block_emitted = _rewrite_diff_block(diff_block, replacement_text, stream_emitted)
                changed += c
                if block_emitted and not stream_emitted:
                    emitted_this_event = True

            markdown = block.get("markdown_block")
            if isinstance(markdown, dict):
                changed += _rewrite_markdown_block(markdown, replacement_text, stream_emitted)
                if not stream_emitted and isinstance(markdown.get("chunks"), list):
                    emitted_this_event = True

    _PPLX_RESPONSE_OVERRIDE_EMITTED[state_key] = bool(stream_emitted or emitted_this_event)

    if bool(obj.get("final_sse_message")):
        _PPLX_RESPONSE_OVERRIDE_EMITTED.pop(state_key, None)

    if changed <= 0:
        return None

    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, "adapter:perplexity_policy_block_message"


def clear_response_override_state(session_id: str) -> None:
    key = (session_id or "").strip()
    if not key:
        return
    _PPLX_RESPONSE_OVERRIDE_EMITTED.pop(key, None)
    _PPLX_RESPONSE_BUFFER.pop(key, None)
    _PPLX_RESPONSE_MESSAGE_ID.pop(key, None)
