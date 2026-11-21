"""
Claude Adapter
Extract request prompt from JSON body and support request prompt rewrite.
"""

from __future__ import annotations

import ast
import gzip
import json
import logging
import os
import re
import time
import uuid
import zlib
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import account_lookup as _account_lookup
except ImportError:
    _account_lookup = None

from ai_service_codes import AI_SERVICE_CLAUDE
from adapters.claude_upload import (
    POLICY_ATTACHMENTS_FINAL,
    build_policy_attachments,
    build_rpc_policy_attachments,
    normalize_upload,
    save_perform_action_inline_files,
    strip_completion_attachments,
    _is_file_content_block,
)

logger = logging.getLogger(__name__)


_PATH_RE = re.compile(
    r"/api/organizations/(?P<org>[0-9a-fA-F-]+)/chat_conversations/(?P<conv>[0-9a-fA-F-]+)(?:/completion)?(?:[/?].*)?$"
)


_ORG_PROFILE_PATH_RE = re.compile(r"^/api/organizations/(?P<org>[0-9a-fA-F-]+)/?$")
_BOOTSTRAP_APP_START_PATH_RE = re.compile(
    r"^/(?:api|edge-api)/bootstrap/(?P<id>[0-9a-fA-F-]+)/app_start(?:\?.*)?$"
)
_CURRENT_USER_ACCESS_PATH_RE = re.compile(
    r"^/(?:api|edge-api)/bootstrap/(?P<id>[0-9a-fA-F-]+)/current_user_access(?:\?.*)?$"
)
_ACCOUNT_PROFILE_PATH_RE = re.compile(r"^/api/account_profile/?$")
_EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_UUID_RE = re.compile(r'"uuid"\s*:\s*"([0-9a-fA-F-]{36})"')
_KEYED_EMAIL_RE = re.compile(r'"(?:email_address|email)"\s*:\s*"([^"]+)"')
_NEXT_F_FRAME_PREFIX_RE = re.compile(r"^\s*[0-9A-Za-z]+\s*:\s*")
_NEXT_F_PUSH_MARKER = "self.__next_f.push([1,"
_MAX_NEXT_F_SCAN = 256
_MAX_PROFILE_SEARCH_DEPTH = 40
_PLACEHOLDER_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
_CLAUDE_VENDOR_EMAIL_DOMAINS = frozenset({"anthropic.com", "claude.ai"})
# Contact/system mailboxes that appear in claude.ai HTML/JS, not the logged-in user.
_CLAUDE_VENDOR_SYSTEM_LOCALPARTS = frozenset(
    {
        "support",
        "help",
        "hello",
        "noreply",
        "no-reply",
        "no_reply",
        "donotreply",
        "do-not-reply",
        "privacy",
        "legal",
        "press",
        "security",
        "abuse",
        "postmaster",
        "mailer-daemon",
        "info",
        "contact",
        "feedback",
        "team",
        "claude",
        "www",
    }
)

_CLAUDE_RESPONSE_BUFFER: Dict[str, str] = {}
_CLAUDE_RESPONSE_MESSAGE_ID: Dict[str, str] = {}
_CLAUDE_RESPONSE_OVERRIDE_EMITTED: Dict[str, bool] = {}
_CLAUDE_FALLBACK_KEY = "__fallback__"


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
    if _is_claude_web_profile_response(req):
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
    if _is_json_request(req):
        pass
    elif (req.get("direction") or "").lower() == "request" and _is_completion_endpoint(req):
        text = _safe_utf8(payload).lstrip()
        if not text.startswith("{"):
            return None
    else:
        return None
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _text_from_content_blocks(content: Any) -> str:
    if isinstance(content, str) and content.strip():
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if isinstance(block, str) and block.strip():
            parts.append(block.strip())
            continue
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").lower()
        if btype in ("text", "text_delta"):
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


def _extract_prompt(obj: Dict[str, Any]) -> str:
    prompt = obj.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    for key in ("input", "query", "text"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    message = obj.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    if isinstance(message, dict):
        text = _text_from_content_blocks(message.get("content"))
        if text:
            return text
    messages = obj.get("messages")
    if not isinstance(messages, list):
        messages = obj.get("chat_messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            sender = str(msg.get("sender") or msg.get("role") or "").lower()
            if sender and sender not in ("human", "user"):
                continue
            text = _text_from_content_blocks(msg.get("content"))
            if not text and isinstance(msg.get("text"), str):
                text = msg["text"].strip()
            if text:
                return text
    return ""


def _pb_read_varint(buf: bytes, index: int) -> Tuple[Optional[int], int]:
    value = 0
    shift = 0
    while index < len(buf) and shift <= 63:
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
    return None, index


def _pb_iter(buf: bytes) -> List[Tuple[int, int, Union[int, bytes]]]:
    """Length-delimited and varint fields. Stops at a truncated or unknown wire."""
    found: List[Tuple[int, int, Union[int, bytes]]] = []
    index = 0
    size = len(buf or b"")
    while index < size:
        key, index = _pb_read_varint(buf, index)
        if key is None or key <= 0:
            break
        field = key >> 3
        wire = key & 7
        if field <= 0:
            break
        if wire == 0:
            value, index = _pb_read_varint(buf, index)
            if value is None:
                break
            found.append((field, wire, value))
            continue
        if wire == 2:
            length, index = _pb_read_varint(buf, index)
            if length is None or length < 0 or index + length > size:
                break
            found.append((field, wire, buf[index : index + length]))
            index += length
            continue
        if wire == 5:
            if index + 4 > size:
                break
            found.append((field, wire, buf[index : index + 4]))
            index += 4
            continue
        if wire == 1:
            if index + 8 > size:
                break
            found.append((field, wire, buf[index : index + 8]))
            index += 8
            continue
        break
    return found


def _pb_encode_varint(value: int) -> bytes:
    out = bytearray()
    number = int(value)
    while True:
        byte = number & 0x7F
        number >>= 7
        if number:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _pb_encode(fields: List[Tuple[int, int, Union[int, bytes]]]) -> bytes:
    out = bytearray()
    for field, wire, value in fields:
        out += _pb_encode_varint((int(field) << 3) | int(wire))
        if wire == 0:
            out += _pb_encode_varint(int(value))
        elif wire == 2:
            blob = bytes(value)
            out += _pb_encode_varint(len(blob))
            out += blob
        elif wire in (1, 5):
            out += bytes(value)
    return bytes(out)


def _pb_parse(buf: bytes) -> Optional[List[Tuple[int, int, Union[int, bytes]]]]:
    fields = _pb_iter(buf or b"")
    if _pb_encode(fields) != bytes(buf or b""):
        return None
    return fields


def _rewrite_perform_action_prompt(payload: bytes, replacement: str) -> Optional[bytes]:
    """Replace prompt and remove attachment metadata from the action envelope.

    PerformAction field 2 is the user action: fields 1/2 are message lineage
    UUIDs and field 3 is the prompt. File/multimodal metadata follows those
    fields. BLOCK, MASKING and CONSENT must not forward those file references.
    Top-level context is retained so Claude can still accept the rewritten turn.
    """
    top = _pb_parse(payload)
    if top is None:
        return None
    replaced = False
    rebuilt: List[Tuple[int, int, Union[int, bytes]]] = []
    for field, wire, value in top:
        if field != 2 or wire != 2 or not isinstance(value, (bytes, bytearray)):
            rebuilt.append((field, wire, value))
            continue
        inner = _pb_parse(bytes(value))
        if inner is None:
            return None
        new_inner: List[Tuple[int, int, Union[int, bytes]]] = []
        for inner_field, inner_wire, inner_value in inner:
            if inner_field == 3 and inner_wire == 2 and isinstance(inner_value, (bytes, bytearray)):
                new_inner.append((inner_field, inner_wire, replacement.encode("utf-8")))
                replaced = True
            elif inner_field in (1, 2):
                new_inner.append((inner_field, inner_wire, inner_value))
        rebuilt.append((field, wire, _pb_encode(new_inner)))
    if not replaced:
        return None
    return _pb_encode(rebuilt)


def _pb_utf8(blob: bytes) -> str:
    try:
        text = blob.decode("utf-8")
    except Exception:
        return ""
    if not text:
        return ""
    if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in text):
        return ""
    return text


def _claude_rpc_method(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "").split("?", 1)[0].rstrip("/").lower()
    if "/claudeai-rpc/" not in path or "conversationservice/" not in path:
        return ""
    return path.rsplit("/", 1)[-1]


def _perform_action_message(payload: bytes) -> bytes:
    for field, wire, value in _pb_iter(payload or b""):
        if field == 2 and wire == 2 and isinstance(value, (bytes, bytearray)):
            return bytes(value)
    return b""


def _extract_perform_action_prompt(payload: bytes) -> str:
    """User text in ConversationService/PerformAction field 2.3.

    Heartbeats carry locale and timezone only. A real turn nests the prompt
    beside two message uuids.
    """
    action = _perform_action_message(payload)
    if not action:
        return ""
    for field, wire, value in _pb_iter(action):
        if field == 3 and wire == 2 and isinstance(value, (bytes, bytearray)):
            text = _pb_utf8(bytes(value)).strip()
            if text:
                return text
    return ""


def _extract_perform_action_conversation(payload: bytes) -> str:
    for field, wire, value in _pb_iter(payload or b""):
        if field != 1 or wire != 2 or not isinstance(value, (bytes, bytearray)):
            continue
        for inner_field, inner_wire, inner in _pb_iter(bytes(value)):
            if inner_field == 2 and inner_wire == 2 and isinstance(inner, (bytes, bytearray)):
                text = _pb_utf8(bytes(inner)).strip()
                if text:
                    return text
    return ""


def _perform_action_file_refs(payload: bytes) -> Tuple[List[str], bool]:
    """UUID-like file refs below action fields 4+, never message lineage UUIDs."""
    action = _perform_action_message(payload)
    fields = _pb_parse(action) if action else None
    if not fields:
        return [], False
    extras = [
        bytes(value)
        for field, wire, value in fields
        if field not in (1, 2, 3)
        and wire == 2
        and isinstance(value, (bytes, bytearray))
    ]
    refs: List[str] = []
    seen: set[str] = set()

    def walk(blob: bytes, depth: int = 0) -> None:
        if depth > 8 or not blob:
            return
        text = _pb_utf8(blob).strip()
        if _UUID_TEXT_RE.fullmatch(text):
            key = text.lower()
            if key not in seen:
                seen.add(key)
                refs.append(text)
            return
        nested = _pb_parse(blob)
        if nested is None:
            return
        for _field, wire, value in nested:
            if wire == 2 and isinstance(value, (bytes, bytearray)):
                walk(bytes(value), depth + 1)

    for extra in extras:
        walk(extra)
    return refs, bool(extras)


def _perform_action_inline_files(payload: bytes) -> List[Tuple[str, str, bytes]]:
    """Files embedded in PerformAction, not uploaded on a separate URL.

    Desktop sends the bytes in an action field after the prompt. The observed
    shape is field 1 filename, field 3 mime, field 4 content. A few trailing
    bytes make the whole action fail a round-trip parse, so this walks fields
    directly and does not require the action to re-encode.
    """
    action = _perform_action_message(payload)
    if not action:
        return []
    found: List[Tuple[str, str, bytes]] = []
    for field, wire, value in _pb_iter(action):
        if field in (1, 2, 3) or wire != 2 or not isinstance(value, (bytes, bytearray)):
            continue
        item = _inline_upload_parts(bytes(value))
        if item:
            found.append(item)
    return found


def _looks_like_upload_filename(text: str) -> bool:
    name = (text or "").strip()
    if not name or len(name) > 240 or any(ch in name for ch in "\n\r/\\"):
        return False
    if name.startswith(("dgrp_", "cblk_", "msg_")):
        return False
    if _UUID_TEXT_RE.fullmatch(name):
        return False
    return "." in name and not name.startswith(".")


def _inline_upload_parts(blob: bytes) -> Optional[Tuple[str, str, bytes]]:
    name = ""
    mime = ""
    content = b""
    for field, wire, value in _pb_iter(blob):
        if wire != 2 or not isinstance(value, (bytes, bytearray)):
            continue
        raw = bytes(value)
        if field == 4 and raw:
            content = raw
            continue
        text = _pb_utf8(raw).strip()
        if field == 1 and _looks_like_upload_filename(text):
            name = text
        elif field == 3 and text and "/" in text and len(text) < 120 and "\n" not in text:
            mime = text
    if not name or len(content) < 16:
        return None
    return name, mime, content


def _merge_claude_attachments(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            file_id = str(item.get("id") or "").strip()
            if not name and not file_id:
                continue
            key = (name, file_id)
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
    if rows:
        return rows
    return [{"id": "", "size": 0, "name": "", "mime_type": ""}]


def claude_should_skip_policy(req: Dict[str, Any], payload: bytes = b"") -> bool:
    """Input/Output filter는 /completion POST와 새 PerformAction 질문만 대상."""
    if (req.get("direction") or "").lower() != "request":
        return False
    if _is_completion_endpoint(req):
        return False
    if _claude_rpc_method(req) == "performaction" and _extract_perform_action_prompt(payload):
        return False
    return True


def _extract_path_ids(req: Dict[str, Any]) -> Tuple[str, str]:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    m = _PATH_RE.search(path)
    if not m:
        return "", ""
    return m.group("org"), m.group("conv")


def _extract_org_profile_uuid(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    method = str(meta.get("method") or "").upper()
    if method != "GET":
        return ""
    raw_path = str(meta.get("path") or "")
    base_path = raw_path.split("?", 1)[0]
    m = _ORG_PROFILE_PATH_RE.match(base_path)
    if not m:
        return ""
    return m.group("org")


def _is_claude_web_profile_response(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return False

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    method = str(meta.get("method") or "").upper()
    path = str(meta.get("path") or "")
    content_type = str(req.get("content_type") or "").lower()
    if "claude.ai" not in host:
        return False
    if method != "GET":
        return False
    base_path = path.split("?", 1)[0]
    if base_path not in ("/", "/new"):
        return False
    return "text/html" in content_type


def _completion_has_files(obj: Dict[str, Any]) -> bool:
    files = obj.get("files")
    if not isinstance(files, list):
        return False
    for item in files:
        if isinstance(item, str) and item.strip():
            return True
        if isinstance(item, dict):
            uid = str(item.get("file_uuid") or item.get("uuid") or item.get("id") or "").strip()
            if uid:
                return True
    return False


def _is_org_profile_response(req: Dict[str, Any]) -> bool:
    """True for GET /api/organizations/{org_uuid} response. Input/Output filter must not run."""
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    method = str(meta.get("method") or "").upper()
    path = str(meta.get("path") or "")
    if "claude.ai" not in host or method != "GET":
        return False
    base_path = path.split("?", 1)[0]
    return _ORG_PROFILE_PATH_RE.match(base_path) is not None


def _is_bootstrap_app_start_response(req: Dict[str, Any]) -> bool:
    """True for /api/bootstrap/{id}/app_start response (JSON with account). Input/Output filter must not run."""
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    path = str(meta.get("path") or "").strip()
    if "claude.ai" not in host:
        return False
    base_path = path.split("?", 1)[0].strip()
    if base_path and not base_path.startswith("/"):
        base_path = "/" + base_path
    return _BOOTSTRAP_APP_START_PATH_RE.match(base_path) is not None


def _is_completion_endpoint(req: Dict[str, Any]) -> bool:
    """True only for .../chat_conversations/{id}/completion. Only this path may run Input/Output filter.

    Must not match /completion_status (substring "/completion" is a false positive that
    created empty OpenSearch/UI prompt rows after a real completion turn).
    """
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    base_path = path.split("?", 1)[0].rstrip("/")
    return bool(
        re.search(
            r"/api/organizations/[0-9a-fA-F-]+/chat_conversations/[0-9a-fA-F-]+/completion$",
            base_path,
            re.IGNORECASE,
        )
    )


def _extract_web_profile(payload: bytes) -> Tuple[str, str]:
    text = _safe_utf8(payload)
    uuid = ""
    email = ""

    # Claude web pages are often streamed as Next.js flight chunks; parse them first.
    # Use organization uuid so web_profile matches organization_profile (same org uuid).
    chunks = _iter_next_f_push_strings(text)
    if chunks:
        for chunk in chunks:
            json_text = _extract_next_f_frame_json(chunk)
            if not json_text:
                continue
            try:
                parsed = json.loads(json_text)
            except Exception:
                continue
            account = _find_account_dict(parsed, 0)
            if account is None:
                continue
            candidate_email = _extract_account_email(account)
            candidate_org_uuid = _find_organization_uuid(parsed, 0)
            if candidate_org_uuid and not uuid:
                uuid = candidate_org_uuid
            if candidate_email and not email:
                email = candidate_email
            if uuid and email:
                return uuid, email

    if not email:
        for m in _KEYED_EMAIL_RE.finditer(text):
            candidate = m.group(1).strip()
            if _is_real_user_email(candidate):
                email = candidate
                break

    if not uuid:
        # Prefer organization uuid so web_profile matches organization_profile.
        idx = text.find('"organization"')
        if idx != -1:
            m_uuid = _UUID_RE.search(text, idx)
            if m_uuid:
                uuid = m_uuid.group(1)
        if not uuid:
            m_uuid = _UUID_RE.search(text)
            if m_uuid:
                uuid = m_uuid.group(1)

    if not email:
        for m_email in _EMAIL_RE.finditer(text):
            candidate = m_email.group(0).strip()
            if _is_real_user_email(candidate):
                email = candidate
                break

    return uuid, email


def _decode_js_string_literal(literal: str) -> str:
    try:
        return json.loads(literal)
    except Exception:
        try:
            value = ast.literal_eval(literal)
            if isinstance(value, str):
                return value
        except Exception:
            pass
    if len(literal) >= 2:
        return literal[1:-1]
    return literal


def _iter_next_f_push_strings(text: str) -> List[str]:
    out: List[str] = []
    idx = 0
    n = len(text)

    while len(out) < _MAX_NEXT_F_SCAN:
        pos = text.find(_NEXT_F_PUSH_MARKER, idx)
        if pos == -1:
            break
        i = pos + len(_NEXT_F_PUSH_MARKER)
        while i < n and text[i].isspace():
            i += 1
        if i >= n or text[i] not in ("'", '"'):
            idx = pos + len(_NEXT_F_PUSH_MARKER)
            continue

        quote = text[i]
        j = i + 1
        while j < n:
            ch = text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == quote:
                break
            j += 1
        if j >= n:
            break

        literal = text[i : j + 1]
        decoded = _decode_js_string_literal(literal)
        if decoded:
            out.append(decoded)
        idx = j + 1

    return out


def _extract_next_f_frame_json(chunk: str) -> str:
    s = (chunk or "").strip()
    if not s:
        return ""
    if not _NEXT_F_FRAME_PREFIX_RE.match(s):
        return ""
    body = _NEXT_F_FRAME_PREFIX_RE.sub("", s, count=1).lstrip()
    if not body.startswith(("{", "[")):
        return ""
    return body


def _find_account_dict(value: Any, depth: int) -> Optional[Dict[str, Any]]:
    if depth > _MAX_PROFILE_SEARCH_DEPTH:
        return None

    if isinstance(value, dict):
        oauth = value.get("oauth_account")
        if isinstance(oauth, dict):
            return oauth
        account = value.get("account")
        if isinstance(account, dict):
            return account
        if "email_address" in value and ("uuid" in value or "tagged_id" in value or "account_uuid" in value):
            return value
        for child in value.values():
            found = _find_account_dict(child, depth + 1)
            if found is not None:
                return found
        return None

    if isinstance(value, list):
        for item in value:
            found = _find_account_dict(item, depth + 1)
            if found is not None:
                return found

    return None


def _find_organization_uuid(value: Any, depth: int) -> str:
    """Extract organization uuid from parsed payload (same as organization_profile)."""
    if depth > _MAX_PROFILE_SEARCH_DEPTH:
        return ""

    if isinstance(value, dict):
        org = value.get("organization")
        if isinstance(org, dict):
            raw = org.get("uuid")
            if isinstance(raw, str) and raw:
                return raw
        for child in value.values():
            found = _find_organization_uuid(child, depth + 1)
            if found:
                return found
        return ""

    if isinstance(value, list):
        for item in value:
            found = _find_organization_uuid(item, depth + 1)
            if found:
                return found

    return ""


def _is_real_user_email(value: str) -> bool:
    if not isinstance(value, str):
        return False
    email = value.strip()
    if not email or not _EMAIL_RE.fullmatch(email):
        return False
    local, domain = email.rsplit("@", 1)
    domain = domain.lower()
    if domain in _PLACEHOLDER_EMAIL_DOMAINS:
        return False
    if domain in _CLAUDE_VENDOR_EMAIL_DOMAINS:
        local_base = local.lower().split("+", 1)[0]
        if local_base in _CLAUDE_VENDOR_SYSTEM_LOCALPARTS:
            return False
    return True


def _extract_account_email(account: Dict[str, Any]) -> str:
    for key in ("email_address", "email", "account_email", "primary_email"):
        raw = account.get(key)
        if isinstance(raw, str):
            candidate = raw.strip()
            if _is_real_user_email(candidate):
                return candidate
    return ""


def _extract_account_uuid(account: Dict[str, Any]) -> str:
    for key in ("uuid", "account_uuid", "user_uuid", "tagged_id"):
        raw = account.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def _parse_json_any(payload: bytes) -> Tuple[Any, Optional[str]]:
    text = _safe_utf8(payload).strip()
    if not text:
        return None, "empty"
    candidates = [text]
    if not text.startswith(("{", "[")):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    last_err = "parse_failed"
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, str) and parsed.strip().startswith(("{", "[")):
                try:
                    parsed = json.loads(parsed)
                except Exception:
                    pass
            return parsed, None
        except Exception as exc:
            last_err = str(exc)[:160]
    return None, last_err


def _extract_claude_identity(obj: Any) -> Tuple[str, str]:
    uuid = ""
    email = ""
    account: Optional[Dict[str, Any]] = None
    if isinstance(obj, dict):
        oauth = obj.get("oauth_account")
        if isinstance(oauth, dict):
            uuid = _extract_account_uuid(oauth)
            email = _extract_account_email(oauth)
        nested_account = obj.get("account")
        if isinstance(nested_account, dict):
            account = nested_account
        elif account is None:
            account = _find_account_dict(obj, 0)
    else:
        account = _find_account_dict(obj, 0)
    if isinstance(account, dict):
        if not uuid:
            uuid = _extract_account_uuid(account)
        if not email:
            email = _extract_account_email(account)
    return uuid, email


def _register_identity_from_text(req: Dict[str, Any], payload: bytes, *, source: str) -> bool:
    uuid, email = _extract_web_profile(payload)
    if not (uuid or email):
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    cookie_str = str(meta.get("cookie") or "").strip()
    registered = _register_claude_account(req, uuid, email, cookie_str=cookie_str, source=source)
    return bool(registered)


def _nearest_uuid(text: str, pos: int) -> str:
    start = max(0, pos - 4000)
    end = min(len(text), pos + 4000)
    best_uuid = ""
    best_dist = 10**9
    for m in _UUID_RE.finditer(text[start:end]):
        dist = abs((start + m.start()) - pos)
        if dist < best_dist:
            best_dist = dist
            best_uuid = m.group(1)
    return best_uuid


def _extract_primary_next_f_text(text: str) -> str:
    for chunk in _iter_next_f_push_strings(text):
        candidate = (chunk or "").strip()
        if not candidate:
            continue
        # Skip RSC transport frames such as "1a:[...]".
        if _extract_next_f_frame_json(candidate):
            continue
        if len(candidate) < 40:
            continue
        if candidate.count(" ") < 5:
            continue
        return candidate
    return ""


def _extract_ssid_from_cookie(cookie_str: str) -> Optional[str]:
    """Extract __ssid value from Cookie header."""
    if not cookie_str or not isinstance(cookie_str, str):
        return None
    cookie_str = cookie_str.strip()
    m = re.search(r"__ssid=([^;\s]+)", cookie_str)
    if m:
        return m.group(1).strip()
    return None


def _bootstrap_org_id_from_path(path: str) -> str:
    base_path = path.split("?", 1)[0].strip()
    if base_path and not base_path.startswith("/"):
        base_path = "/" + base_path
    for pattern in (_BOOTSTRAP_APP_START_PATH_RE, _CURRENT_USER_ACCESS_PATH_RE):
        m = pattern.match(base_path)
        if m:
            return str(m.group("id") or "")
    return ""


def _claude_session_keys(req: Dict[str, Any], cookie_str: str = "") -> List[str]:
    keys: List[str] = []
    seen: set[str] = set()

    def _add(key: str) -> None:
        key = (key or "").strip()
        if not key or key in seen:
            return
        seen.add(key)
        keys.append(key)

    ssid = _extract_ssid_from_cookie(cookie_str)
    if ssid:
        _add(ssid)

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    org_id, _ = _extract_path_ids(req)
    if not org_id:
        org_id = _bootstrap_org_id_from_path(path)
    if not org_id:
        org_id = _extract_org_profile_uuid(req)
    if org_id:
        _add(f"org:{org_id}")

    client_ip = str(meta.get("client_ip") or "").strip()
    if client_ip:
        _add(f"ip:{client_ip}")
    return keys


def _claude_person_name(value: Any) -> str:
    """Person name from the account object. Organization titles are not names."""
    text = " ".join(str(value or "").split())
    if not text or len(text) > 80 or "@" in text:
        return ""
    lowered = text.lower()
    if lowered.endswith("'s organization") or lowered.endswith(" organization"):
        return ""
    if "/" in text or "\\" in text or lowered.startswith("http"):
        return ""
    return text


def _register_claude_account(
    req: Dict[str, Any],
    uuid: str,
    email: str,
    *,
    cookie_str: str = "",
    source: str = "",
    display_name: str = "",
) -> int:
    uid = uuid or ""
    em = (email or "").strip()
    if em and not _is_real_user_email(em):
        em = ""
    name = _claude_person_name(display_name)
    if not _account_lookup or not (uid or em):
        return 0
    keys = _claude_session_keys(req, cookie_str)
    if not keys:
        return 0
    for key in keys:
        replace = False
        prev = _account_lookup.get("claude", key)
        if prev and prev[1] and not _is_real_user_email(prev[1]):
            # Drop poisoned vendor/support addresses so a later real email can land.
            replace = True
        _account_lookup.set_account(
            "claude", key, uid, em, replace=replace, display_name=name
        )
    return len(keys)


def _claude_cached_name(req: Dict[str, Any]) -> str:
    if not _account_lookup or not hasattr(_account_lookup, "get_result"):
        return ""
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    cookie_str = str(meta.get("cookie") or "").strip()
    for key in _claude_session_keys(req, cookie_str):
        try:
            info = _account_lookup.get_result("claude", key)
        except Exception:
            info = None
        raw = getattr(info, "display_name", "") if info else ""
        if not isinstance(raw, str):
            continue
        name = _claude_person_name(raw)
        if name:
            return name
    return ""


def _lookup_claude_account(req: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    if not _account_lookup:
        return None
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    cookie_str = str(meta.get("cookie") or "").strip()
    for key in _claude_session_keys(req, cookie_str):
        info = _account_lookup.get("claude", key)
        if not info:
            continue
        uid, em = info[0] or "", info[1] or ""
        if em and not _is_real_user_email(em):
            em = ""
        if uid or em:
            return uid, em
    return None


def _wait_for_claude_account(req: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Wait only on the asynchronous response-log path for late profile identity.

    Mirror replay can deliver a completion response milliseconds before the
    organization profile that maps its path org UUID to an email.  The caller
    runs in the output logging thread, so this does not delay policy evaluation
    or the user request.
    """
    if not _account_lookup:
        return None
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    cookie_str = str(meta.get("cookie") or "").strip()
    info = _account_lookup.wait_for(
        "claude",
        _claude_session_keys(req, cookie_str),
        env_name="AIVAX_CLAUDE_ACCOUNT_WAIT_MS",
    )
    if info:
        uid, em = info[0] or "", info[1] or ""
        if em and not _is_real_user_email(em):
            em = ""
        if uid or em:
            logger.info(
                "[Account Info] claude: late profile resolved user_id=%s email=%s",
                uid or "-",
                em or "-",
            )
            return uid, em
    return None


def _is_account_profile_response(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    path = str(meta.get("path") or "").strip()
    if "claude.ai" not in host:
        return False
    base_path = path.split("?", 1)[0].strip()
    if base_path and not base_path.startswith("/"):
        base_path = "/" + base_path
    return _ACCOUNT_PROFILE_PATH_RE.match(base_path) is not None


def _is_current_user_access_response(req: Dict[str, Any]) -> bool:
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    path = str(meta.get("path") or "").strip()
    if "claude.ai" not in host:
        return False
    base_path = path.split("?", 1)[0].strip()
    if base_path and not base_path.startswith("/"):
        base_path = "/" + base_path
    return _CURRENT_USER_ACCESS_PATH_RE.match(base_path) is not None


def _log_account_json_response(req: Dict[str, Any], obj: Any, *, source: str) -> None:
    uuid, email = _extract_claude_identity(obj)
    if not (uuid or email):
        return
    full_name = ""
    if isinstance(obj, dict):
        candidates: List[Dict[str, Any]] = []
        for key in ("account", "oauth_account"):
            candidate = obj.get(key)
            if isinstance(candidate, dict):
                candidates.append(candidate)
        found = _find_account_dict(obj, 0)
        if isinstance(found, dict):
            candidates.append(found)
        for account in candidates:
            full_name = _claude_person_name(account.get("full_name") or account.get("display_name"))
            if full_name:
                break
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    cookie_str = str(meta.get("cookie") or "").strip()
    ssid = _extract_ssid_from_cookie(cookie_str)
    if ssid:
        req["_claude_ssid"] = ssid
    logger.info(
        "[Account Info] claude: %s uuid=%s email=%s name=%s (path=%s)%s",
        source,
        uuid,
        email,
        full_name,
        path,
        " __ssid=%s" % ssid if ssid else "",
    )
    _register_claude_account(
        req, uuid, email, cookie_str=cookie_str, source=source, display_name=full_name
    )


def _ingest_claude_account_response(req: Dict[str, Any], payload: bytes, *, source: str) -> None:
    parsed, err = _parse_json_any(payload)
    if parsed is not None:
        _log_account_json_response(req, parsed, source=source)
        return
    _register_identity_from_text(req, payload, source=f"{source}_text")


def _log_web_profile(req: Dict[str, Any], payload: bytes) -> None:
    uuid, email = _extract_web_profile(payload)
    profile_hint = _extract_primary_next_f_text(_safe_utf8(payload))
    if profile_hint:
        req["_claude_profile_hint"] = profile_hint
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    cookie_str = str(meta.get("cookie") or "").strip()
    ssid = _extract_ssid_from_cookie(cookie_str)
    if ssid:
        req["_claude_ssid"] = ssid
    logger.info(
        "[Account Info] claude: web_profile uuid=%s email=%s hint_len=%d (path=%s)%s",
        uuid,
        email,
        len(profile_hint),
        path,
        " __ssid=%s" % ssid if ssid else "",
    )
    _register_claude_account(req, uuid, email, cookie_str=cookie_str, source="web_profile")


def _log_org_profile(req: Dict[str, Any], obj: Dict[str, Any]) -> None:
    profile_uuid = _extract_org_profile_uuid(req)
    if not profile_uuid:
        return
    uuid = str(obj.get("uuid") or "")
    name = str(obj.get("name") or "")
    email = ""
    m = _EMAIL_RE.search(name)
    if m:
        candidate = m.group(0).strip()
        if _is_real_user_email(candidate):
            email = candidate
    req["_claude_org_uuid"] = uuid
    req["_claude_org_name"] = name
    req["_claude_org_email"] = email
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    cookie_str = str(meta.get("cookie") or "").strip()
    ssid = _extract_ssid_from_cookie(cookie_str)
    if ssid:
        req["_claude_ssid"] = ssid
    logger.info(
        "[Account Info] claude: organization_profile uuid=%s email=%s (name=%s)%s",
        uuid,
        email,
        name,
        " __ssid=%s" % ssid if ssid else "",
    )
    _register_claude_account(req, uuid, email, cookie_str=cookie_str, source="org_profile")


def _log_bootstrap_app_start(req: Dict[str, Any], obj: Any) -> None:
    """Extract account from bootstrap/app_start JSON and register for lookup."""
    _log_account_json_response(req, obj, source="bootstrap_app_start")


def _extract_message_id(obj: Dict[str, Any]) -> str:
    turn = obj.get("turn_message_uuids")
    if isinstance(turn, dict):
        human = turn.get("human_message_uuid")
        if isinstance(human, str) and human:
            return human
    parent = obj.get("parent_message_uuid")
    if isinstance(parent, str) and parent:
        return parent
    return ""


def _extract_assistant_message_id_from_sse(obj: Dict[str, Any]) -> str:
    message = obj.get("message")
    if isinstance(message, dict):
        for key in ("uuid", "id"):
            value = message.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _extract_response_text_delta(obj: Dict[str, Any]) -> str:
    if str(obj.get("type") or "") != "content_block_delta":
        return ""
    delta = obj.get("delta")
    if not isinstance(delta, dict):
        return ""
    if str(delta.get("type") or "") != "text_delta":
        return ""
    text = delta.get("text")
    if isinstance(text, str):
        return text
    return ""


def _resolve_stream_key(req: Dict[str, Any]) -> str:
    _, conversation_id = _extract_path_ids(req)
    if conversation_id:
        return conversation_id
    if len(_CLAUDE_RESPONSE_BUFFER) == 1:
        return next(iter(_CLAUDE_RESPONSE_BUFFER))
    if len(_CLAUDE_RESPONSE_MESSAGE_ID) == 1:
        return next(iter(_CLAUDE_RESPONSE_MESSAGE_ID))
    return _CLAUDE_FALLBACK_KEY


def _set_output_ctx(req: Dict[str, Any], key: str) -> None:
    session_id = key if key != _CLAUDE_FALLBACK_KEY else ""
    req["_claude_output_session_id"] = session_id
    req["_claude_output_message_id"] = str(_CLAUDE_RESPONSE_MESSAGE_ID.get(key, "") or "")


def _set_extract_final(req: Dict[str, Any], is_final: bool) -> None:
    req["_extract_final"] = bool(is_final)


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


def _parse_sse_data_json(text: str) -> Optional[Dict[str, Any]]:
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


def _extract_text_from_conversation_snapshot(obj: Dict[str, Any]) -> str:
    messages = obj.get("chat_messages")
    if not isinstance(messages, list):
        return ""

    leaf_id = obj.get("current_leaf_message_uuid")
    target: Optional[Dict[str, Any]] = None
    if isinstance(leaf_id, str) and leaf_id:
        for msg in messages:
            if isinstance(msg, dict) and msg.get("uuid") == leaf_id:
                target = msg
                break
    if target is None:
        for msg in reversed(messages):
            if isinstance(msg, dict) and str(msg.get("sender") or "") == "assistant":
                target = msg
                break
    if not isinstance(target, dict):
        return ""

    content = target.get("content")
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text.strip())
    return "\n\n".join(chunks).strip()


_CLAUDE_RPC_DUMP_COUNT = 0
_CLAUDE_RPC_DUMP_LIMIT = 200
# Heartbeats are 9 bytes every few seconds and would exhaust the dump limit.
_CLAUDE_RPC_DUMP_MIN_BYTES = 64
# conversation uuid -> (prompt request_id, seen_at, prompt text)
_CLAUDE_RPC_PROMPTS: Dict[str, Tuple[str, float, str]] = {}
# Desktop StreamTimeline keeps one cblk and then sends the answer as field-2
# deltas. Field 9 only carries a short prefix. Same cblk must stay on the
# prompt's output row, or the UI keeps that prefix and drops the rest.
_CLAUDE_CBLK_TEXT: Dict[str, Tuple[str, float]] = {}
_CLAUDE_CBLK_PARENT: Dict[str, Tuple[str, float]] = {}
# Some model variants announce the conversation in one StreamTimeline DATA
# frame and emit the assistant on a different cblk in later frames.
# Value is (prompt request_id, that prompt's seen time). The seen time is the
# PerformAction time, not the latest frame time: a long-poll stays open across
# turns, and refreshing it on every frame hides a newer prompt.
_CLAUDE_STREAM_PARENT: Dict[str, Tuple[str, float]] = {}
# stream request_id -> (conversation uuid, seen). Desktop deltas omit the uuid
# but stay on the same StreamTimeline, so the latest prompt can still be found.
_CLAUDE_STREAM_CONV: Dict[str, Tuple[str, float]] = {}
# After a tool call the visible answer often continues on a new StreamTimeline
# request that never repeats the conversation id. Keep the latest prompt for
# that client so the prose replaces the tool transcript on the same row.
_CLAUDE_CLIENT_PARENT: Dict[str, Tuple[str, float, str]] = {}
_CLAUDE_TOOL_CBLK: Dict[str, float] = {}
_CLAUDE_CBLK_TTL_SEC = 1800.0
_CLAUDE_CLIENT_PARENT_TTL_SEC = 600.0
_CLAUDE_TOOL_NAMES = frozenset(
    {
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "WebSearch",
        "WebFetch",
        "Skill",
        "TodoWrite",
        "Task",
        "NotebookEdit",
        "PowerShell",
    }
)
_UUID_TEXT_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _remember_claude_rpc_prompt(req: Dict[str, Any], payload: bytes) -> None:
    if (req.get("direction") or "").lower() != "request":
        return
    if _claude_rpc_method(req) != "performaction":
        return
    conversation_id = _extract_perform_action_conversation(payload)
    request_id = str(req.get("request_id") or "").strip()
    prompt = _extract_perform_action_prompt(payload)
    if not conversation_id or not request_id or not prompt:
        return
    now = time.time()
    stale = [key for key, item in _CLAUDE_RPC_PROMPTS.items() if now - item[1] > 1800]
    for key in stale:
        _CLAUDE_RPC_PROMPTS.pop(key, None)
    _CLAUDE_RPC_PROMPTS[conversation_id] = (request_id, now, prompt)
    client_ip = _claude_client_key(req)
    if client_ip:
        _CLAUDE_CLIENT_PARENT[client_ip] = (request_id, now, conversation_id)


def _lookup_claude_rpc_prompt(conversation_id: str) -> str:
    item = _CLAUDE_RPC_PROMPTS.get(conversation_id or "")
    if not item:
        return ""
    request_id, seen, _prompt = item
    if time.time() - seen > 1800:
        _CLAUDE_RPC_PROMPTS.pop(conversation_id, None)
        return ""
    return request_id


def _claude_rpc_prompt_text(conversation_id: str) -> str:
    item = _CLAUDE_RPC_PROMPTS.get(conversation_id or "")
    if not item:
        return ""
    return item[2]


def _claude_latest_prompt(
    conversation_id: str, client_ip: str, now: float
) -> Tuple[str, float]:
    """Latest PerformAction this frame may attach a new assistant block to."""
    if conversation_id:
        parent = _lookup_claude_rpc_prompt(conversation_id)
        item = _CLAUDE_RPC_PROMPTS.get(conversation_id or "")
        if parent and item:
            return parent, float(item[1])
    if client_ip:
        remembered = _CLAUDE_CLIENT_PARENT.get(client_ip)
        if remembered and now - remembered[1] <= _CLAUDE_CLIENT_PARENT_TTL_SEC:
            prompt_id, seen, prompt_conversation = remembered
            if not conversation_id or conversation_id == prompt_conversation:
                return prompt_id, float(seen)
    return "", 0.0


def _dump_claude_rpc_body(req: Dict[str, Any], payload: bytes) -> None:
    """Optional capture while the new RPC answer shape is being checked."""
    global _CLAUDE_RPC_DUMP_COUNT
    if os.environ.get("AIVAX_CLAUDE_RPC_DUMP") != "1":
        return
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    if "claudeai-rpc/" not in path:
        return
    if _CLAUDE_RPC_DUMP_COUNT >= _CLAUDE_RPC_DUMP_LIMIT:
        return
    raw = payload or b""
    if len(raw) < _CLAUDE_RPC_DUMP_MIN_BYTES:
        return
    _CLAUDE_RPC_DUMP_COUNT += 1
    try:
        out_dir = os.environ.get("AIVAX_CLAUDE_RPC_DUMP_DIR") or "/home1/aivax/ai_engine/var/claude_rpc"
        os.makedirs(out_dir, exist_ok=True)
        stem = (
            f"{int(time.time())}_{_CLAUDE_RPC_DUMP_COUNT}_"
            f"{(req.get('direction') or 'na')}_{len(raw)}"
        )
        with open(os.path.join(out_dir, stem + ".bin"), "wb") as handle:
            handle.write(raw[:2_000_000])
        path_only = path.split("?", 1)[0]
        note = "\n".join(
            [
                f"direction={req.get('direction') or ''}",
                f"path={path_only}",
                f"content_type={req.get('content_type') or ''}",
                f"client={meta.get('client_ip') or meta.get('client_host') or ''}",
                f"bytes={len(raw)}",
            ]
        )
        with open(os.path.join(out_dir, stem + ".txt"), "w", encoding="utf-8") as handle:
            handle.write(note)
    except Exception:
        logger.debug("claude rpc dump failed", exc_info=True)


def _inflate_rpc_blob(blob: bytes) -> bytes:
    if len(blob) >= 2 and blob[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(blob)
        except Exception:
            return blob
    for window in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            return zlib.decompress(blob, window)
        except Exception:
            continue
    # Connect's compression flag does not identify the codec in-frame. The web
    # app has used gzip, raw deflate, Brotli and zstd across releases.
    try:
        import brotli

        return brotli.decompress(blob)
    except Exception:
        pass
    try:
        import zstandard as zstd

        return zstd.ZstdDecompressor().decompress(blob)
    except Exception:
        pass
    return blob


def _connect_messages(body: bytes) -> List[bytes]:
    """Split a Connect stream into protobuf messages. Unary proto is one message."""
    raw = body or b""
    if len(raw) < 5 or raw[0] > 3:
        return [raw] if raw else []
    messages: List[bytes] = []
    index = 0
    while index + 5 <= len(raw):
        flags = raw[index]
        if flags > 3:
            break
        length = int.from_bytes(raw[index + 1 : index + 5], "big")
        if length > 8 * 1024 * 1024 or index + 5 + length > len(raw):
            break
        payload = raw[index + 5 : index + 5 + length]
        index += 5 + length
        if flags & 0x02:
            continue
        if flags & 0x01:
            payload = _inflate_rpc_blob(payload)
        if payload:
            messages.append(payload)
    return messages or ([raw] if raw else [])


def _rpc_uuid(buf: bytes, depth: int = 0) -> str:
    """Find the conversation UUID even when a release nests the envelope."""
    if depth > 8:
        return ""
    fallback = ""
    children: List[bytes] = []
    for _field, wire, value in _pb_iter(buf):
        if wire != 2 or not isinstance(value, (bytes, bytearray)):
            continue
        blob = bytes(value)
        text = _pb_utf8(blob).strip()
        if _UUID_TEXT_RE.match(text):
            if text in _CLAUDE_RPC_PROMPTS:
                return text
            if not fallback:
                fallback = text
        else:
            children.append(blob)
    for child in children:
        nested = _rpc_uuid(child, depth + 1)
        if nested in _CLAUDE_RPC_PROMPTS:
            return nested
        if nested and not fallback:
            fallback = nested
    return fallback


def _timeline_id_text(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return True
    if value.startswith(("dgrp_", "cblk_", "msg_")):
        return True
    return _UUID_TEXT_RE.match(value) is not None


def _timeline_entity(node: bytes) -> Optional[Tuple[str, str, str]]:
    """Return (block id, field-9 snapshot, field-2 delta) for one message entity."""
    block_id = ""
    snapshot = ""
    delta = ""
    for field, wire, value in _pb_iter(node):
        if wire != 2 or not isinstance(value, (bytes, bytearray)):
            continue
        text = _pb_utf8(bytes(value))
        if field == 1 and text.startswith(("cblk_", "msg_")):
            block_id = text
        elif field == 9:
            cleaned = text.strip()
            if cleaned and not _timeline_id_text(cleaned):
                snapshot = cleaned
        elif field == 2 and text and not _timeline_id_text(text):
            delta = text
    if not snapshot and not (block_id and delta):
        return None
    return block_id, snapshot, delta


def _claude_client_key(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("client_ip") or meta.get("clientIp") or "").strip()


def _timeline_tool_block_id(node: bytes, depth: int = 0) -> str:
    """Cblk id of a tool call or tool result. Empty when the node is prose."""
    if depth > 6 or not node:
        return ""
    block_id = ""
    tool = False
    nested = ""
    for field, wire, value in _pb_iter(node):
        if wire != 2 or not isinstance(value, (bytes, bytearray)):
            continue
        raw = bytes(value)
        text = _pb_utf8(raw).strip()
        if field == 1 and text.startswith(("cblk_", "msg_")):
            block_id = text
        elif text.startswith("toolu_"):
            tool = True
        elif field == 15 and text in _CLAUDE_TOOL_NAMES:
            tool = True
        elif not text:
            found = _timeline_tool_block_id(raw, depth + 1)
            if found:
                tool = True
                nested = nested or found
    if not tool:
        return ""
    return block_id or nested or "tool"


def _remember_tool_block(node: bytes, now: float) -> bool:
    block_id = _timeline_tool_block_id(node)
    if not block_id:
        return False
    if block_id != "tool":
        _CLAUDE_TOOL_CBLK[block_id] = now
    return True


def _timeline_entities(message: bytes) -> List[Tuple[str, str, str]]:
    """Web snapshots are field 1.1.5. Desktop deltas are field 1.2 or 1.5.

    Field 9 is a growing prefix. The rest of a desktop answer arrives as
    field 2 chunks on the same cblk. Workspace and sandbox nodes have neither.
    Tool calls and file-read results share that shape, so they are recorded
    and left out of the assistant answer.
    """
    found: List[Tuple[str, str, str]] = []
    now = time.time()

    def consider(node: bytes) -> None:
        if _remember_tool_block(node, now):
            return
        entity = _timeline_entity(node)
        if entity and entity[0] in _CLAUDE_TOOL_CBLK:
            return
        if entity:
            found.append(entity)

    for f1, w1, v1 in _pb_iter(message):
        if f1 != 1 or w1 != 2 or not isinstance(v1, (bytes, bytearray)):
            continue
        node = bytes(v1)
        if _remember_tool_block(node, now):
            continue
        consider(node)
        for f2, w2, v2 in _pb_iter(node):
            if f2 not in (1, 2, 5) or w2 != 2 or not isinstance(v2, (bytes, bytearray)):
                continue
            child = bytes(v2)
            if _remember_tool_block(child, now):
                continue
            consider(child)
            if f2 != 1:
                continue
            for f3, w3, v3 in _pb_iter(child):
                if f3 not in (2, 5) or w3 != 2 or not isinstance(v3, (bytes, bytearray)):
                    continue
                consider(bytes(v3))
    return found


def _purge_claude_cblk(now: float) -> None:
    for store in (
        _CLAUDE_CBLK_TEXT,
        _CLAUDE_CBLK_PARENT,
        _CLAUDE_STREAM_PARENT,
        _CLAUDE_STREAM_CONV,
        _CLAUDE_CLIENT_PARENT,
    ):
        expired = [key for key, item in store.items() if now - item[1] > _CLAUDE_CBLK_TTL_SEC]
        for key in expired:
            store.pop(key, None)
    expired_tools = [
        key for key, seen in _CLAUDE_TOOL_CBLK.items() if now - seen > _CLAUDE_CBLK_TTL_SEC
    ]
    for key in expired_tools:
        _CLAUDE_TOOL_CBLK.pop(key, None)


def _merge_timeline_entity(block_id: str, snapshot: str, delta: str) -> str:
    """Fold one desktop chunk into the block text. Snapshots replace, deltas append."""
    now = time.time()
    _purge_claude_cblk(now)
    key = block_id or snapshot[:80]
    text = _CLAUDE_CBLK_TEXT.get(key, ("", 0.0))[0]
    if snapshot:
        if (
            not text
            or snapshot.startswith(text)
            or (text in snapshot and len(snapshot) >= len(text))
        ):
            text = snapshot
        elif text.startswith(snapshot) or snapshot in text:
            pass
        elif len(snapshot) > len(text):
            text = snapshot
    if delta:
        if not text:
            text = delta
        elif text.endswith(delta):
            pass
        elif len(delta) >= 8 and delta in text:
            pass
        else:
            text += delta
    _CLAUDE_CBLK_TEXT[key] = (text, now)
    return text


def _extract_stream_timeline_answer(req: Dict[str, Any], payload: bytes) -> str:
    """Assistant text on ConversationService/StreamTimeline.

    The classic /completion SSE path is unchanged. This only reads the new
    Connect stream. Heartbeats and locale frames carry no prose, so they
    return empty and are not logged.
    """
    now = time.time()
    _purge_claude_cblk(now)
    stream_id = str(req.get("request_id") or "").strip()
    conversation_id = ""
    entities: List[Tuple[str, str, str]] = []
    for message in _connect_messages(payload):
        if not conversation_id:
            conversation_id = _rpc_uuid(message)
        entities.extend(_timeline_entities(message))
    # A uuid that is not a prompt we have seen must not steal the stream.
    # Heartbeats and field-2 deltas often omit the conversation entirely.
    parsed_conversation = conversation_id
    known_conversation = (
        parsed_conversation
        if parsed_conversation and _lookup_claude_rpc_prompt(parsed_conversation)
        else ""
    )
    if known_conversation and stream_id:
        _CLAUDE_STREAM_CONV[stream_id] = (known_conversation, now)
    elif not parsed_conversation and stream_id:
        remembered_conv = _CLAUDE_STREAM_CONV.get(stream_id)
        if remembered_conv and _lookup_claude_rpc_prompt(remembered_conv[0]):
            known_conversation = remembered_conv[0]
    if known_conversation:
        req["_claude_output_session_id"] = known_conversation

    client_ip = _claude_client_key(req)
    # Unknown uuid: keep it so a different conversation is not glued to this client.
    parent_conversation = parsed_conversation or known_conversation
    latest_id, latest_seen = _claude_latest_prompt(parent_conversation, client_ip, now)
    stream_parent = ""
    stream_seen = 0.0
    if stream_id:
        remembered_stream = _CLAUDE_STREAM_PARENT.get(stream_id)
        if remembered_stream:
            stream_parent, stream_seen = remembered_stream
    foreign_conversation = bool(parsed_conversation and not known_conversation)
    default_parent = "" if foreign_conversation else stream_parent
    if latest_id and not foreign_conversation and (
        not stream_parent or latest_seen >= stream_seen
    ):
        default_parent = latest_id
    if (
        default_parent
        and stream_id
        and latest_id
        and default_parent == latest_id
        and not foreign_conversation
    ):
        _CLAUDE_STREAM_PARENT[stream_id] = (latest_id, latest_seen)

    def parent_for_block(block_id: str) -> str:
        if block_id:
            remembered = _CLAUDE_CBLK_PARENT.get(block_id)
            if remembered:
                return remembered[0]
        return default_parent

    for block_id, _snapshot, _delta in entities:
        if not block_id or block_id in _CLAUDE_CBLK_PARENT or not default_parent:
            continue
        _CLAUDE_CBLK_PARENT[block_id] = (default_parent, now)

    prompt_text = _claude_rpc_prompt_text(known_conversation).strip()
    answers: List[Tuple[str, str]] = []
    for block_id, snapshot, delta in entities:
        if block_id and block_id in _CLAUDE_TOOL_CBLK:
            continue
        text = _merge_timeline_entity(block_id, snapshot, delta).strip()
        if text and text != prompt_text:
            answers.append((text, parent_for_block(block_id)))
    # Snapshots grow and deltas append. Keep the longest block in this frame,
    # and log it on the prompt that owned that block when it first appeared.
    text = ""
    text_parent = ""
    if answers:
        text, text_parent = max(answers, key=lambda item: len(item[0]))
    if text_parent:
        req["_claude_ui_message_id"] = text_parent
    if not text:
        req["_extract_final"] = False
        return ""
    # The long-poll may resend the entity. Hold until it settles.
    req["_claude_response_growing"] = True
    req["_extract_final"] = False
    return text


def extract(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    _dump_claude_rpc_body(req, payload)
    _remember_claude_rpc_prompt(req, payload)
    direction = (req.get("direction") or "").lower()
    method = _claude_rpc_method(req)
    # New web client. The /completion JSON path below is unchanged.
    if direction == "request" and method == "performaction":
        return _extract_perform_action_prompt(payload)
    if direction == "response" and method == "streamtimeline":
        return _extract_stream_timeline_answer(req, payload)
    if not _is_completion_endpoint(req):
        # /, /new HTML embeds account markers — must run before early return.
        if _is_claude_web_profile_response(req) and payload:
            _log_web_profile(req, payload)
        if _is_account_profile_response(req) and payload:
            _ingest_claude_account_response(req, payload, source="account_profile")
        if _is_current_user_access_response(req) and payload:
            _ingest_claude_account_response(req, payload, source="current_user_access")
        if _is_org_profile_response(req) and payload:
            parsed, err = _parse_json_any(payload)
            if isinstance(parsed, dict):
                _log_org_profile(req, parsed)
            elif err:
                _register_identity_from_text(req, payload, source="org_profile_text")
        if _is_bootstrap_app_start_response(req) and payload:
            parsed, err = _parse_json_any(payload)
            if parsed is not None:
                _log_bootstrap_app_start(req, parsed)
            else:
                _register_identity_from_text(req, payload, source="bootstrap_app_start_text")
        req["_extract_final"] = False
        return ""

    if _is_json_response(req):
        try:
            obj = json.loads(_safe_utf8(payload))
        except Exception:
            return _safe_utf8(payload)
        if isinstance(obj, dict):
            _log_org_profile(req, obj)
            text = _extract_text_from_conversation_snapshot(obj)
            if text:
                return text
        return ""

    if _is_sse_response(req):
        raw = _safe_utf8(payload)
        event_name, data_text = _parse_sse_payload(raw)
        key = _resolve_stream_key(req)

        if data_text == "[DONE]":
            merged = _CLAUDE_RESPONSE_BUFFER.pop(key, "")
            _CLAUDE_RESPONSE_OVERRIDE_EMITTED.pop(key, None)
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

        event_type = str(obj.get("type") or event_name or "")
        if event_type == "message_start":
            message_id = _extract_assistant_message_id_from_sse(obj)
            if message_id:
                _CLAUDE_RESPONSE_MESSAGE_ID[key] = message_id
            _set_extract_final(req, False)
            return ""

        if event_type == "content_block_delta":
            chunk = _extract_response_text_delta(obj)
            if chunk:
                _CLAUDE_RESPONSE_BUFFER[key] = _CLAUDE_RESPONSE_BUFFER.get(key, "") + chunk
            _set_extract_final(req, False)
            return ""

        if event_type == "message_stop":
            merged = _CLAUDE_RESPONSE_BUFFER.pop(key, "")
            _CLAUDE_RESPONSE_OVERRIDE_EMITTED.pop(key, None)
            _set_output_ctx(req, key)
            _set_extract_final(req, bool(merged))
            return merged

        _set_extract_final(req, False)
        return ""

    obj = _parse_request_json(req, payload)
    if obj is None:
        return None
    prompt = _extract_prompt(obj)
    has_files = bool(_completion_has_files(obj))
    if prompt:
        return prompt
    if has_files:
        return "사용자가 파일을 업로드하였습니다."
    return ""


def build_policy_api_payload(
    req: Dict[str, Any],
    payload: bytes,
    extracted_text: str,
) -> Optional[Dict[str, Any]]:
    direction = (req.get("direction") or "").lower()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}

    if direction == "request" and _claude_rpc_method(req) == "performaction":
        prompt = _extract_perform_action_prompt(payload)
        if not prompt:
            return None
        conversation_id = _extract_perform_action_conversation(payload)
        inline_atts = save_perform_action_inline_files(
            _perform_action_inline_files(payload)
        )
        file_refs, has_file_extras = _perform_action_file_refs(payload)
        attachments = _merge_claude_attachments(
            inline_atts,
            build_rpc_policy_attachments(req, file_refs, has_file_extras),
        )
        user_id = ""
        email = ""
        info = _lookup_claude_account(req)
        if info:
            user_id = info[0] or ""
            email = info[1] or ""
        user_name = _claude_cached_name(req)
        if user_id or email or user_name:
            logger.info(
                "[Account Info] claude: rpc user_id=%s email=%s name=%s path=%s",
                user_id or "-",
                email or "-",
                user_name or "-",
                str(meta.get("path") or "").split("?", 1)[0],
            )
        logger.info(
            "claude_prompt rpc extracted_len=%d session=%s",
            len(prompt),
            conversation_id or "-",
        )
        rpc_payload = {
            "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
            "prompt": prompt,
            "encoding": False,
            "user_id": user_id,
            "email": email,
            "ai_service": AI_SERVICE_CLAUDE,
            "client_host": str(meta.get("client_ip") or meta.get("client_host") or ""),
            "session_id": conversation_id,
            "attachments": attachments,
            "message_id": "",
        }
        if user_name:
            rpc_payload["user_name"] = user_name
        return rpc_payload

    if direction == "response":
        org_id, conversation_id = _extract_path_ids(req)
        session_id = str(req.get("_claude_output_session_id") or conversation_id or "")
        message_id = str(req.get("_claude_output_message_id") or "")
        if not message_id:
            obj = _parse_sse_data_json(_safe_utf8(payload))
            if obj is not None:
                message_id = _extract_assistant_message_id_from_sse(obj)
        user_id = ""
        email = ""
        info = _wait_for_claude_account(req)
        if info:
            user_id = info[0] or ""
            email = info[1] or ""
        response_name = _claude_cached_name(req)
        response_payload = {
            "llm_output": extracted_text,
            "user_id": user_id,
            "email": email,
            "ai_service": AI_SERVICE_CLAUDE,
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

    org_id, conversation_id = _extract_path_ids(req)
    if not conversation_id:
        conversation_id = str(obj.get("conversation_id") or "")
    prompt = _extract_prompt(obj).strip()
    if not prompt:
        fallback = (extracted_text or "").strip()
        # Protocol envelopes contain parent_message_uuid/tool metadata and
        # must not be scanned as user content.
        if fallback and not fallback.startswith(("{", "[")):
            prompt = fallback
    if not prompt and _completion_has_files(obj):
        prompt = "사용자가 파일을 업로드하였습니다."
    if not prompt:
        return None

    user_id = str(obj.get("user_id") or "")
    email = str(obj.get("email") or "")
    if not user_id or not email:
        info = _lookup_claude_account(req)
        if info:
            if not user_id:
                user_id = info[0]
            if not email:
                email = info[1]
    # org/cookie/IP are correlators only.  The account is the profile
    # identity (email / account uuid) looked up from those keys.
    user_name = _claude_cached_name(req)
    if user_id or email or user_name:
        logger.info(
            "[Account Info] claude: completion user_id=%s email=%s name=%s path=%s",
            user_id or "-",
            email or "-",
            user_name or "-",
            str(meta.get("path") or ""),
        )

    completion_payload = {
        "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
        "prompt": prompt,
        "encoding": False,
        "user_id": user_id,
        "email": email,
        "ai_service": AI_SERVICE_CLAUDE,
        "client_host": str(meta.get("host") or ""),
        "session_id": conversation_id,
        "attachments": build_policy_attachments(req, obj, payload),
        "message_id": _extract_message_id(obj),
    }
    if user_name:
        completion_payload["user_name"] = user_name
    return completion_payload


# Gemini와 동일: 안내문·지시 사이 긴 줄바꿈(UI 휴리스틱, 보장 아님).
_BLOCK_SYSINST_VERTICAL_GAP = "\n" * 1


def _mutate_user_content_blocks(content: Any, replacement_text: str) -> Tuple[Any, int]:
    changed = 0
    if isinstance(content, str):
        if content != replacement_text:
            return replacement_text, 1
        return content, 0
    if not isinstance(content, list):
        return content, 0

    new_list: List[Any] = []
    text_written = False
    for block in content:
        if isinstance(block, str):
            new_list.append(replacement_text)
            text_written = True
            if block != replacement_text:
                changed += 1
            continue
        if not isinstance(block, dict):
            new_list.append(block)
            continue
        if _is_file_content_block(block):
            changed += 1
            continue
        d = dict(block)
        btype = str(d.get("type") or "").lower()
        if btype in ("text", "text_delta") or isinstance(d.get("text"), str):
            if d.get("text") != replacement_text:
                changed += 1
            d["text"] = replacement_text
            text_written = True
            new_list.append(d)
            continue
        if isinstance(d.get("content"), str):
            if d["content"] != replacement_text:
                changed += 1
            d["content"] = replacement_text
            text_written = True
            new_list.append(d)
            continue
        new_list.append(block)

    if not text_written and replacement_text:
        new_list.insert(0, {"type": "text", "text": replacement_text})
        changed += 1
    return new_list, changed


def _rewrite_last_user_prompt_obj(obj: Dict[str, Any], replacement_text: str) -> int:
    prompt = obj.get("prompt")
    if isinstance(prompt, str):
        if prompt != replacement_text:
            obj["prompt"] = replacement_text
            return 1
        return 0

    for key in ("input", "query", "text"):
        if isinstance(obj.get(key), str):
            if obj[key] != replacement_text:
                obj[key] = replacement_text
                return 1
            return 0

    message = obj.get("message")
    if isinstance(message, str):
        if message != replacement_text:
            obj["message"] = replacement_text
            return 1
        return 0
    if isinstance(message, dict):
        new_content, changed = _mutate_user_content_blocks(message.get("content"), replacement_text)
        if changed > 0:
            message["content"] = new_content
            return changed

    messages = obj.get("messages")
    if not isinstance(messages, list):
        messages = obj.get("chat_messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            sender = str(msg.get("sender") or msg.get("role") or "").lower()
            if sender and sender not in ("human", "user"):
                continue
            if isinstance(msg.get("text"), str):
                if msg["text"] != replacement_text:
                    msg["text"] = replacement_text
                    return 1
                return 0
            new_content, changed = _mutate_user_content_blocks(msg.get("content"), replacement_text)
            if changed > 0:
                msg["content"] = new_content
                return changed
    return 0


def _rewrite_completion_payload(
    obj: Dict[str, Any],
    replacement_text: str,
    *,
    allow_empty: bool = False,
) -> Optional[Tuple[bytes, int]]:
    if not allow_empty and not (replacement_text or "").strip():
        return None

    had_files = _completion_has_files(obj)
    changed = _rewrite_last_user_prompt_obj(obj, replacement_text)
    strip_cnt = strip_completion_attachments(obj)
    if changed <= 0 and strip_cnt <= 0 and not had_files:
        return None
    if changed <= 0 and (strip_cnt > 0 or had_files):
        changed = 1

    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, changed


def rewrite_prompt_for_block(
    req: Dict[str, Any],
    payload: bytes,
    user_prompt: str,
    system_instruction: str,
) -> Optional[Tuple[bytes, str]]:
    """
    BLOCK 전용: 차단 안내문(user_prompt) 뒤에 system_instruction을 붙여 JSON의 prompt(등) 한 칸에 넣는다.
    Gemini와 동일하게 안내문 아래 긴 빈 줄 뒤에 지시문을 둔다.
    """
    if _claude_rpc_method(req) == "performaction":
        sys_t = (system_instruction or "").strip()
        user_t = (user_prompt or "").strip()
        combined = f"{user_t}\n{_BLOCK_SYSINST_VERTICAL_GAP}{sys_t}" if sys_t and user_t else (user_t or sys_t)
        if not combined:
            return None
        rewritten = _rewrite_perform_action_prompt(payload, combined)
        if rewritten is None:
            return None
        return rewritten, "adapter:claude_policy_block_sysinstruct"
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

    out = _rewrite_completion_payload(obj, combined, allow_empty=False)
    if out is None:
        return None
    encoded, _ = out
    return encoded, "adapter:claude_policy_block_sysinstruct"


def rewrite_prompt_masking(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    """MASKING 전용: completion prompt 치환 + files[] strip. RPC는 질문 필드만 바꾼다."""
    if _claude_rpc_method(req) == "performaction":
        if not (replacement_text or "").strip():
            return None
        rewritten = _rewrite_perform_action_prompt(payload, replacement_text)
        if rewritten is None:
            return None
        return rewritten, "adapter:claude_policy_masking"
    obj = _parse_request_json(req, payload)
    if obj is None:
        return None
    out = _rewrite_completion_payload(
        obj,
        replacement_text,
        allow_empty=bool((replacement_text or "").strip()),
    )
    if out is None:
        return None
    encoded, _ = out
    return encoded, "adapter:claude_policy_masking"


def rewrite_prompt(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    policy_code: str,
) -> Optional[Tuple[bytes, str]]:
    if _claude_rpc_method(req) == "performaction":
        if not replacement_text:
            return None
        rewritten = _rewrite_perform_action_prompt(payload, replacement_text)
        if rewritten is None:
            return None
        code = (policy_code or "masking").strip().lower()
        return rewritten, f"adapter:claude_policy_{code}"
    obj = _parse_request_json(req, payload)
    if obj is None:
        return None

    if not replacement_text:
        return None

    out = _rewrite_completion_payload(
        obj,
        replacement_text,
        allow_empty=bool((replacement_text or "").strip()),
    )
    if out is None:
        return None
    encoded, _ = out
    code = (policy_code or "masking").strip().lower()
    return encoded, f"adapter:claude_policy_{code}"


_COMPLETION_FILE_KEYS = ("files", "file_uuids", "attachments", "attachment_ids")


def _last_user_content_list(obj: Dict[str, Any]) -> Optional[List[Any]]:
    message = obj.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        return message["content"]
    for key in ("messages", "chat_messages"):
        messages = obj.get(key)
        if not isinstance(messages, list):
            continue
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            sender = str(msg.get("sender") or msg.get("role") or "").lower()
            if sender and sender not in ("human", "user"):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                return content
            return None
    return None


def consent_attachment_state(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    """File references of the blocked turn, so 동의 can send them with the prompt.

    The upload itself already reached Claude. Only the turn's references were
    removed on BLOCK, so the consent turn re-adds exactly those references.
    """
    if (req.get("direction") or "").lower() != "request":
        return None
    if _claude_rpc_method(req) == "performaction":
        action = _perform_action_message(payload)
        inner = _pb_parse(action) if action else None
        if not inner:
            return None
        extras = [item for item in inner if item[0] not in (1, 2, 3)]
        return {"kind": "rpc", "extras": extras} if extras else None
    if not _is_completion_endpoint(req):
        return None
    obj = _parse_request_json(req, payload)
    if not isinstance(obj, dict):
        return None
    top = {
        key: json.loads(json.dumps(obj[key]))
        for key in _COMPLETION_FILE_KEYS
        if isinstance(obj.get(key), list) and obj[key]
    }
    content = _last_user_content_list(obj) or []
    blocks = [json.loads(json.dumps(b)) for b in content if _is_file_content_block(b)]
    if not top and not blocks:
        return None
    return {"kind": "completion", "top": top, "blocks": blocks}


def restore_consent_attachments(
    req: Dict[str, Any],
    payload: bytes,
    state: Any,
    prompt: str = "",
) -> Optional[bytes]:
    if not isinstance(state, dict):
        return None
    if state.get("kind") == "rpc" and _claude_rpc_method(req) == "performaction":
        top = _pb_parse(payload)
        if top is None:
            return None
        extras = list(state.get("extras") or [])
        rebuilt: List[Tuple[int, int, Union[int, bytes]]] = []
        restored = False
        for field, wire, value in top:
            if field == 2 and wire == 2 and isinstance(value, (bytes, bytearray)) and not restored:
                inner = _pb_parse(bytes(value))
                if inner is None:
                    return None
                kept = [item for item in inner if item[0] in (1, 2, 3)]
                rebuilt.append((field, wire, _pb_encode(kept + extras)))
                restored = True
            else:
                rebuilt.append((field, wire, value))
        return _pb_encode(rebuilt) if restored else None
    if state.get("kind") == "completion" and _is_completion_endpoint(req):
        obj = _parse_request_json(req, payload)
        if not isinstance(obj, dict):
            return None
        for key, value in (state.get("top") or {}).items():
            obj[key] = value
        blocks = list(state.get("blocks") or [])
        if blocks:
            content = _last_user_content_list(obj)
            if content is None:
                return None
            content[:0] = blocks
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return None


def extract_full_body(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    """Suricata eve / Deep INLINE response body: SSE assistant text, or account extract.

    Deep INLINE sets `_eve_full_body=true`, so account/profile responses must be
    handled here (not only in extract()). Uses no process-global SSE buffers.
    """
    direction = (req.get("direction") or "").lower()
    if direction == "response" and not _is_completion_endpoint(req):
        # Deep INLINE always sets _eve_full_body, so the desktop/web answer on
        # StreamTimeline never reaches extract() unless we return it here.
        if _claude_rpc_method(req) == "streamtimeline":
            return extract(req, payload) or ""
        # Reuse extract() account registration side effects.
        extract(req, payload)
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
        event_type = str(obj.get("type") or "")
        if event_type == "message_start":
            message_id = _extract_assistant_message_id_from_sse(obj)
            if message_id:
                req["_claude_output_message_id"] = message_id
        elif event_type == "content_block_delta":
            chunk = _extract_response_text_delta(obj)
            if chunk:
                chunks.append(chunk)

    if chunks:
        req["_extract_final"] = True
        return "".join(chunks)
    return None


def extract_session_id(req: Dict[str, Any], payload: bytes) -> str:
    if _claude_rpc_method(req) == "performaction":
        return _extract_perform_action_conversation(payload)
    _, conversation_id = _extract_path_ids(req)
    if conversation_id:
        return conversation_id

    direction = (req.get("direction") or "").lower()
    if direction == "request":
        obj = _parse_request_json(req, payload)
        if isinstance(obj, dict):
            value = obj.get("conversation_id")
            if isinstance(value, str) and value:
                return value

    if direction == "response" and _is_sse_response(req):
        key = _resolve_stream_key(req)
        if key and key != _CLAUDE_FALLBACK_KEY:
            return key
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
    return str(obj.get("type") or "") == "message_stop"


def _rewrite_delta(obj: Dict[str, Any], replacement_text: str, state_key: str) -> int:
    if str(obj.get("type") or "") != "content_block_delta":
        return 0

    delta = obj.get("delta")
    if not isinstance(delta, dict):
        return 0

    changed = 0
    emitted = bool(_CLAUDE_RESPONSE_OVERRIDE_EMITTED.get(state_key, False))
    delta_type = str(delta.get("type") or "")

    if delta_type == "thinking_delta":
        if isinstance(delta.get("thinking"), str) and delta.get("thinking"):
            delta["thinking"] = ""
            changed += 1
        return changed

    if delta_type != "text_delta":
        return 0

    if emitted:
        target_text = ""
    else:
        target_text = replacement_text
        emitted = True

    current_text = delta.get("text")
    if not isinstance(current_text, str):
        current_text = ""
    if current_text != target_text:
        delta["text"] = target_text
        changed += 1

    _CLAUDE_RESPONSE_OVERRIDE_EMITTED[state_key] = emitted
    return changed


def _rebuild_sse_block(event_name: str, data_text: str) -> str:
    lines: List[str] = []
    if event_name:
        lines.append(f"event: {event_name}")
    lines.append(f"data: {data_text}")
    return "\n".join(lines)


def _rewrite_assistant_json(obj: Dict[str, Any], replacement_text: str, state_key: str) -> int:
    event_type = str(obj.get("type") or "")
    if event_type == "content_block_delta":
        return _rewrite_delta(obj, replacement_text, state_key)
    if event_type in (
        "message_start",
        "content_block_start",
        "content_block_stop",
        "message_delta",
        "ping",
    ):
        return 0
    if event_type == "message_stop":
        _CLAUDE_RESPONSE_OVERRIDE_EMITTED.pop(state_key, None)
        return 0

    emitted = bool(_CLAUDE_RESPONSE_OVERRIDE_EMITTED.get(state_key, False))
    target = "" if emitted else replacement_text
    changed = 0

    completion = obj.get("completion")
    if isinstance(completion, str) and completion != target:
        obj["completion"] = target
        changed += 1
        if target:
            emitted = True

    for key in ("text", "content"):
        value = obj.get(key)
        if isinstance(value, str) and value and value != target:
            obj[key] = target
            changed += 1
            if target:
                emitted = True
        elif isinstance(value, list):
            new_list, block_changed = _mutate_user_content_blocks(value, target or replacement_text)
            if block_changed > 0:
                obj[key] = new_list
                changed += block_changed
                if replacement_text:
                    emitted = True

    message = obj.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            new_list, block_changed = _mutate_user_content_blocks(
                content, target or replacement_text
            )
            if block_changed > 0:
                message["content"] = new_list
                changed += block_changed
                if replacement_text:
                    emitted = True
        elif isinstance(content, str) and content != target:
            message["content"] = target
            changed += 1
            if target:
                emitted = True

    if changed > 0:
        _CLAUDE_RESPONSE_OVERRIDE_EMITTED[state_key] = emitted
    return changed


def rewrite_response(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
) -> Optional[Tuple[bytes, str]]:
    if not replacement_text:
        return None

    state_key = _resolve_stream_key(req)
    raw = _safe_utf8(payload)

    if _is_sse_response(req):
        if raw.strip() == "[DONE]":
            _CLAUDE_RESPONSE_OVERRIDE_EMITTED.pop(state_key, None)
            return None
        blocks = [block for block in raw.replace("\r\n", "\n").split("\n\n") if block.strip()]
        if not blocks:
            blocks = [raw]
        out_blocks: List[str] = []
        total_changed = 0
        for block in blocks:
            event_name, data_text = _parse_sse_payload(block)
            if data_text == "[DONE]":
                _CLAUDE_RESPONSE_OVERRIDE_EMITTED.pop(state_key, None)
                out_blocks.append(block.strip())
                continue
            try:
                obj = json.loads(data_text) if data_text.lstrip().startswith("{") else None
            except Exception:
                obj = None
            if not isinstance(obj, dict):
                out_blocks.append(block)
                continue
            changed = _rewrite_assistant_json(obj, replacement_text, state_key)
            if str(obj.get("type") or "") == "message_stop":
                _CLAUDE_RESPONSE_OVERRIDE_EMITTED.pop(state_key, None)
            if changed > 0:
                total_changed += changed
                encoded_json = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                out_blocks.append(_rebuild_sse_block(event_name, encoded_json))
            else:
                out_blocks.append(block)
        if total_changed <= 0:
            return None
        return ("\n\n".join(out_blocks) + "\n\n").encode("utf-8"), "adapter:claude_policy_block_message"

    if _is_json_response(req):
        try:
            obj = json.loads(raw)
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        changed = _rewrite_assistant_json(obj, replacement_text, state_key)
        if changed <= 0:
            return None
        encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return encoded, "adapter:claude_policy_block_message"

    return None


def clear_response_override_state(session_id: str) -> None:
    key = (session_id or "").strip()
    if not key:
        return
    _CLAUDE_RESPONSE_OVERRIDE_EMITTED.pop(key, None)
    _CLAUDE_RESPONSE_BUFFER.pop(key, None)
    _CLAUDE_RESPONSE_MESSAGE_ID.pop(key, None)
