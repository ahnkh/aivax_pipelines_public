"""
LibreChat internal gateway (chat_version) adapter.

Traffic: http://10.0.50.84:3080/api/agents/chat/{provider}
  POST /api/agents/chat/{provider}  -> JSON {message, conversationId, ...}
  POST response                     -> {streamId, conversationId, status}
  GET  /api/agents/chat/stream/:id  -> SSE (on_message_delta, final)

Provider path segment maps to policy ai_service code (openAI->GPT, google->Gemini, ...).
File upload (/api/files) is not handled here.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import account_lookup as _account_lookup
except ImportError:
    _account_lookup = None

from ai_service_codes import (
    AI_SERVICE_CLAUDE,
    AI_SERVICE_GEMINI,
    AI_SERVICE_GPT,
    AI_SERVICE_GROK,
    AI_SERVICE_PERPLEXITY,
)

logger = logging.getLogger(__name__)

CHAT_VERSION_GATEWAY_HOST = "10.0.50.84"

_PROVIDER_PATH_RE = re.compile(
    r"/api/agents/chat/(?P<provider>[^/?]+)", re.IGNORECASE
)
_STREAM_PATH_RE = re.compile(
    r"/api/agents/chat/stream/(?P<stream_id>[^/?]+)", re.IGNORECASE
)

_RESERVED_CHAT_PATH_SEGMENTS = frozenset(
    {"stream", "active", "status", "tools"},
)

_PROVIDER_TO_AI_SERVICE = {
    "openai": AI_SERVICE_GPT,
    "google": AI_SERVICE_GEMINI,
    "anthropic": AI_SERVICE_CLAUDE,
    "grok": AI_SERVICE_GROK,
    "perplexity": AI_SERVICE_PERPLEXITY,
}

_CHAT_VERSION_RESPONSE_BUFFER: Dict[str, str] = {}
_CHAT_VERSION_OUTPUT_MESSAGE_ID: Dict[str, str] = {}
_CHAT_VERSION_STREAM_PROVIDER: Dict[str, str] = {}
_CHAT_VERSION_STREAM_CONVERSATION: Dict[str, str] = {}
_CHAT_VERSION_RESPONSE_OVERRIDE_EMITTED: Dict[str, bool] = {}
_CHAT_VERSION_EMITTED_LOCK = threading.Lock()
_CHAT_VERSION_FALLBACK_KEY = "__fallback__"
_BLOCK_SYSINST_VERTICAL_GAP = "\n" * 1


def _safe_utf8(data: Union[bytes, bytearray, str]) -> str:
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _path_base(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("path") or "").split("?", 1)[0]


def _host_base(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("host") or "").split(":", 1)[0].lower()


def _is_chat_version_host(req: Dict[str, Any]) -> bool:
    return _host_base(req) == CHAT_VERSION_GATEWAY_HOST.lower()


def _resolve_provider(req: Dict[str, Any]) -> str:
    m = _PROVIDER_PATH_RE.search(_path_base(req))
    if not m:
        return ""
    provider = str(m.group("provider") or "").strip().lower()
    if provider in _RESERVED_CHAT_PATH_SEGMENTS:
        return ""
    return provider


def _remember_stream_context(
    stream_id: str,
    conversation_id: str,
    provider: str,
) -> None:
    sid = (stream_id or "").strip()
    if not sid:
        return
    conv = (conversation_id or "").strip()
    if conv:
        _CHAT_VERSION_STREAM_CONVERSATION[sid] = conv
    prov = (provider or "").strip().lower()
    if prov:
        _CHAT_VERSION_STREAM_PROVIDER[sid] = prov


def _resolve_provider_for_policy(req: Dict[str, Any]) -> str:
    provider = _resolve_provider(req)
    if provider:
        return provider
    stream_id = _extract_stream_id_from_path(req)
    if stream_id:
        cached = _CHAT_VERSION_STREAM_PROVIDER.get(stream_id, "")
        if cached:
            return cached
    return str(req.get("_chat_version_provider") or "").strip().lower()


def _resolve_policy_ai_service(req: Dict[str, Any]) -> int:
    provider = _resolve_provider_for_policy(req)
    if not provider:
        return 0
    return int(_PROVIDER_TO_AI_SERVICE.get(provider, 0))


def _extract_stream_id_from_path(req: Dict[str, Any]) -> str:
    m = _STREAM_PATH_RE.search(_path_base(req))
    if m:
        return str(m.group("stream_id") or "").strip()
    return ""


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
    return unit_type == "json_body" or "application/json" in content_type


def _is_sse_response(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if direction != "response":
        return False
    return unit_type == "sse_event" or "text/event-stream" in content_type


def _is_chat_post_request(req: Dict[str, Any]) -> bool:
    if not _is_chat_version_host(req):
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    if str(meta.get("method") or "").upper() != "POST":
        return False
    provider = _resolve_provider(req)
    if not provider:
        return False
    base = _path_base(req).rstrip("/")
    if base.endswith("/active") or "/status/" in base:
        return False
    return True


def _is_stream_get_request(req: Dict[str, Any]) -> bool:
    if not _is_chat_version_host(req):
        return False
    return _STREAM_PATH_RE.search(_path_base(req)) is not None


def _is_chat_start_response(req: Dict[str, Any]) -> bool:
    if not _is_json_response(req):
        return False
    if not _is_chat_version_host(req):
        return False
    base = _path_base(req)
    if "/api/agents/chat/stream/" in base:
        return False
    return _PROVIDER_PATH_RE.search(base) is not None


def _is_user_info_response(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    if direction != "response" or not _is_chat_version_host(req):
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "").split("?", 1)[0]
    return path.rstrip("/") == "/api/user"


def _is_auth_refresh_response(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    if direction != "response" or not _is_chat_version_host(req):
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "").split("?", 1)[0]
    return path.rstrip("/") == "/api/auth/refresh"


def _parse_json_payload(payload: bytes) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _parse_request_json(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    if _is_chat_post_request(req):
        obj = _parse_json_payload(payload)
        if obj is not None:
            return obj
    if not _is_json_request(req):
        unit_type = (req.get("unit_type") or "").lower()
        content_type = (req.get("content_type") or "").lower()
        if unit_type != "raw_body" and "application/json" not in content_type:
            return None
    return _parse_json_payload(payload)


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


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = str(token or "").split(".")
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


def _extract_identity_from_jwt(token: str) -> Tuple[str, str]:
    claims = _decode_jwt_payload(token)
    if not claims:
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


def _extract_identity_from_authorization(req: Dict[str, Any]) -> Tuple[str, str]:
    token = _extract_bearer_token(req)
    if not token:
        return "", ""
    return _extract_identity_from_jwt(token)


def _register_account(session_key: str, user_id: str, email: str) -> None:
    if not _account_lookup or not session_key:
        return
    if user_id or email:
        _account_lookup.set_account("chat_version", session_key, user_id or "", email or "")


def _apply_account_to_req(req: Dict[str, Any], user_id: str, email: str) -> None:
    if user_id:
        req["_chat_version_session_user_id"] = user_id
    if email:
        req["_chat_version_session_email"] = email


def _register_account_from_bearer(req: Dict[str, Any]) -> Tuple[str, str]:
    token = _extract_bearer_token(req)
    if not token:
        return "", ""

    user_id, email = _extract_identity_from_jwt(token)
    if _account_lookup:
        cached = _account_lookup.get("chat_version", token)
        if cached:
            if not user_id:
                user_id = cached[0]
            if not email:
                email = cached[1]

    if user_id or email:
        _register_account(token, user_id, email)
        _apply_account_to_req(req, user_id, email)
        logging.debug(
            "[Account Info] chat_version: bearer user_id=%s email=%s token=%s",
            user_id or "-",
            email or "-",
            token[:12] + "..." if len(token) > 12 else token or "-",
        )
    return user_id, email


def _log_user_from_obj(req: Dict[str, Any], obj: Dict[str, Any]) -> None:
    user = obj.get("user")
    user_id = ""
    email = ""
    if isinstance(user, dict):
        user_id = str(user.get("_id") or user.get("id") or "").strip()
        email = str(user.get("email") or "").strip()

    token_field = obj.get("token")
    if isinstance(token_field, str) and token_field.strip():
        jwt_user_id, jwt_email = _extract_identity_from_jwt(token_field.strip())
        if not user_id:
            user_id = jwt_user_id
        if not email:
            email = jwt_email
        _register_account(token_field.strip(), user_id, email)

    bearer = _extract_bearer_token(req)
    session_key = bearer or (token_field.strip() if isinstance(token_field, str) else "")
    if session_key:
        _register_account(session_key, user_id, email)

    if user_id or email:
        _apply_account_to_req(req, user_id, email)
        logging.debug(
            "[Account Info] chat_version: refresh user_id=%s email=%s",
            user_id or "-",
            email or "-",
        )


def _lookup_account(req: Dict[str, Any]) -> Tuple[str, str]:
    user_id = str(req.get("_chat_version_session_user_id") or "").strip()
    email = str(req.get("_chat_version_session_email") or "").strip()

    if not user_id or not email:
        auth_user_id, auth_email = _extract_identity_from_authorization(req)
        if not user_id:
            user_id = auth_user_id
        if not email:
            email = auth_email

    token = _extract_bearer_token(req)
    if _account_lookup and token:
        info = _account_lookup.get("chat_version", token)
        if info:
            if not user_id:
                user_id = info[0]
            if not email:
                email = info[1]

    if token and (user_id or email):
        _register_account(token, user_id, email)

    return user_id, email


def _extract_prompt(obj: Dict[str, Any]) -> str:
    message = obj.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    for key in ("prompt", "input", "query", "text"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_conversation_id(obj: Dict[str, Any]) -> str:
    for key in ("conversationId", "conversation_id"):
        value = obj.get(key)
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


def _extract_delta_text_from_content(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "".join(chunks)


def _extract_sse_delta(obj: Dict[str, Any]) -> str:
    event = str(obj.get("event") or "")
    if event != "on_message_delta":
        return ""
    data = obj.get("data")
    if not isinstance(data, dict):
        return ""
    delta = data.get("delta")
    if not isinstance(delta, dict):
        return ""
    return _extract_delta_text_from_content(delta.get("content"))


def _extract_response_message_text(obj: Dict[str, Any]) -> str:
    response = obj.get("responseMessage")
    if not isinstance(response, dict):
        return ""
    text = response.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    content_text = _extract_delta_text_from_content(response.get("content"))
    if content_text.strip():
        return content_text.strip()
    return ""


def _resolve_stream_key(req: Dict[str, Any], obj: Optional[Dict[str, Any]] = None) -> str:
    stream_id = _extract_stream_id_from_path(req)
    if stream_id:
        return stream_id
    if isinstance(obj, dict):
        for key in ("streamId", "conversationId"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        message = obj.get("message")
        if isinstance(message, dict):
            conv = message.get("conversationId")
            if isinstance(conv, str) and conv.strip():
                return conv.strip()
    req_key = str(req.get("_chat_version_stream_key") or "")
    if req_key:
        return req_key
    if len(_CHAT_VERSION_RESPONSE_BUFFER) == 1:
        return next(iter(_CHAT_VERSION_RESPONSE_BUFFER))
    return _CHAT_VERSION_FALLBACK_KEY


def _set_output_ctx(req: Dict[str, Any], key: str, obj: Optional[Dict[str, Any]] = None) -> None:
    session_id = key if key != _CHAT_VERSION_FALLBACK_KEY else ""
    req["_chat_version_output_session_id"] = session_id
    message_id = ""
    if isinstance(obj, dict):
        response = obj.get("responseMessage")
        if isinstance(response, dict):
            message_id = str(response.get("messageId") or "")
    if message_id:
        _CHAT_VERSION_OUTPUT_MESSAGE_ID[key] = message_id
    req["_chat_version_output_message_id"] = str(
        _CHAT_VERSION_OUTPUT_MESSAGE_ID.get(key, "") or message_id
    )


def _set_extract_final(req: Dict[str, Any], is_final: bool) -> None:
    req["_extract_final"] = bool(is_final)


def extract(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    if not _is_chat_version_host(req):
        return None

    if _is_user_info_response(req) or _is_auth_refresh_response(req):
        try:
            obj = json.loads(_safe_utf8(payload))
            if isinstance(obj, dict):
                if _is_user_info_response(req) and "user" not in obj and (
                    obj.get("_id") or obj.get("id") or obj.get("email")
                ):
                    _log_user_from_obj(req, {"user": obj})
                else:
                    _log_user_from_obj(req, obj)
        except Exception:
            pass
        _set_extract_final(req, False)
        return ""

    if _is_chat_start_response(req):
        try:
            obj = json.loads(_safe_utf8(payload))
            if isinstance(obj, dict):
                _remember_stream_context(
                    str(obj.get("streamId") or ""),
                    str(obj.get("conversationId") or ""),
                    _resolve_provider(req),
                )
        except Exception:
            pass
        _set_extract_final(req, False)
        return ""

    if _is_sse_response(req):
        raw = _safe_utf8(payload)
        _, data_text = _parse_sse_payload(raw)
        if not data_text:
            _set_extract_final(req, False)
            return ""

        if data_text == "[DONE]":
            key = _resolve_stream_key(req)
            merged = _CHAT_VERSION_RESPONSE_BUFFER.pop(key, "")
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

        key = _resolve_stream_key(req, obj)
        req["_chat_version_stream_key"] = key

        if obj.get("final") is True:
            merged = _CHAT_VERSION_RESPONSE_BUFFER.pop(key, "")
            if not merged:
                merged = _extract_response_message_text(obj)
            _set_output_ctx(req, key, obj)
            _set_extract_final(req, bool(merged))
            return merged

        delta = _extract_sse_delta(obj)
        if delta:
            _CHAT_VERSION_RESPONSE_BUFFER[key] = (
                _CHAT_VERSION_RESPONSE_BUFFER.get(key, "") + delta
            )

        _set_extract_final(req, False)
        return ""

    if not _is_chat_post_request(req):
        _set_extract_final(req, False)
        return ""

    _register_account_from_bearer(req)

    obj = _parse_request_json(req, payload)
    if obj is None:
        return None

    provider = _resolve_provider(req)
    req["_chat_version_provider"] = provider
    conversation_id = _extract_conversation_id(obj)
    if conversation_id:
        req["_chat_version_conversation_id"] = conversation_id
    stream_id = str(obj.get("streamId") or "").strip()
    if stream_id:
        _remember_stream_context(stream_id, conversation_id, provider)

    prompt = _extract_prompt(obj)
    if prompt:
        return prompt

    attachments = obj.get("attachments")
    if isinstance(attachments, list) and attachments:
        return "사용자가 파일을 업로드하였습니다."

    return ""


def build_policy_api_payload(
    req: Dict[str, Any],
    payload: bytes,
    extracted_text: str,
) -> Optional[Dict[str, Any]]:
    direction = (req.get("direction") or "").lower()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    ai_service = _resolve_policy_ai_service(req)
    if direction == "request":
        _register_account_from_bearer(req)
    user_id, email = _lookup_account(req)

    if direction == "response":
        session_id = str(
            req.get("_chat_version_output_session_id")
            or req.get("_chat_version_conversation_id")
            or ""
        )
        message_id = str(req.get("_chat_version_output_message_id") or "")
        return {
            "llm_output": extracted_text,
            "user_id": user_id,
            "email": email,
            "ai_service": ai_service,
            "client_host": str(meta.get("host") or ""),
            "session_id": session_id,
            "message_id": message_id,
        }

    obj = _parse_request_json(req, payload)
    prompt = extracted_text
    session_id = ""
    if isinstance(obj, dict):
        prompt = _extract_prompt(obj) or extracted_text
        session_id = _extract_conversation_id(obj)

    return {
        "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
        "prompt": prompt,
        "encoding": False,
        "user_id": user_id,
        "email": email,
        "ai_service": ai_service,
        "client_host": str(meta.get("host") or ""),
        "session_id": session_id,
        "attachments": [{"id": "", "size": 0, "name": "", "mime_type": ""}],
        "message_id": "",
    }


def extract_session_id(req: Dict[str, Any], payload: bytes) -> str:
    conv = str(req.get("_chat_version_conversation_id") or "").strip()
    if conv:
        return conv
    stream_id = _extract_stream_id_from_path(req)
    if stream_id:
        mapped = _CHAT_VERSION_STREAM_CONVERSATION.get(stream_id, "")
        if mapped:
            return mapped
        return stream_id
    obj = _parse_json_payload(payload) if payload else None
    if isinstance(obj, dict):
        for key in ("conversationId", "conversation_id", "streamId"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        message = obj.get("message")
        if isinstance(message, dict):
            value = message.get("conversationId")
            if isinstance(value, str) and value.strip():
                return value.strip()
    sse_obj = _parse_sse_data_json(_safe_utf8(payload)) if payload else None
    if isinstance(sse_obj, dict):
        for key in ("conversationId", "streamId"):
            value = sse_obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        conversation = sse_obj.get("conversation")
        if isinstance(conversation, dict):
            value = conversation.get("conversationId")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def response_stream_done(req: Dict[str, Any], payload: bytes) -> bool:
    if not _is_sse_response(req):
        return False
    obj = _parse_sse_data_json(_safe_utf8(payload))
    if isinstance(obj, dict) and obj.get("final") is True:
        return True
    raw = _safe_utf8(payload)
    _, data_text = _parse_sse_payload(raw)
    return data_text == "[DONE]"


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

    changed = 0
    if isinstance(obj.get("message"), str):
        if obj["message"] != combined:
            obj["message"] = combined
            changed += 1
    else:
        for key in ("prompt", "input", "query", "text"):
            if isinstance(obj.get(key), str):
                if obj[key] != combined:
                    obj[key] = combined
                    changed += 1
                break
        else:
            obj["message"] = combined
            changed += 1

    if changed <= 0:
        return None

    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, "adapter:chat_version_policy_block_sysinstruct"


def _rewrite_content_blocks(
    content: Any,
    replacement_text: str,
    emitted: bool,
) -> Tuple[Any, int, bool]:
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
        target = replacement_text if not emitted else ""
        text = new_block.get("text")
        if isinstance(text, str) and text != target:
            new_block["text"] = target
            changed += 1
            emitted = True
        new_content.append(new_block)
    return new_content, changed, emitted


def _rewrite_sse_obj(obj: Dict[str, Any], replacement_text: str, state_key: str) -> int:
    with _CHAT_VERSION_EMITTED_LOCK:
        emitted = bool(_CHAT_VERSION_RESPONSE_OVERRIDE_EMITTED.get(state_key, False))
        changed = 0

        if obj.get("final") is True:
            response = obj.get("responseMessage")
            if isinstance(response, dict):
                target = replacement_text if not emitted else ""
                text = response.get("text")
                if isinstance(text, str) and text != target:
                    response["text"] = target
                    changed += 1
                    emitted = True
                new_content, c, emitted = _rewrite_content_blocks(
                    response.get("content"), replacement_text, emitted
                )
                if c > 0:
                    response["content"] = new_content
                    changed += c

        event = str(obj.get("event") or "")
        if event == "on_message_delta":
            data = obj.get("data")
            if isinstance(data, dict):
                delta = data.get("delta")
                if isinstance(delta, dict):
                    new_content, c, emitted = _rewrite_content_blocks(
                        delta.get("content"), replacement_text, emitted
                    )
                    if c > 0:
                        delta["content"] = new_content
                        changed += c

        _CHAT_VERSION_RESPONSE_OVERRIDE_EMITTED[state_key] = emitted
        if obj.get("final") is True and state_key:
            _CHAT_VERSION_RESPONSE_OVERRIDE_EMITTED.pop(state_key, None)
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
    state_key = _resolve_stream_key(req)

    if data_text == "[DONE]":
        with _CHAT_VERSION_EMITTED_LOCK:
            _CHAT_VERSION_RESPONSE_OVERRIDE_EMITTED.pop(state_key, None)
        return None

    try:
        obj = json.loads(data_text)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None

    changed = _rewrite_sse_obj(obj, replacement_text, state_key)
    if changed <= 0:
        return None

    encoded_json = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if raw.strip().startswith("data:") or raw.strip().startswith("event:"):
        encoded = f"data: {encoded_json}".encode("utf-8")
    else:
        encoded = encoded_json.encode("utf-8")
    return encoded, "adapter:chat_version_policy_block_message"


def clear_response_override_state(session_id: str) -> None:
    key = (session_id or "").strip()
    if not key:
        return
    with _CHAT_VERSION_EMITTED_LOCK:
        _CHAT_VERSION_RESPONSE_OVERRIDE_EMITTED.pop(key, None)
    _CHAT_VERSION_RESPONSE_BUFFER.pop(key, None)
    _CHAT_VERSION_OUTPUT_MESSAGE_ID.pop(key, None)


def rewrite_prompt(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    policy_code: str,
) -> Optional[Tuple[bytes, str]]:
    obj = _parse_request_json(req, payload)
    if obj is None:
        return None
    combined = (replacement_text or "").strip()
    if not combined:
        return None
    changed = 0
    if isinstance(obj.get("message"), str):
        if obj["message"] != combined:
            obj["message"] = combined
            changed += 1
    else:
        for key in ("prompt", "input", "query", "text"):
            if isinstance(obj.get(key), str):
                if obj[key] != combined:
                    obj[key] = combined
                    changed += 1
                break
        else:
            obj["message"] = combined
            changed += 1
    if changed <= 0:
        return None
    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, f"adapter:chat_version_policy_{policy_code}"
