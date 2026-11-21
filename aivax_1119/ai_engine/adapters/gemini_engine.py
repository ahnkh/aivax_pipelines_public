"""
Gemini Adapter
Extract request prompt text from form field `f.req`, and from the logged-in
`geminiweb-pa` `/v1/processSession` protobuf (`req0___data__`).
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
from html.parser import HTMLParser
import logging
import os
import re
import threading
import time
import zlib
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import parse_qs, parse_qsl, quote, unquote, unquote_plus, urlencode

from ai_service_codes import AI_SERVICE_GEMINI
from adapters.gemini_upload import (
    POLICY_ATTACHMENTS_FINAL,
    build_policy_attachments,
    claim_recent_gemini_attachments,
    normalize_upload,
    strip_f_req_attachments,
    _extract_gemini_ttl_segments,
    _strip_parsed_f_req_inner,
)

try:
    import account_lookup as _account_lookup
except ImportError:
    _account_lookup = None


_MAX_PROMPTS = 8
_MAX_DEPTH = 20
_XSSI_PREFIX = b")]}'"

# User turn in f.req inner JSON: [["<prompt>",0|1|2,null,...]]
# Pro Extended / thinking modes may use 1 (or rarely 2) instead of classic 0.
_GEMINI_F_REQ_USER_PROMPT_RE = re.compile(
    r'\[\[\s*(?:\\"|")((?:\\.|[^"\\])*)(?:\\"|")\s*,\s*[0-2]\s*,\s*null',
    re.IGNORECASE,
)

# Account page: only these response paths skip output filter and optionally log account.
_GEMINI_ACCOUNT_HOST_SUBSTR = "gemini.google.com"
# Logged-in Gemini web turns leave StreamGenerate and use this private API.
_GEMINI_WEB_PA_HOST = "geminiweb-pa.clients6.google.com"
_GEMINI_PROCESS_SESSION_PATH = "/v1/processsession"
_GEMINI_CONV_ID_RE = re.compile(r"c_[0-9a-f]{8,64}\Z")
_GEMINI_MSG_ID_RE = re.compile(r"r_[0-9a-f]{8,64}\Z")
# Browserchannel batches frames as req0___data__, req1___data__, ...
_PROCESS_SESSION_DATA_KEY_RE = re.compile(r"req\d+___data__\Z")
_PROCESS_SESSION_DUMP_LIMIT = 20
_process_session_dump_count = 0
_GEMINI_ACCOUNT_PATH_PREFIXES = ("/", "/app")
_GEMINI_ACCOUNT_RPC_ID = "o30O0e"
_GOOGLE_ACCOUNTS_HOST_SUBSTR = "accounts.google.com"
_GOOGLE_LIST_ACCOUNTS_PATH = "/listaccounts"
logger = logging.getLogger(__name__)

_GEMINI_RESPONSE_OVERRIDE_EMITTED: Dict[str, bool] = {}

# Pro Extended may finish the answer on a later StreamGenerate HTTP than the
# prompt turn. Map conversation id (c_*) → prompt request_id for UI merge.
_GEMINI_SESSION_PARENT: Dict[str, Tuple[str, float]] = {}
_GEMINI_SESSION_PARENT_LOCK = threading.Lock()
_GEMINI_SESSION_PARENT_TTL_SEC = 3600.0
# processSession keeps one gsessionid for the context POST, the prompt POST,
# and the long-poll that carries the answer. A logged-in context POST may
# include c_*. A private browser sends c_* only on the answer, so the prompt
# request_id is remembered on the gsession until that answer arrives.
_GEMINI_GSESSION_CONV: Dict[str, Tuple[str, float]] = {}
_GEMINI_GSESSION_PROMPT: Dict[str, Tuple[str, float]] = {}
_GEMINI_GSESSION_CONV_LOCK = threading.Lock()
_GEMINI_EMPTY_DUMP_DIR = os.environ.get(
    "AIVAX_GEMINI_EMPTY_DUMP_DIR",
    "/home/juyonglee/AIVAX/inline_engine/aivax/evidence/PROVE/I350_BYPASS_1G/empty_gemini",
)


def _is_gemini_app_response_path(path: str) -> bool:
    """True if path is /, /app, or /app/{id} (conversation). Excludes /app/foo.css.map etc."""
    if not path:
        return False
    path = path.split("?")[0].strip().lower().rstrip("/") or "/"
    if path == "/" or path == "/app":
        return True
    if not path.startswith("/app/"):
        return False
    # First segment after /app/ must exist and have no dot (conversation id, not static file)
    rest = path[5:]
    segment = rest.split("/")[0].strip()
    return bool(segment and "." not in segment)


def _is_account_page_response(req: Dict[str, Any], payload: bytes) -> bool:
    """True if response is gemini app root/conversation page or typography CSS blob; do not run output filter."""
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = (str(meta.get("host") or "")).lower()
    path = (str(meta.get("path") or "")).strip().lower() or "/"
    if _GEMINI_ACCOUNT_HOST_SUBSTR not in host and "bard.google.com" not in host:
        return False
    if _is_gemini_app_response_path(path):
        return True
    # Fallback: response body is the big typography CSS (avoids output filter even if path missed)
    if payload and len(payload) > 500:
        raw = _safe_utf8(payload)
        if "--gem-sys-typography-type-scale" in raw and "font-name" in raw:
            return True
    return False


def _gemini_person_name(value: Any) -> str:
    """Display name from a known account field. Emails, ids, and URLs are not names."""
    if not isinstance(value, str):
        return ""
    text = " ".join(html.unescape(value).split())
    if not text or len(text) > 80 or "@" in text or text.isdigit():
        return ""
    lowered = text.lower()
    if "/" in text or "\\" in text or lowered.startswith("http") or lowered.startswith("//"):
        return ""
    return text


_GEMINI_ACCOUNT_LABEL_RE = re.compile(
    r"""aria-label=["']Google (?:Account|계정):\s*(.*?)\s*\(([^)]+@[^)]+)\)["']""",
    re.IGNORECASE | re.DOTALL,
)


def _extract_account_from_html(text: str) -> Optional[Dict[str, str]]:
    """Extract email (oPEP7c), gaia_id (S06Grb) from Gemini HTML-like body. No f_sid (changes often)."""
    if not text or len(text) < 50:
        return None
    email = gaia_id = None
    # oPEP7c: WIZ_global_data / JSON object form
    m = re.search(r'["\']oPEP7c["\']\s*:\s*["\']([^"\']+)["\']', text)
    if not m:
        # WIZ array form: ["oPEP7c","user@example.com"]
        m = re.search(r'\[["\']oPEP7c["\']\s*,\s*["\']([^"\']+@[^"\']+)["\']', text)
    if not m:
        # JS-escaped JSON inside HTML strings: \"oPEP7c\":\"email\"
        m = re.search(
            r'\\["\']oPEP7c\\["\']\s*:\\["\']([^"\'\\]+@[^"\'\\]+)\\["\']',
            text,
        )
    if m:
        email = m.group(1).strip()
        if email and "@" not in email:
            email = None
    # S06Grb: Gaia ID (object / array / escaped)
    m = re.search(r'["\']S06Grb["\']\s*:\s*["\']?([0-9-]+)["\']?', text)
    if not m:
        m = re.search(r'\[["\']S06Grb["\']\s*,\s*["\']?([0-9-]+)["\']?', text)
    if not m:
        m = re.search(r'\\["\']S06Grb\\["\']\s*:\\?["\']?([0-9-]+)', text)
    if m:
        gaia_id = m.group(1).strip()
    name = ""
    label = _GEMINI_ACCOUNT_LABEL_RE.search(text)
    if label:
        label_name = _gemini_person_name(label.group(1))
        label_email = label.group(2).strip()
        if label_name and (not email or label_email.lower() == email.lower()):
            name = label_name
    if email or gaia_id:
        out = {"email": email or "", "gaia_id": gaia_id or ""}
        if name:
            out["name"] = name
        return out
    return None


def _is_google_list_accounts_response(req: Dict[str, Any]) -> bool:
    """Return True only for the Google account-list response used by Gemini."""
    if (req.get("direction") or "").lower() != "response":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    path = str(meta.get("path") or "").split("?", 1)[0].lower().rstrip("/")
    return _GOOGLE_ACCOUNTS_HOST_SUBSTR in host and path == _GOOGLE_LIST_ACCOUNTS_PATH


def _is_gemini_account_rpc_response(req: Dict[str, Any]) -> bool:
    """Return True for Gemini's signed-in profile batchexecute response."""
    if (req.get("direction") or "").lower() != "response":
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    path = str(meta.get("path") or "")
    if _GEMINI_ACCOUNT_HOST_SUBSTR not in host and "bard.google.com" not in host:
        return False
    if "batchexecute" not in path.lower():
        return False
    return bool(
        re.search(
            rf"(?:[?&]rpcids=)(?:[^&#,]*,)*{re.escape(_GEMINI_ACCOUNT_RPC_ID)}(?:,|&|#|$)",
            path,
        )
    )


def _extract_account_from_gemini_rpc(payload: bytes) -> Optional[Dict[str, str]]:
    """Extract the signed-in profile from Gemini's ``o30O0e`` response.

    The RPC wraps a ``me`` record inside a size-prefixed ``wrb.fr`` frame. The
    profile's first field is the Gaia ID, and its account-selector records carry
    the matching email. Restricting extraction to this RPC and record shape
    prevents email addresses in prompts or model output from becoming identity.
    """
    if not payload or b"wrb.fr" not in payload:
        return None

    email_re = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

    def find_me_record(value: Any) -> Optional[List[Any]]:
        if not isinstance(value, list):
            return None
        if len(value) >= 3 and value[0] == "me" and isinstance(value[2], list):
            return value
        for child in value:
            found = find_me_record(child)
            if found is not None:
                return found
        return None

    def find_matching_email(value: Any, gaia_id: str) -> str:
        if not isinstance(value, list):
            return ""
        if len(value) >= 2 and isinstance(value[0], list) and isinstance(value[1], str):
            flags = value[0]
            email = value[1].strip()
            record_gaia = str(flags[8] or "").strip() if len(flags) > 8 else ""
            if email_re.fullmatch(email) and record_gaia == gaia_id:
                return email
        for child in value:
            found = find_matching_email(child, gaia_id)
            if found:
                return found
        return ""

    for frame in _iter_wrb_frames(payload):
        try:
            outer = json.loads(frame)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(outer, list):
            continue
        for item in outer:
            if not (
                isinstance(item, list)
                and len(item) > 2
                and item[0] == "wrb.fr"
                and item[1] == _GEMINI_ACCOUNT_RPC_ID
                and isinstance(item[2], str)
            ):
                continue
            try:
                inner = json.loads(item[2])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            me_record = find_me_record(inner)
            if me_record is None:
                continue
            profile = me_record[2]
            gaia_id = str(profile[0] or "").strip() if profile else ""
            if gaia_id.startswith("-"):
                gaia_id = gaia_id.lstrip("-")
            if not gaia_id.isdigit() or not (10 <= len(gaia_id) <= 30):
                continue
            email = find_matching_email(profile, gaia_id)
            if email:
                return {"email": email, "gaia_id": gaia_id}
    return None


def _extract_account_from_list_accounts(text: str) -> Optional[Dict[str, str]]:
    """Extract the active email/Gaia ID from accounts.google.com/ListAccounts JSON.

    The response is XSSI-prefixed and contains one or more ``gaia.l.a`` records.
    Record fields 3 and 10 are the email and Gaia ID; field 14, when present,
    marks a signed-out account with 1, so an active account is preferred.
    """
    raw = (text or "").lstrip()
    if not raw:
        return None
    if raw.startswith(")]}'"):
        raw = raw[4:].lstrip("\r\n ")

    try:
        data: Any = json.loads(raw)
        # Some captures contain the JSON response as one encoded JSON string.
        if isinstance(data, str):
            data = json.loads(data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    records: List[List[Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            if value and value[0] == "gaia.l.a":
                records.append(value)
                return
            for child in value:
                walk(child)
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)

    walk(data)
    if not records:
        return None

    def account_info(record: List[Any]) -> Optional[Dict[str, str]]:
        email = str(record[3] or "").strip() if len(record) > 3 else ""
        gaia_id = str(record[10] or "").strip() if len(record) > 10 else ""
        name = _gemini_person_name(record[2]) if len(record) > 2 else ""
        if email and "@" not in email:
            email = ""
        if gaia_id.startswith("-"):
            gaia_id = gaia_id.lstrip("-")
        if gaia_id and not gaia_id.isdigit():
            gaia_id = ""
        if name and email and name.lower() == email.lower():
            name = ""
        if not email and not gaia_id:
            return None
        info = {"email": email, "gaia_id": gaia_id}
        if name:
            info["name"] = name
        return info

    # Chromium's ListAccounts schema uses 0 for a signed-in record at index 14.
    active = [r for r in records if len(r) <= 14 or r[14] in (0, "0", False, None)]
    for record in active + records:
        info = account_info(record)
        if info:
            return info
    return None


def _extract_account_from_response(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, str]]:
    text = _safe_utf8(payload) if payload else ""
    if _is_google_list_accounts_response(req):
        return _extract_account_from_list_accounts(text)
    if _is_gemini_account_rpc_response(req):
        return _extract_account_from_gemini_rpc(payload)
    return _extract_account_from_html(text)


def _extract_psid_sid_from_cookie(cookie_str: str) -> Optional[str]:
    """Extract __Secure-1PSID or SID value from Cookie header. Prefer __Secure-1PSID."""
    if not cookie_str or not isinstance(cookie_str, str):
        return None
    cookie_str = cookie_str.strip()
    # __Secure-1PSID=value or SID=value (value: alphanumeric, dots, hyphens, etc.)
    m = re.search(r"__Secure-1PSID=([^;\s]+)", cookie_str)
    if m:
        return m.group(1).strip()
    m = re.search(r"SID=([^;\s]+)", cookie_str, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def extract_account_info(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, str]]:
    """Account-page: merge email/gaia_id from HTML and psid/sid from meta.cookie for logging."""
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    cookie_str = str(meta.get("cookie") or "").strip()
    if cookie_str.upper() == "[REDACTED]":
        cookie_str = ""
    psid_sid = _extract_psid_sid_from_cookie(cookie_str)

    # Deep INLINE tees app HTML as unit_type=json_body. Mirror mode can also
    # correlate the accounts.google.com/ListAccounts response with the PSID.
    account_info = _extract_account_from_response(req, payload)
    if account_info is None and not psid_sid:
        return None
    out = dict(account_info) if account_info else {}
    out.setdefault("email", "")
    out.setdefault("gaia_id", "")
    if psid_sid:
        out["psid"] = psid_sid
    return out


def _register_account_from_body_if_present(req: Dict[str, Any], payload: bytes) -> bool:
    """Cookie psid + HTML/JSON body에서 gaia_id·email 추출 시 account_lookup 등록."""
    if not _account_lookup:
        return False
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    cookie_str = str(meta.get("cookie") or "").strip()
    if cookie_str.upper() == "[REDACTED]":
        cookie_str = ""
    psid = _extract_psid_sid_from_cookie(cookie_str) or ""
    if not psid:
        return False
    account_info = _extract_account_from_response(req, payload)
    if not account_info:
        return False
    email = (account_info.get("email") or "").strip()
    gaia_id = (account_info.get("gaia_id") or "").strip()
    if gaia_id.startswith("-"):
        gaia_id = gaia_id.lstrip("-")
    if not email and not gaia_id:
        return False
    client_ip = str(meta.get("client_ip") or "").strip()
    _account_lookup.set_account(
        "gemini",
        psid,
        gaia_id or "",
        email or "",
        client_ip=client_ip,
        display_name=_gemini_person_name(account_info.get("name") or ""),
    )
    return True


def _gemini_cached_name(psid: str) -> str:
    if not psid or not _account_lookup or not hasattr(_account_lookup, "get_result"):
        return ""
    try:
        info = _account_lookup.get_result("gemini", psid)
    except Exception:
        return ""
    raw = getattr(info, "display_name", "") if info else ""
    if not isinstance(raw, str):
        return ""
    return _gemini_person_name(raw)


def _safe_utf8(data: Union[bytes, bytearray, str]) -> str:
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _looks_like_token(text: str) -> bool:
    if len(text) < 128:
        return False
    if any(ch.isspace() for ch in text):
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_+=/.")
    allowed_count = sum((1 for ch in text if ch in allowed))
    return (allowed_count / max(1, len(text))) > 0.95


def _is_gemini_internal_prompt(text: str) -> bool:
    """TTL 첨부 경로·내부 ID — 사용자 입력 프롬프트가 아님 (ChatGPT file-only → \"\" 와 동일 처리)."""
    t = (text or "").strip()
    if not t:
        return False
    if "/contrib_service/" in t or "ttl_1d" in t:
        return True
    if t.startswith("c_"):
        return True
    return False



def _purge_gemini_session_parents(now: float) -> None:
    expired = [
        k
        for k, (_, ts) in _GEMINI_SESSION_PARENT.items()
        if now - ts > _GEMINI_SESSION_PARENT_TTL_SEC
    ]
    for k in expired:
        _GEMINI_SESSION_PARENT.pop(k, None)


def register_gemini_turn_parent(session_id: str, parent_request_id: str) -> None:
    """Remember prompt HTTP request_id for conversation c_* (Pro Extended merge)."""
    sid = (session_id or "").strip()
    rid = (parent_request_id or "").strip()
    if not sid.startswith("c_") or not rid:
        return
    now = time.monotonic()
    with _GEMINI_SESSION_PARENT_LOCK:
        _purge_gemini_session_parents(now)
        _GEMINI_SESSION_PARENT[sid] = (rid, now)


def _remember_stream_turn_parent(req: Dict[str, Any], session_id: str) -> None:
    """Bind a conversation id that this turn saw first.

    A new chat's c_* id arrives on the thinking stub, before any reply.
    The visible answer often comes on a later StreamGenerate. Remembering
    the stub's request_id lets that answer join the prompt row. An id that
    already has a parent is left alone.
    """
    sid = (session_id or "").strip()
    rid = str(req.get("request_id") or "").strip()
    if not sid.startswith("c_") or not rid:
        return
    if resolve_gemini_parent_request_id(sid):
        return
    register_gemini_turn_parent(sid, rid)


def resolve_gemini_parent_request_id(session_id: str) -> str:
    sid = (session_id or "").strip()
    if not sid.startswith("c_"):
        return ""
    now = time.monotonic()
    with _GEMINI_SESSION_PARENT_LOCK:
        _purge_gemini_session_parents(now)
        entry = _GEMINI_SESSION_PARENT.get(sid)
        if not entry:
            return ""
        return entry[0]


def _dump_process_session_without_prompt(req: Dict[str, Any], payload: bytes) -> None:
    """Keep a few large no-prompt frames sent right after an upload (file turn debug)."""
    global _process_session_dump_count
    if _process_session_dump_count >= _PROCESS_SESSION_DUMP_LIMIT:
        return
    try:
        import file_upload_manager as _fum_dump

        recent = _fum_dump.lookup_recent_saved_upload_ids(prefix="", limit=1, max_age_sec=60.0)
    except Exception:
        recent = []
    if not recent:
        return
    _process_session_dump_count += 1
    _dump_empty_gemini_stream_body(req, payload, "process_session_no_prompt")


def _dump_empty_gemini_stream_body(req: Dict[str, Any], payload: bytes, reason: str) -> None:
    """Persist StreamGenerate bodies that yielded no extract (Pro Extended debug)."""
    try:
        os.makedirs(_GEMINI_EMPTY_DUMP_DIR, mode=0o755, exist_ok=True)
        rid = str(req.get("request_id") or "unknown").replace("/", "_")[:64]
        meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        path = str(meta.get("path") or "").split("?")[0].replace("/", "_")[:80]
        name = f"{int(time.time())}_{rid}_{reason}{path}.bin"
        out = os.path.join(_GEMINI_EMPTY_DUMP_DIR, name)
        with open(out, "wb") as fh:
            fh.write(payload or b"")
        logger.info(
            "gemini empty extract dumped path=%s bytes=%d reason=%s req=%s",
            out,
            len(payload or b""),
            reason,
            rid,
        )
    except Exception:
        logger.exception("gemini empty dump failed")


def _maybe_remap_gemini_ui_message_id(req: Dict[str, Any], session_id: str) -> None:
    parent = resolve_gemini_parent_request_id(session_id)
    rid = str(req.get("request_id") or "").strip()
    if parent and rid and parent != rid:
        req["_gemini_ui_message_id"] = parent
        logger.info(
            "[gemini] ui_message_id_remap parent=%s request_id=%s session=%s",
            parent,
            rid,
            session_id,
        )


def _append_prompt(prompts: List[str], text: str) -> None:
    normalized = (text or "").strip()
    if not normalized:
        return
    if _is_gemini_internal_prompt(normalized):
        return
    if _looks_like_token(normalized):
        return
    if normalized not in prompts:
        prompts.append(normalized)


def _collect_prompts(value: Any, prompts: List[str], depth: int = 0) -> None:
    if depth > _MAX_DEPTH or len(prompts) >= _MAX_PROMPTS:
        return

    if isinstance(value, str):
        # Bare strings are too noisy in Gemini payloads (locale, ids, tokens).
        return

    if isinstance(value, dict):
        for key in ("prompt", "text", "query", "input"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                _append_prompt(prompts, candidate)
        for item in value.values():
            _collect_prompts(item, prompts, depth + 1)
        return

    if isinstance(value, list):
        # Gemini f.req payload often has prompt in tuple-like nodes:
        # ["<prompt>", 0, null, ...]
        if len(value) >= 2 and isinstance(value[0], str) and not isinstance(value[1], str):
            _append_prompt(prompts, value[0])
        for item in value:
            _collect_prompts(item, prompts, depth + 1)


def _sanitize_f_req_raw(raw: str) -> str:
    """f.req 내부 제어문자 제거 — gemini_upload 파서와 동일 (첨부파일 포함 요청 JSON 파싱)."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw or "")


def _loads_maybe_json(raw: str) -> Optional[Any]:
    text = (raw or "").strip()
    if not text:
        return None
    for candidate in (text, _sanitize_f_req_raw(text)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            if "%" in candidate:
                try:
                    return json.loads(unquote_plus(candidate))
                except Exception:
                    pass
    return None


def _normalize_gemini_text(text: str) -> str:
    if not text:
        return ""
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    out = out.replace("\\n", "\n").replace("\\t", "\t")
    out = out.replace("\\!", "!")
    # Fenced code blocks: strip ``` fence markers but keep content
    out = re.sub(r"```[a-zA-Z0-9]*\n?", "", out)
    # Inline code: strip backticks, keep content
    out = re.sub(r"`([^`\n]+)`", r"\1", out)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", out)
    out = out.replace("**", "")
    out = re.sub(r"(?m)^\s*#{1,6}\s*", "", out)
    out = re.sub(r"(?m)^\s*>\s?", "", out)
    out = re.sub(r"(?m)^\s*[-*]{3,}\s*$", "", out)
    out = re.sub(r"(?m)^\s*[*+-]\s+", "", out)
    out = re.sub(r"(?mi)^\s*\[image of [^\]]+\]\s*$", "", out)
    out = re.sub(r"(?mi)^\s*image of .*diagram.*$", "", out)
    out = re.sub(r"(?m)^(\d+)\.\s{2,}", r"\1. ", out)
    out = re.sub(r"'([^']*)'", lambda m: "'" + m.group(1).strip() + "'", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _strip_xssi_prefix(payload: bytes) -> bytes:
    data = payload.lstrip()
    if data.startswith(_XSSI_PREFIX):
        nl = data.find(b"\n")
        if nl == -1:
            return b""
        return data[nl + 1 :]
    # Handle multi-line XSSI prefix: ) \n ] \n } ' \n
    if data[:1] == b")":
        prefix = bytearray()
        idx = 0
        while idx < min(len(data), 30) and len(prefix) < len(_XSSI_PREFIX):
            b = data[idx]
            if b in (0x20, 0x09, 0x0D, 0x0A):
                idx += 1
                continue
            prefix.append(b)
            idx += 1
        if bytes(prefix) == _XSSI_PREFIX:
            while idx < len(data) and data[idx] in (0x20, 0x09, 0x0D, 0x0A):
                idx += 1
            return data[idx:]
    return data


def _iter_wrb_frames(payload: bytes) -> List[str]:
    # Gemini frame SIZE numbers are in CHARACTERS, not bytes.
    # Korean UTF-8 (3 bytes/char) makes byte-based indexing undercount frame
    # content. Decode to a str first so all SIZE arithmetic uses char indices.
    data = _strip_xssi_prefix(payload)
    text = data.decode("utf-8", errors="replace")
    out: List[str] = []
    idx = 0
    n = len(text)

    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break

        j = idx
        while j < n and "0" <= text[j] <= "9":
            j += 1
        if j == idx:
            k = text.find("\n", idx)
            if k == -1:
                k = n
            line = text[idx:k].strip()
            idx = k + 1 if k < n else n
            if line.startswith("["):
                out.append(line)
            continue

        num_end = j
        while num_end < n and text[num_end] in " \t":
            num_end += 1
        if num_end < n and text[num_end] == "\r":
            num_end += 1
        if num_end >= n:
            idx = num_end + 1
            continue
        if text[num_end] == "\n":
            # SIZE chars = \n(leading) + JSON + \n(trailing); start at the \n.
            frame_start = num_end
        elif text[num_end] == "[":
            frame_start = num_end
        else:
            idx = num_end + 1
            continue

        token = text[idx:j]
        if not token:
            idx = frame_start
            continue
        candidates: List[int] = []
        try:
            v10 = int(token, 10)
            if v10 > 0:
                candidates.append(v10)
        except Exception:
            pass
        try:
            v16 = int(token, 16)
            if v16 > 0 and v16 not in candidates:
                candidates.append(v16)
        except Exception:
            pass
        if not candidates:
            idx = num_end + 1
            continue

        best_len = 0
        best_score = -1
        for frame_len in candidates:
            # Build candidate end positions to try.
            # Gemini SIZE appears to be computed in JavaScript's UTF-16 code
            # units, while Python str uses Unicode code points.  Supplementary-
            # plane characters (emoji U+10000+, e.g. 🚀 U+1F680) count as 2 in
            # JS .length but as 1 in Python len().  When the frame content
            # contains K such characters the JS-based SIZE is K units larger
            # than Python len(), making frame_end overflow by exactly K.
            # We therefore try SIZE, SIZE-1 … SIZE-4 (covers ≤4 emoji/frame)
            # and also fall back to n (end-of-text) so the last frame is never
            # silently dropped just because of the UTF-16/code-point mismatch.
            ends_to_try: List[int] = []
            base_end = frame_start + frame_len
            for adj in range(0, 5):  # 0 = exact SIZE, 1..4 = emoji adjustment
                end = base_end - adj
                if frame_start < end <= n:
                    ends_to_try.append(end)
            # If SIZE overshoots n, also try n itself (last frame, may have no
            # trailing newline OR SIZE counts a few extra UTF-16 units).
            if base_end > n and n > frame_start and n not in ends_to_try:
                ends_to_try.append(n)
            if not ends_to_try:
                continue

            for actual_end in ends_to_try:
                candidate_str = text[frame_start:actual_end]
                score = 0
                stripped = candidate_str.lstrip()
                if stripped.startswith("["):
                    score += 2
                if stripped.startswith("[["):
                    score += 2
                try:
                    obj = json.loads(candidate_str)
                    if isinstance(obj, list):
                        score += 6
                    else:
                        score += 3
                except Exception:
                    pass
                actual_len = actual_end - frame_start
                if score > best_score or (score == best_score and actual_len > best_len):
                    best_score = score
                    best_len = actual_len

        if best_len <= 0:
            idx = frame_start
            continue

        frame_end = frame_start + best_len
        out.append(text[frame_start:frame_end])
        idx = frame_end

    return out


def _collect_structured_segments(
    value: Any, out: List[Tuple[int, str]], depth: int = 0,
) -> None:
    """Collect (type_code, text) from Gemini [None, <int>, <str>, ...] nodes.

    type_code 0 = paragraph, 3 = heading-h3, etc.
    """
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, list):
        if (
            len(value) >= 3
            and value[0] is None
            and isinstance(value[1], int)
            and isinstance(value[2], str)
        ):
            seg = value[2].strip()
            if seg:
                out.append((value[1], seg))
        for item in value:
            _collect_structured_segments(item, out, depth + 1)
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_structured_segments(item, out, depth + 1)


def _join_structured_segments(segments: List[Tuple[int, str]]) -> str:
    """Join structured (type_code, text) segments with correct spacing.

    Headings (type != 0) are followed by a single newline;
    everything else is separated by a blank line.
    """
    seen: set = set()
    deduped: List[Tuple[int, str]] = []
    for type_code, text in segments:
        if text not in seen:
            seen.add(text)
            deduped.append((type_code, text))

    if not deduped:
        return ""

    parts: List[str] = []
    for i, (type_code, text) in enumerate(deduped):
        if i > 0:
            prev_type = deduped[i - 1][0]
            parts.append("\n" if prev_type != 0 else "\n\n")
        parts.append(text)
    return "".join(parts)


def _light_normalize(text: str) -> str:
    """Minimal cleanup for structured-segment text (no markdown stripping)."""
    if not text:
        return ""
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"(?mi)^\s*\[image of [^\]]+\]\s*$", "", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# Gemini web candidate layout (rc_* node), same slots the web client reads:
#   [1][0]     visible answer text
#   [37]       thought summary, not shown as the chat reply
_GEMINI_RC_THOUGHTS_INDEX = 37


def _iter_nested_strings(value: Any, out: List[str], depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, str):
        text = value.strip()
        if text and not text.startswith("rc_"):
            out.append(text)
        return
    if isinstance(value, list):
        for item in value:
            _iter_nested_strings(item, out, depth + 1)
        return
    if isinstance(value, dict):
        for item in value.values():
            _iter_nested_strings(item, out, depth + 1)


def _text_belongs_to_channel(text: str, channel: str) -> bool:
    """True when text is the side channel itself, not a separate reply."""
    body = (text or "").strip()
    side = (channel or "").strip()
    if not body or not side:
        return False
    return body == side or body in side


_FILE_CHIP_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _is_file_chip(text: str) -> bool:
    """한 줄짜리 파일 이름. 답변 문장과 같이 있을 때 문장을 지우면 안 된다."""
    line = " ".join((text or "").split())
    if not line or "\n" in (text or "") or len(line) > 180:
        return False
    return bool(_FILE_CHIP_RE.search(line))


def _visible_text_from_rc_node(node: list) -> str:
    """User-visible reply from one rc_* candidate. Thoughts stay in their slot.

    The chat bubble is the rendered [None, int, str] segments. Index 1 is the
    plain-text copy, and index 37 is the thought summary. A file chip is kept
    with the answer sentence. Language is not consulted.
    """
    if _rc_node_is_thinking(node):
        return ""

    thoughts = ""
    if len(node) > _GEMINI_RC_THOUGHTS_INDEX:
        thought_parts: List[str] = []
        _iter_nested_strings(node[_GEMINI_RC_THOUGHTS_INDEX], thought_parts)
        thoughts = "\n".join(thought_parts).strip()

    plain = ""
    if len(node) > 1 and isinstance(node[1], list):
        chunks = [
            c.strip()
            for c in node[1]
            if isinstance(c, str) and c.strip() and not _text_belongs_to_channel(c, thoughts)
        ]
        plain = "\n".join(chunks).strip()

    segments: List[Tuple[int, str]] = []
    for index, item in enumerate(node):
        if index in (0, _GEMINI_RC_THOUGHTS_INDEX):
            continue
        _collect_structured_segments(item, segments)
    visible = [
        (code, seg)
        for code, seg in segments
        if not _text_belongs_to_channel(seg, thoughts)
    ]
    extras = [
        (code, seg)
        for code, seg in visible
        if not _text_belongs_to_channel(seg, plain)
    ]
    rendered = _light_normalize(_join_structured_segments(visible))
    extra_text = _light_normalize(_join_structured_segments(extras))
    if plain and rendered and _text_belongs_to_channel(plain, rendered):
        return rendered
    if plain and extras and all(_is_file_chip(seg) for _, seg in extras):
        if not extra_text or _text_belongs_to_channel(extra_text, plain):
            return plain
        return _light_normalize(f"{plain}\n{extra_text}")
    if extra_text:
        return extra_text
    if plain:
        return plain
    return rendered


def _extract_text_from_inner(inner: Any) -> str:
    """Extract the user-visible assistant reply from a parsed Gemini inner JSON.

    Each streaming chunk carries the full accumulated reply on rc_* candidates
    at inner[4]. Thought text lives in a separate slot and is not the reply.
    """
    if not isinstance(inner, list):
        return ""

    node_texts: List[str] = []

    def _collect_rc_nodes(container: Any) -> None:
        if not isinstance(container, list):
            return
        for node in container:
            if not (isinstance(node, list) and len(node) > 1):
                continue
            if not (isinstance(node[0], str) and node[0].startswith("rc_")):
                continue
            text = _visible_text_from_rc_node(node)
            if text:
                node_texts.append(text)

    if len(inner) > 4 and isinstance(inner[4], list):
        _collect_rc_nodes(inner[4])

    if not node_texts:
        for idx in (2, 3, 5, 6):
            if len(inner) > idx and isinstance(inner[idx], list):
                _collect_rc_nodes(inner[idx])
                if node_texts:
                    break

    if node_texts:
        return _normalize_gemini_text("\n\n".join(node_texts))

    segments: List[Tuple[int, str]] = []
    if len(inner) > 4 and isinstance(inner[4], list):
        _collect_structured_segments(inner[4], segments)
    if not segments:
        _collect_structured_segments(inner, segments)
    if not segments:
        return ""
    return _light_normalize(_join_structured_segments(segments))


def _rc_node_is_thinking(node: list) -> bool:
    """Detect Gemini thought-summary rc_* nodes via flags / nested markers."""
    if not isinstance(node, list) or len(node) < 2:
        return False
    # Common web markers: nested dict/list containing thought-like type strings.
    for item in node[2:12]:
        if isinstance(item, str):
            low = item.lower()
            if low in ("thought", "thoughts", "thinking", "thought_summary"):
                return True
        elif isinstance(item, dict):
            for key, val in item.items():
                k = str(key).lower()
                if "thought" in k or k in ("thinking", "is_thinking"):
                    if val is True or (isinstance(val, str) and val.strip()):
                        return True
                if isinstance(val, str) and val.lower() in ("thought", "thoughts", "thinking"):
                    return True
        elif isinstance(item, list):
            for sub in item[:8]:
                if isinstance(sub, str) and sub.lower() in (
                    "thought",
                    "thoughts",
                    "thinking",
                    "thought_summary",
                ):
                    return True
    return False


def _extract_ids_from_inner(inner: Any) -> Tuple[str, str]:
    session_id = ""
    message_id = ""
    if isinstance(inner, list) and len(inner) > 1 and isinstance(inner[1], list):
        ids = inner[1]
        if len(ids) > 0 and isinstance(ids[0], str):
            session_id = ids[0]
        if len(ids) > 1 and isinstance(ids[1], str):
            message_id = ids[1]
    return session_id, message_id


def _extract_first_rc_id_from_inner(inner: Any) -> str:
    if not isinstance(inner, list):
        return ""

    def walk(node: Any) -> str:
        if not isinstance(node, list):
            return ""
        if (
            len(node) >= 2
            and isinstance(node[0], str)
            and node[0].startswith("rc_")
        ):
            return node[0]
        for child in node:
            found = walk(child)
            if found:
                return found
        return ""

    for idx in (4, 2, 3, 5, 6):
        if len(inner) > idx:
            found = walk(inner[idx])
            if found:
                return found
    return walk(inner)


def _extract_stream_ids_from_payload(payload: bytes) -> Tuple[str, str, str]:
    session_id = ""
    response_id = ""
    rc_id = ""
    _, session_id, response_id = _extract_response_text(payload)
    for frame in _iter_wrb_frames(payload):
        try:
            outer = json.loads(frame)
        except Exception:
            continue
        if not isinstance(outer, list):
            continue
        for item in outer:
            if not (isinstance(item, list) and len(item) > 2 and item[0] == "wrb.fr"):
                continue
            inner_raw = item[2]
            if not isinstance(inner_raw, str):
                continue
            inner = _loads_maybe_json(inner_raw)
            if inner is None:
                continue
            sid, rid = _extract_ids_from_inner(inner)
            if sid and not session_id:
                session_id = sid
            if rid and not response_id:
                response_id = rid
            if not rc_id:
                rc_id = _extract_first_rc_id_from_inner(inner)
            if session_id and response_id and rc_id:
                return session_id, response_id, rc_id
    return session_id, response_id, rc_id


def _find_wrb_fr_inner_strings(raw: str) -> List[str]:
    """Fallback: find all inner JSON strings in wrb.fr entries without frame parsing.

    Uses C-level str.find instead of a Python char-by-char scan. On multi-MB
    Gemini StreamGenerate bodies this is ~50x faster and equivalent for the
    ["wrb.fr",null,"..."] pattern.
    """
    out: List[str] = []
    needle = '["wrb.fr",null,"'
    nlen = len(needle)
    i = 0
    while True:
        i = raw.find(needle, i)
        if i == -1:
            break
        start = i + nlen
        j = start
        while True:
            q = raw.find('"', j)
            if q == -1:
                return out
            # Odd number of preceding backslashes ⇒ escaped quote; keep scanning.
            bs = 0
            k = q - 1
            while k >= start and raw[k] == "\\":
                bs += 1
                k -= 1
            if bs % 2 == 0:
                if start < q:
                    out.append(raw[start:q])
                i = q + 1
                break
            j = q + 1
    return out


def _extract_response_text(payload: bytes) -> Tuple[str, str, str]:
    latest_text = ""
    latest_session = ""
    latest_message = ""

    def _consume_inner(inner: Any) -> None:
        nonlocal latest_text, latest_session, latest_message
        session_id, message_id = _extract_ids_from_inner(inner)
        text = _extract_text_from_inner(inner)
        if not text:
            # A thinking stub carries the new conversation id and no reply.
            # Keep that id so the later StreamGenerate can join this turn.
            if session_id and not latest_session:
                latest_session = session_id
            if message_id and not latest_message:
                latest_message = message_id
            return
        # Each wrb frame carries the full reply so far. The last one is final.
        latest_text = text
        if session_id:
            latest_session = session_id
        if message_id:
            latest_message = message_id

    frames = _iter_wrb_frames(payload)

    for _fi, frame in enumerate(frames):
        try:
            outer = json.loads(frame)
        except Exception:
            continue
        if not isinstance(outer, list):
            continue
        for item in outer:
            if not (isinstance(item, list) and len(item) > 2 and item[0] == "wrb.fr"):
                continue
            inner_raw = item[2]
            if not isinstance(inner_raw, str):
                continue
            inner = _loads_maybe_json(inner_raw)
            if inner is None:
                continue
            _consume_inner(inner)

    # Frame parsing can miss entries when transport formatting is irregular
    # (e.g. spaces around frame-size delimiters). Always run a raw wrb.fr scan
    # as a safety net: frames alone can yield a short early text while longer
    # later entries are only visible via raw scan.
    if b"wrb.fr" in payload:
        raw = _safe_utf8(payload)
        for inner_str in _find_wrb_fr_inner_strings(raw):
            try:
                decoded_inner_str = json.loads('"' + inner_str + '"')
            except Exception:
                decoded_inner_str = inner_str
            inner = _loads_maybe_json(decoded_inner_str)
            if inner is None:
                continue
            _consume_inner(inner)

    chosen_text = latest_text
    chosen_session = latest_session
    chosen_message = latest_message

    logging.debug(
        "[gemini] extracted %d chars session=%r",
        len(chosen_text),
        chosen_session,
    )
    return chosen_text, chosen_session, chosen_message


# Model slot in the StreamGenerate inner list: [39]=id, [40]=null, [41]=null, [42]=display name, then true.
_GEMINI_MODEL_SLOT_RE = re.compile(
    rb'null,null,(?:\\"|")([^"\\]{1,80})(?:\\"|"),true'
)
_GEMINI_SESSION_MODEL: Dict[str, str] = {}
_GEMINI_SESSION_MODEL_LOCK = threading.Lock()


def _remember_gemini_model(session_id: str, model: str) -> None:
    sid = str(session_id or "").strip()
    name = str(model or "").strip()
    if not sid or not name:
        return
    with _GEMINI_SESSION_MODEL_LOCK:
        _GEMINI_SESSION_MODEL[sid] = name
        if len(_GEMINI_SESSION_MODEL) > 4000:
            for key in list(_GEMINI_SESSION_MODEL)[:1000]:
                _GEMINI_SESSION_MODEL.pop(key, None)


def _recall_gemini_model(session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    with _GEMINI_SESSION_MODEL_LOCK:
        return _GEMINI_SESSION_MODEL.get(sid, "")


def _gemini_slot_model(value: Any) -> str:
    """Return the display name Gemini put in the model slot, unchanged."""
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or len(name) > 80 or any(ord(ch) < 32 for ch in name):
        return ""
    return name


def _extract_model_from_payload(payload: bytes) -> str:
    """Find the Gemini display name in the response model slot."""
    if not payload:
        return ""

    def _check_inner(inner: Any) -> str:
        if isinstance(inner, list) and len(inner) > 42:
            return _gemini_slot_model(inner[42])
        return ""

    for frame in _iter_wrb_frames(payload):
        try:
            outer = json.loads(frame)
        except Exception:
            continue
        if not isinstance(outer, list):
            continue
        for item in outer:
            if not (isinstance(item, list) and len(item) > 2 and item[0] == "wrb.fr"):
                continue
            inner_raw = item[2]
            if not isinstance(inner_raw, str):
                continue
            inner = _loads_maybe_json(inner_raw)
            found = _check_inner(inner)
            if found:
                return found

    if b"wrb.fr" in payload:
        raw = _safe_utf8(payload)
        for inner_str in _find_wrb_fr_inner_strings(raw):
            try:
                decoded = json.loads('"' + inner_str + '"')
            except Exception:
                decoded = inner_str
            inner = _loads_maybe_json(decoded)
            found = _check_inner(inner)
            if found:
                return found

    match = _GEMINI_MODEL_SLOT_RE.search(payload or b"")
    if match:
        try:
            name = match.group(1).decode("utf-8", errors="replace")
        except Exception:
            name = ""
        found = _gemini_slot_model(name)
        if found:
            return found

    return _extract_model_from_process_session(payload)


def _process_session_candidate_model(fields: List[Tuple[int, int, Any]]) -> str:
    """Display name on the answer candidate. Field 44 true is the same marker as StreamGenerate's trailing true."""
    name = ""
    marked = False
    for field, wire, value in fields:
        if field == 43 and wire == 2 and isinstance(value, (bytes, bytearray)):
            name = _gemini_slot_model(_utf8_field(bytes(value)))
        elif field == 44 and wire == 0 and value == 1:
            marked = True
    if name and marked:
        return name
    return ""


def _walk_process_session_model(buf: bytes, path: str = "") -> str:
    """Last display name under a `.4.4` candidate. Same shift as ids at field 2 and the answer at field 5."""
    fields = list(_iter_pb_fields(buf))
    if not fields:
        return ""
    found = _process_session_candidate_model(fields) if path.endswith(".4.4") else ""
    for field, wire, value in fields:
        if wire != 2 or not isinstance(value, (bytes, bytearray)):
            continue
        chunk = bytes(value)
        if _parse_pb_fields(chunk) is None:
            continue
        nested = _walk_process_session_model(chunk, f"{path}.{field}")
        if nested:
            found = nested
    return found


def _might_be_process_session_body(payload: bytes) -> bool:
    """Length-prefixed browserchannel record, or the gzip form of one."""
    if len(payload or b"") < 4:
        return False
    if payload[:2] == b"\x1f\x8b":
        return True
    newline = payload.find(b"\n", 0, 12)
    if newline <= 0:
        return False
    return payload[:newline].strip().isdigit()


def _extract_model_from_process_session(payload: bytes) -> str:
    """Model display name from a processSession browserchannel body.

    StreamGenerate stores it at JSON index 42, followed by true. This protobuf
    numbers that same candidate one higher, so the name is field 43 and the
    marker is field 44. A later record in the body wins; a record without the
    name keeps the earlier one.
    """
    if not _might_be_process_session_body(payload):
        return ""
    found = ""
    for record in _process_session_records(payload):
        try:
            obj = json.loads(record)
        except Exception:
            continue
        for blob in _iter_json_strings(obj):
            decoded = _b64_decode_loose(blob)
            if not decoded:
                continue
            name = _walk_process_session_model(decoded)
            if name:
                found = name
    return found


def _note_process_session_model(req: Dict[str, Any], payload: bytes, session_id: str) -> None:
    """Remember a processSession model before the growing snapshot is logged.

    A later long-poll can carry a longer answer and omit the name. The c_*
    session keeps the name already seen.
    """
    sid = str(session_id or req.get("_gemini_output_session_id") or "").strip()
    found = _extract_model_from_process_session(payload)
    if found:
        previous = _recall_gemini_model(sid) if sid else ""
        _remember_gemini_model(sid, found)
        if found != previous:
            logger.info(
                "gemini_model name=%s session=%s source=body",
                found,
                sid or "-",
            )
    elif sid:
        found = _recall_gemini_model(sid)
    if found:
        req["_gemini_model"] = found


def extract_model(req: Dict[str, Any], payload: bytes) -> str:
    """Extract the model display name Gemini sent, without renaming it.

    StreamGenerate carries the name in the wrb.fr model slot. processSession
    carries the same name on candidate field 43. A later chunk sometimes omits
    it, so the last name seen for the same c_* session is reused.
    """
    if isinstance(req, dict) and req.get("_gemini_model"):
        return str(req.get("_gemini_model") or "").strip()
    found = _extract_model_from_payload(payload or b"")
    source = "body" if found else ""
    session_id = ""
    if isinstance(req, dict):
        session_id = str(
            req.get("_gemini_output_session_id") or req.get("_gemini_session_id") or ""
        ).strip()
    if not found and session_id:
        found = _recall_gemini_model(session_id)
        source = "session" if found else ""
    if found and isinstance(req, dict):
        req["_gemini_model"] = found
        _remember_gemini_model(session_id, found)
        logger.info(
            "gemini_model name=%s session=%s source=%s",
            found,
            session_id or "-",
            source,
        )
    return found


def _decode_f_req_prompt_literal(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        decoded = json.loads(f'"{text}"')
        if isinstance(decoded, str):
            return decoded.strip()
    except Exception:
        pass
    return text.replace('\\"', '"').replace("\\\\", "\\").strip()


def _extract_prompt_from_f_req_regex(f_req_raw: str) -> str:
    """Fallback when json.loads fails on f.req (binary noise, partial frames)."""
    text = _sanitize_f_req_raw(f_req_raw)
    if not text:
        return ""
    matches = list(_GEMINI_F_REQ_USER_PROMPT_RE.finditer(text))
    if not matches:
        return ""
    prompts: List[str] = []
    for m in matches:
        candidate = _decode_f_req_prompt_literal(m.group(1))
        if not candidate or "/contrib_service/" in candidate or "ttl_1d" in candidate:
            continue
        if candidate.startswith("c_") or _looks_like_token(candidate):
            continue
        prompts.append(candidate)
    if not prompts:
        return ""
    return prompts[-1]


def _rewrite_f_req_regex_slot(f_req_raw: str, replacement_text: str) -> Optional[Tuple[str, int]]:
    text = _sanitize_f_req_raw(f_req_raw)
    matches = list(_GEMINI_F_REQ_USER_PROMPT_RE.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    current = _decode_f_req_prompt_literal(m.group(1))
    if current == replacement_text:
        return None
    escaped = json.dumps(replacement_text, ensure_ascii=False)[1:-1]
    updated = text[: m.start(1)] + escaped + text[m.end(1) :]
    if updated == text:
        return None
    return updated, 1


def _extract_prompt_from_f_req(f_req_raw: str) -> str:
    prompt = ""
    outer = _loads_maybe_json(f_req_raw)
    if outer is not None:
        prompts: List[str] = []

        def collect_from_target(target: Any) -> None:
            tuples: List[list] = []
            _collect_user_prompt_tuples(target, tuples)
            for item in tuples:
                text = str(item[0] or "").strip()
                if text and not _is_gemini_internal_prompt(text):
                    prompts.append(text)

        if isinstance(outer, list):
            for item in outer:
                candidate = item
                if isinstance(item, str):
                    parsed = _loads_maybe_json(item)
                    if isinstance(parsed, (dict, list)):
                        candidate = parsed
                if isinstance(candidate, (dict, list)):
                    collect_from_target(candidate)

        if not prompts:
            fallback: List[str] = []
            _collect_prompts(outer, fallback, 0)
            if isinstance(outer, list):
                for item in outer:
                    if not isinstance(item, str):
                        continue
                    inner = _loads_maybe_json(item)
                    if inner is None:
                        continue
                    _collect_prompts(inner, fallback, 0)
            prompts = fallback

        if prompts:
            prompt = prompts[-1]

    if not prompt:
        prompt = _extract_prompt_from_f_req_regex(f_req_raw)
    if _is_gemini_internal_prompt(prompt):
        prompt = ""
    return prompt


def _read_pb_varint(buf: bytes, index: int) -> Tuple[Optional[int], int]:
    number = 0
    shift = 0
    while index < len(buf) and shift <= 63:
        byte = buf[index]
        index += 1
        number |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return number, index
        shift += 7
    return None, index


def _encode_pb_varint(number: int) -> bytes:
    out = bytearray()
    value = int(number)
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _encode_pb_ld(field: int, data: bytes) -> bytes:
    return _encode_pb_varint((field << 3) | 2) + _encode_pb_varint(len(data)) + data


def _parse_pb_fields(buf: bytes) -> Optional[List[Tuple[int, int, bytes]]]:
    """Parse one complete protobuf message. None unless every byte is consumed."""
    if not buf:
        return None
    fields: List[Tuple[int, int, bytes]] = []
    index = 0
    while index < len(buf):
        key, nxt = _read_pb_varint(buf, index)
        if key is None:
            return None
        field = key >> 3
        wire = key & 7
        index = nxt
        if field <= 0:
            return None
        if wire == 0:
            _, index = _read_pb_varint(buf, index)
            if _ is None:
                return None
            fields.append((field, wire, b""))
        elif wire == 1:
            if index + 8 > len(buf):
                return None
            index += 8
            fields.append((field, wire, b""))
        elif wire == 5:
            if index + 4 > len(buf):
                return None
            index += 4
            fields.append((field, wire, b""))
        elif wire == 2:
            length, index = _read_pb_varint(buf, index)
            if length is None or length < 0 or index + length > len(buf):
                return None
            fields.append((field, wire, buf[index : index + length]))
            index += length
        else:
            return None
    if index != len(buf) or not fields:
        return None
    return fields


def _walk_pb_strings(buf: bytes, path: str = "") -> List[Tuple[str, str]]:
    """UTF-8 leaves. A chunk that is itself a complete message is walked, not stored."""
    fields = _parse_pb_fields(buf)
    if fields is None:
        return []
    found: List[Tuple[str, str]] = []
    for field, wire, chunk in fields:
        if wire != 2:
            continue
        child = f"{path}.{field}"
        if _parse_pb_fields(chunk) is not None:
            found.extend(_walk_pb_strings(chunk, child))
            continue
        try:
            text = chunk.decode("utf-8")
        except Exception:
            continue
        if text and all(ch >= " " or ch in "\n\t\r" for ch in text):
            found.append((child, text))
    return found


def _b64_decode_loose(value: str) -> bytes:
    # Standard base64 uses '+'. unquote_plus turns that into a space and the
    # processSession answer protobuf never decodes. Percent-decoding only.
    text = unquote(str(value or "")).strip()
    if not text:
        return b""
    pad = "=" * ((4 - len(text) % 4) % 4)
    try:
        return base64.b64decode(text + pad, validate=False)
    except Exception:
        try:
            return base64.urlsafe_b64decode(text + pad)
        except Exception:
            return b""


def is_google_search_host(host: str) -> bool:
    """www.google.com / google.co.kr. Not gemini.google.com or googleapis."""
    raw = (host or "").lower().split(":", 1)[0].strip().rstrip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    if not raw.startswith("google.") or raw.startswith("googleapis."):
        return False
    return "." in raw


def _google_path_and_query(path: str) -> Tuple[str, str]:
    base, _, query = (path or "").strip().partition("?")
    base = base.lower().rstrip("/") or "/"
    return base, query


def is_google_ai_mode_answer_path(path: str) -> bool:
    """Answer body. /async/folsrch is AI Overviews, not AI Mode."""
    base, _ = _google_path_and_query(path)
    return base in ("/async/folif", "/async/folwr")


def is_google_ai_mode_path(path: str) -> bool:
    """AI Mode turn. Prompt is the q query param. udm=50 is the search shell."""
    base, query = _google_path_and_query(path)
    if base in ("/async/folif", "/async/folwr"):
        return True
    if base != "/search":
        return False
    values = parse_qs(query, keep_blank_values=True).get("udm") or []
    return any(str(value) == "50" for value in values)


def is_google_ai_mode_request(host: str, path: str) -> bool:
    return is_google_search_host(host) and is_google_ai_mode_path(path)


def google_ai_mode_prompt(path: str) -> str:
    _, query = _google_path_and_query(path)
    values = parse_qs(query, keep_blank_values=False).get("q") or []
    if not values:
        return ""
    return unquote_plus(str(values[0])).strip()


def google_ai_mode_event_id(path: str) -> str:
    """Page event id. Not the signed stkp/mstk/srtst tokens."""
    _, query = _google_path_and_query(path)
    values = parse_qs(query, keep_blank_values=False).get("ei") or []
    return str(values[0]).strip() if values else ""


_HTML_DROP_RE = re.compile(
    r"<(script|style|noscript|svg|template)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_BREAK_RE = re.compile(
    r"<br\s*/?>|</(?:p|div|li|tr|h[1-6]|section|article)\s*>",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _google_async_payload(raw: str) -> str:
    text = raw.lstrip("\ufeff \t\r\n")
    if text.startswith(")]}'"):
        text = text[4:].lstrip("\r\n")
    parts: List[str] = []
    index = 0
    limit = len(text)
    while index < limit:
        while index < limit and text[index] in "\r\n \t":
            index += 1
        end = index
        while end < limit and text[end].isdigit():
            end += 1
        if end == index or end >= limit or text[end] not in "\r\n":
            break
        length = int(text[index:end])
        if length <= 0:
            break
        index = end
        while index < limit and text[index] in "\r\n":
            index += 1
        if index + length > limit:
            break
        parts.append(text[index : index + length])
        index += length
    if parts:
        return "".join(parts)
    return text


def _unescape_google_markup(raw: str) -> str:
    if "\\u003" not in raw and "\\n" not in raw and '\\"' not in raw:
        return raw
    return (
        raw.replace("\\u003c", "<")
        .replace("\\u003e", ">")
        .replace("\\u003d", "=")
        .replace("\\u0026", "&")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\/", "/")
    )


_AIM_WRAP_CLASS_RE = re.compile(
    r"\.([A-Za-z0-9_-]+)\s*\{[^}]*overflow-wrap\s*:\s*break-word",
    re.IGNORECASE,
)
_AI_MODE_SKIP_TAGS = frozenset(
    {"script", "style", "svg", "button", "noscript", "template", "textarea", "input"}
)


def _ai_mode_paragraph_classes(markup: str) -> set:
    """Class names the response stylesheet uses for wrapping answer text.

    The hash changes with the search-next bundle. The rule does not.
    """
    return set(_AIM_WRAP_CLASS_RE.findall(markup))


class _AiModeAnswerParser(HTMLParser):
    """Visible AI Mode answer from the folif fragment.

    Kept: stylesheet wrap-paragraphs, role=heading, li.
    Dropped: data-ignore-copy titles, data-amic / notranslate citation chips,
    data-src-id source cards, buttons, dialogs, and anything after the
    support link whose href contains p=aimode.
    """

    def __init__(self, paragraph_classes: set) -> None:
        super().__init__(convert_charrefs=True)
        self._paragraph_classes = paragraph_classes
        self._stack: List[Dict[str, Any]] = []
        self._blocks: List[Dict[str, Any]] = []
        self._stop = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr = {key: value for key, value in attrs}
        classes = set((attr.get("class") or "").split())
        style = (attr.get("style") or "").replace(" ", "").lower()
        parent_skip = bool(self._stack and self._stack[-1]["skip"])
        skip = parent_skip or tag in _AI_MODE_SKIP_TAGS
        if not skip:
            skip = (
                "data-ignore-copy" in attr
                or attr.get("data-amic") == "true"
                or attr.get("aria-hidden") == "true"
                or "hidden" in attr
                or attr.get("role") in ("button", "dialog", "img")
                or "popover" in attr
                or "notranslate" in classes
                or "display:none" in style
                or "data-src-id" in attr
                or "data-crb-el" in attr
            )
        kind = None
        if not skip and not self._stop and not any(frame["kind"] for frame in self._stack):
            if (
                tag == "li"
                or attr.get("role") == "heading"
                or classes & self._paragraph_classes
            ):
                kind = "body"
        frame: Dict[str, Any] = {
            "tag": tag,
            "skip": skip,
            "kind": kind,
            "own": [],
            "texts": [],
        }
        self._stack.append(frame)
        if kind:
            self._blocks.append(frame)
        href = attr.get("href") or ""
        if "p=aimode" in href and not self._stop:
            for parent in reversed(self._stack[:-1]):
                if parent["tag"] != "div" or parent["skip"]:
                    continue
                parent["kind"] = "disc"
                if parent not in self._blocks:
                    parent["texts"] = list(parent["own"])
                    self._blocks.append(parent)
                break
        if tag == "br" and not skip and not self._stop:
            self.handle_data("\n")

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index]["tag"] != tag:
                continue
            if self._stack[index]["kind"] == "disc":
                self._stop = True
            self._stack = self._stack[:index]
            return

    def handle_data(self, data: str) -> None:
        if self._stop or not self._stack or self._stack[-1]["skip"]:
            return
        self._stack[-1]["own"].append(data)
        for frame in reversed(self._stack):
            if frame["kind"]:
                frame["texts"].append(data)
                return

    def answer(self) -> str:
        lines: List[str] = []
        for frame in self._blocks:
            raw = "".join(frame["texts"]).replace("\xa0", " ")
            line = re.sub(r"[ \t]*\n[ \t]*", " ", raw)
            line = re.sub(r"[ \t]{2,}", " ", line).strip()
            if line and (not lines or lines[-1] != line):
                lines.append(line)
        return "\n".join(lines).strip()


def _extract_ai_mode_structured(markup: str) -> str:
    if "<" not in markup:
        return ""
    parser = _AiModeAnswerParser(_ai_mode_paragraph_classes(markup))
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        return ""
    return parser.answer()


def _html_to_text_fallback(markup: str) -> str:
    text = _HTML_DROP_RE.sub(" ", markup)
    text = _HTML_BREAK_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines: List[str] = []
    for line in text.splitlines():
        cleaned = re.sub(r"[ \t]+", " ", line).strip()
        if cleaned and (not lines or lines[-1] != cleaned):
            lines.append(cleaned)
    return "\n".join(lines).strip()


def google_ai_mode_answer(payload: bytes) -> str:
    raw = _safe_utf8(payload)
    if not raw.strip():
        return ""
    text = _unescape_google_markup(_google_async_payload(raw))
    structured = _extract_ai_mode_structured(text)
    if structured:
        return structured
    return _html_to_text_fallback(text)


def _remember_google_ai_mode_event(req: Dict[str, Any], path: str) -> str:
    event_id = google_ai_mode_event_id(path)
    if event_id:
        req["_gemini_session_id"] = event_id
    return event_id


def _is_gemini_process_session(req: Dict[str, Any]) -> bool:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    path = str(meta.get("path") or "").lower()
    return _GEMINI_WEB_PA_HOST in host and _GEMINI_PROCESS_SESSION_PATH in path


def _process_session_gsession(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    query = path.split("?", 1)[1] if "?" in path else ""
    if not query:
        return ""
    values = parse_qs(query, keep_blank_values=True).get("gsessionid") or []
    return str(values[0]).strip() if values else ""


def _remember_process_session_conv(gsessionid: str, conversation_id: str) -> None:
    gsid = (gsessionid or "").strip()
    conv = (conversation_id or "").strip()
    if not gsid or not _GEMINI_CONV_ID_RE.fullmatch(conv):
        return
    now = time.monotonic()
    with _GEMINI_GSESSION_CONV_LOCK:
        expired = [
            key
            for key, (_, ts) in _GEMINI_GSESSION_CONV.items()
            if now - ts > _GEMINI_SESSION_PARENT_TTL_SEC
        ]
        for key in expired:
            _GEMINI_GSESSION_CONV.pop(key, None)
        _GEMINI_GSESSION_CONV[gsid] = (conv, now)


def _lookup_process_session_conv(gsessionid: str) -> str:
    gsid = (gsessionid or "").strip()
    if not gsid:
        return ""
    now = time.monotonic()
    with _GEMINI_GSESSION_CONV_LOCK:
        entry = _GEMINI_GSESSION_CONV.get(gsid)
        if not entry:
            return ""
        conv, ts = entry
        if now - ts > _GEMINI_SESSION_PARENT_TTL_SEC:
            _GEMINI_GSESSION_CONV.pop(gsid, None)
            return ""
        return conv


def _purge_process_session_prompts(now: float) -> None:
    expired = [
        key
        for key, (_, ts) in _GEMINI_GSESSION_PROMPT.items()
        if now - ts > _GEMINI_SESSION_PARENT_TTL_SEC
    ]
    for key in expired:
        _GEMINI_GSESSION_PROMPT.pop(key, None)


def _remember_process_session_prompt(gsessionid: str, request_id: str) -> None:
    """Private processSession answers arrive on the long-poll, not the prompt POST."""
    gsid = (gsessionid or "").strip()
    rid = (request_id or "").strip()
    if not gsid or not rid:
        return
    now = time.monotonic()
    with _GEMINI_GSESSION_CONV_LOCK:
        _purge_process_session_prompts(now)
        _GEMINI_GSESSION_PROMPT[gsid] = (rid, now)


def _lookup_process_session_prompt(gsessionid: str) -> str:
    gsid = (gsessionid or "").strip()
    if not gsid:
        return ""
    now = time.monotonic()
    with _GEMINI_GSESSION_CONV_LOCK:
        _purge_process_session_prompts(now)
        entry = _GEMINI_GSESSION_PROMPT.get(gsid)
        if not entry:
            return ""
        rid, ts = entry
        if now - ts > _GEMINI_SESSION_PARENT_TTL_SEC:
            _GEMINI_GSESSION_PROMPT.pop(gsid, None)
            return ""
        return rid


def _bind_process_session_response(req: Dict[str, Any], session_id: str) -> None:
    """Point the long-poll answer at the prompt POST that opened this gsession."""
    gsession = _process_session_gsession(req)
    if session_id:
        req["_gemini_output_session_id"] = session_id
        if gsession:
            _remember_process_session_conv(gsession, session_id)
    parent = resolve_gemini_parent_request_id(session_id) if session_id else ""
    if not parent and gsession:
        parent = _lookup_process_session_prompt(gsession)
        if parent and session_id:
            register_gemini_turn_parent(session_id, parent)
    rid = str(req.get("request_id") or "").strip()
    if parent and rid and parent != rid:
        req["_gemini_ui_message_id"] = parent
        logger.info(
            "[gemini] ui_message_id_remap parent=%s request_id=%s session=%s",
            parent,
            rid,
            session_id or gsession,
        )


def _conversation_id_from_pb(buf: bytes) -> str:
    preferred = ""
    fallback = ""
    for path, text in _walk_pb_strings(buf):
        if not _GEMINI_CONV_ID_RE.fullmatch(text):
            continue
        if path.endswith(".72.1"):
            preferred = text
        elif not fallback:
            fallback = text
    return preferred or fallback


def _iter_pb_fields(buf: bytes):
    """Yield (field, wire, value). Value is int for varints and bytes for strings."""
    index = 0
    size = len(buf or b"")
    while index < size:
        key, index = _read_pb_varint(buf, index)
        if key is None or key <= 0:
            return
        field, wire = key >> 3, key & 7
        if field <= 0:
            return
        if wire == 0:
            value, index = _read_pb_varint(buf, index)
            if value is None:
                return
            yield field, wire, value
        elif wire == 2:
            length, index = _read_pb_varint(buf, index)
            if length is None or length < 0 or index + length > size:
                return
            yield field, wire, buf[index : index + length]
            index += length
        elif wire == 1:
            if index + 8 > size:
                return
            index += 8
        elif wire == 5:
            if index + 4 > size:
                return
            index += 4
        else:
            return


def _utf8_field(chunk: bytes) -> str:
    try:
        return bytes(chunk).decode("utf-8").strip()
    except Exception:
        return ""


# Logged-in file send has the user-turn box with an empty string. The dashboard
# row uses the same placeholder as the other adapters.
_FILE_ONLY_PROMPT = "사용자가 파일을 업로드하였습니다."


def _process_session_user_box(buf: bytes) -> Optional[str]:
    """Top-level user-turn text, or "" when that box is present but empty.

    None means this message is context or an ack. Nested history is not a
    new turn; an empty box one level down must not become a file send.
    """
    if not buf:
        return None
    empty = False
    for field, wire, value in _iter_pb_fields(buf):
        if field != 2 or wire != 2 or not isinstance(value, (bytes, bytearray)):
            continue
        marker = None
        saw_box = False
        prompt = ""
        for inner_field, inner_wire, inner_value in _iter_pb_fields(value):
            if inner_field == 1 and inner_wire == 0:
                marker = inner_value
            elif inner_field == 9 and inner_wire == 2 and isinstance(inner_value, (bytes, bytearray)):
                saw_box = True
                for box_field, box_wire, box_value in _iter_pb_fields(inner_value):
                    if box_field == 1 and box_wire == 2 and isinstance(box_value, (bytes, bytearray)):
                        prompt = _utf8_field(box_value)
        if marker == 1 and saw_box:
            if prompt and not _is_gemini_internal_prompt(prompt):
                return prompt
            empty = True
    return "" if empty else None


def _parse_process_session_prompt(buf: bytes) -> str:
    """User text is field 2 { field 1 = 1, field 9 { field 1 = prompt } }.

    A file turn inserts other fields between the marker and field 9. Context
    and ack posts use a different top-level field and must not become prompts.
    An empty user box is a file-only turn, not a prompt, so this stays "".
    """
    found = _process_session_user_box(buf)
    return found or ""


def _encode_process_session_prompt(text: str) -> bytes:
    inner = _encode_pb_ld(1, text.encode("utf-8"))
    middle = _encode_pb_varint((1 << 3) | 0) + _encode_pb_varint(1) + _encode_pb_ld(9, inner)
    return _encode_pb_ld(2, middle)


def _process_session_records(payload: bytes) -> List[str]:
    raw = payload or b""
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = zlib.decompress(raw, 31)
        except Exception:
            decoder = zlib.decompressobj(31)
            try:
                raw = decoder.decompress(raw)
            except Exception:
                raw = b""
            if not raw:
                return []
    text = raw.decode("utf-8", "replace")
    records: List[str] = []
    index = 0
    size = len(text)
    while index < size:
        newline = text.find("\n", index)
        if newline < 0 or newline - index > 12:
            break
        try:
            length = int(text[index:newline].strip())
        except ValueError:
            break
        start = newline + 1
        if length < 0 or start + length > size:
            break
        records.append(text[start : start + length])
        index = start + length
    return records


def _iter_json_strings(value: Any) -> List[str]:
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)

    walk(value)
    return found


def _extract_process_session_response(payload: bytes) -> Tuple[str, str, str]:
    """Last assistant text in a processSession browserchannel body.

    Each record is `<len>\\n[[id, ["<base64 protobuf>"]]]`. The visible answer
    grows in field `.4.4.5.2.1` beside an `rc_*` id. Location, chips, and
    classifier labels live on other fields and are not the answer.
    """
    latest_text = ""
    latest_session = ""
    latest_message = ""
    for record in _process_session_records(payload):
        try:
            obj = json.loads(record)
        except Exception:
            continue
        for blob in _iter_json_strings(obj):
            decoded = _b64_decode_loose(blob)
            if not decoded:
                continue
            strings = _walk_pb_strings(decoded)
            answer = ""
            has_rc = False
            session_id = ""
            message_id = ""
            for path, text in strings:
                if path.endswith(".4.4.5.1") and text.startswith("rc_"):
                    has_rc = True
                elif path.endswith(".4.4.5.2.1") and text.strip():
                    answer = text.strip()
                elif path.endswith(".4.4.2.1") and _GEMINI_CONV_ID_RE.fullmatch(text):
                    session_id = text
                elif path.endswith(".4.4.2.2") and _GEMINI_MSG_ID_RE.fullmatch(text):
                    message_id = text
            if has_rc and answer:
                latest_text = answer
            if session_id:
                latest_session = session_id
            if message_id:
                latest_message = message_id
    return latest_text, latest_session, latest_message


def _find_nested_process_session_prompt(buf: bytes, depth: int = 0) -> str:
    """Prompt box nested inside a larger frame (file turns batch it with context)."""
    if depth > 8:
        return ""
    found = _parse_process_session_prompt(buf)
    if found:
        return found
    fields = _parse_pb_fields(buf)
    if not fields:
        return ""
    for _field, wire, chunk in fields:
        if wire != 2 or len(chunk) < 4:
            continue
        found = _find_nested_process_session_prompt(chunk, depth + 1)
        if found:
            return found
    return ""


def _extract_process_session_prompt(req: Dict[str, Any], payload: bytes) -> str:
    form_text = _safe_utf8(payload)
    pairs = parse_qsl(form_text, keep_blank_values=True, strict_parsing=False)
    prompt = ""
    conversation_id = ""
    empty_user_box = False
    for key, value in pairs:
        if not _PROCESS_SESSION_DATA_KEY_RE.match(key):
            continue
        decoded = _b64_decode_loose(value)
        if not decoded:
            continue
        found = _parse_process_session_prompt(decoded) or _find_nested_process_session_prompt(decoded)
        if found and not prompt:
            prompt = found
        if _process_session_user_box(decoded) == "":
            empty_user_box = True
        conv = _conversation_id_from_pb(decoded)
        if conv:
            conversation_id = conv
    # File-only send: the user box is empty and this POST names a TTL file.
    # Remember it as this turn so the answer joins here, not the next sentence.
    if not prompt and empty_user_box and _fresh_process_session_segments(payload, True):
        prompt = _FILE_ONLY_PROMPT
        req["_gemini_file_only"] = True
        logger.info(
            "gemini_prompt processSession file_only session=%s",
            conversation_id or "-",
        )
    elif not prompt and len(payload or b"") > 1000:
        _dump_process_session_without_prompt(req, payload)
    gsessionid = _process_session_gsession(req)
    if conversation_id and gsessionid:
        _remember_process_session_conv(gsessionid, conversation_id)
    if not conversation_id and gsessionid:
        conversation_id = _lookup_process_session_conv(gsessionid)
    if conversation_id:
        req["_gemini_session_id"] = conversation_id
    if prompt and conversation_id:
        register_gemini_turn_parent(conversation_id, str(req.get("request_id") or ""))
    if prompt and gsessionid:
        _remember_process_session_prompt(gsessionid, str(req.get("request_id") or ""))
    if prompt:
        logger.info(
            "gemini_prompt processSession extracted_len=%d session=%s preview=%r",
            len(prompt),
            conversation_id or "-",
            prompt[:160],
        )
    return prompt


def _ttl_segment_from_text(text: str) -> str:
    marker = "/contrib_service/ttl_1d/"
    if marker not in (text or ""):
        return ""
    seg = text.split(marker, 1)[1].split("\x00", 1)[0].strip().strip("/")
    seg = seg.split("/", 1)[0].strip()
    if not seg or any(ch in seg for ch in "\"'[]{}, "):
        return ""
    return seg


def _fresh_process_session_segments(payload: bytes, include_unresolved: bool) -> List[str]:
    """TTL ids from this POST that still belong to a file uploaded just now.

    A later text turn repeats older paths from the chat. Those uploads are no
    longer recent, so they stay on the row that first sent the file.
    """
    try:
        import file_upload_manager as _fum_recent
    except ImportError:
        return _process_session_ttl_segments(payload)
    recent = set(
        _fum_recent.lookup_recent_saved_upload_ids(prefix="", limit=16, max_age_sec=180.0)
    )
    kept: List[str] = []
    for seg in _process_session_ttl_segments(payload):
        uid = _fum_recent.lookup_upload_id_by_ttl_segment(seg)
        if not uid:
            if include_unresolved:
                kept.append(seg)
            continue
        if uid in recent:
            kept.append(seg)
    return kept


def _process_session_ttl_segments(payload: bytes) -> List[str]:
    """TTL ids live in the protobuf, so the raw form body has no plaintext path."""
    found: List[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        seg = _ttl_segment_from_text(text)
        if seg and seg.lower() not in seen:
            seen.add(seg.lower())
            found.append(seg)

    for key, value in parse_qsl(_safe_utf8(payload), keep_blank_values=True, strict_parsing=False):
        if not _PROCESS_SESSION_DATA_KEY_RE.match(key):
            continue
        decoded = _b64_decode_loose(value)
        if not decoded:
            continue
        for _path, text in _walk_pb_strings(decoded):
            add(text)
    for seg in _extract_gemini_ttl_segments(payload):
        add("/contrib_service/ttl_1d/" + seg)
    return found


def _strip_ttl_protobuf(buf: bytes) -> bytes:
    """Drop file-path strings so a blocked file turn is not what the model reads."""
    fields = _pb_raw_fields(buf)
    if not fields:
        return buf
    pieces: List[bytes] = []
    changed = False
    for field, raw in fields:
        key, index = _read_pb_varint(raw, 0)
        if key is None or (key & 7) != 2:
            pieces.append(raw)
            continue
        length, index = _read_pb_varint(raw, index)
        if length is None:
            pieces.append(raw)
            continue
        body = raw[index : index + length]
        if _pb_raw_fields(body):
            stripped = _strip_ttl_protobuf(body)
            if stripped != body:
                changed = True
                pieces.append(_encode_pb_ld(field, stripped))
                continue
        elif "/contrib_service/ttl_1d/" in _utf8_field(body):
            changed = True
            continue
        pieces.append(raw)
    if not changed:
        return buf
    return b"".join(pieces)


def _strip_process_session_ttl(payload: bytes) -> bytes:
    pairs = parse_qsl(_safe_utf8(payload), keep_blank_values=True, strict_parsing=False)
    if not pairs:
        return payload
    changed = False
    out_pairs = []
    for key, value in pairs:
        if _PROCESS_SESSION_DATA_KEY_RE.match(key):
            decoded = _b64_decode_loose(value)
            stripped = _strip_ttl_protobuf(decoded) if decoded else decoded
            if stripped != decoded:
                value = base64.b64encode(stripped).decode("ascii")
                changed = True
        out_pairs.append((key, value))
    if not changed:
        return payload
    return urlencode(out_pairs, doseq=True).encode("utf-8")


def _rewrite_process_session_form(payload: bytes, replacement_text: str) -> Optional[bytes]:
    replacement = (replacement_text or "").strip()
    if not replacement:
        return None
    pairs = parse_qsl(_safe_utf8(payload), keep_blank_values=True, strict_parsing=False)
    if not pairs:
        return None
    changed = 0
    out_pairs = []
    for key, value in pairs:
        if not _PROCESS_SESSION_DATA_KEY_RE.match(key):
            out_pairs.append((key, value))
            continue
        decoded = _b64_decode_loose(value)
        # An empty user box is still the turn. Leaving it blank sends the file.
        if decoded and _process_session_user_box(decoded) is not None:
            encoded = base64.b64encode(_encode_process_session_prompt(replacement)).decode("ascii")
            if encoded != value:
                value = encoded
                changed += 1
        out_pairs.append((key, value))
    if changed <= 0:
        return None
    return urlencode(out_pairs, doseq=True).encode("utf-8")


def _is_gemini_host(req: Dict[str, Any]) -> bool:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    return _GEMINI_ACCOUNT_HOST_SUBSTR in host or "bard.google.com" in host


def _is_gemini_stream_generate_path(req: Dict[str, Any]) -> bool:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return "StreamGenerate" in str(meta.get("path") or "")


def gemini_is_continuation_request(req: Dict[str, Any], payload: bytes = b"") -> bool:
    """True when StreamGenerate has no new user prompt but continues a known c_* turn.

    Pro Extended may open a second StreamGenerate HTTP for the deep answer without
    repeating the user prompt — skip empty input_filter, still collect response.
    """
    if str(req.get("direction") or "").lower() != "request":
        return False
    if not _is_gemini_host(req) or not _is_gemini_stream_generate_path(req):
        return False
    if not payload or b"f.req=" not in payload[:8192]:
        return False
    form_text = _safe_utf8(payload)
    params = parse_qs(form_text, keep_blank_values=True, strict_parsing=False)
    values = params.get("f.req") or []
    if not values:
        return False
    prompt = _extract_prompt_from_f_req(values[0])
    if (prompt or "").strip():
        return False
    # A file-only send in an existing chat also has no prompt and a known c_*.
    # The TTL path is the new user turn; a real continuation does not attach a file.
    if _extract_gemini_ttl_segments(payload):
        return False
    sid = _extract_conversation_id_from_f_req(values[0])
    return bool(resolve_gemini_parent_request_id(sid))


def _is_gemini_policy_form_request(req: Dict[str, Any], payload: bytes = b"") -> bool:
    """StreamGenerate POST with f.req form body (unit_type may be raw_body in some paths)."""
    if str(req.get("direction") or "").lower() != "request":
        return False
    if not _is_gemini_host(req) or not _is_gemini_stream_generate_path(req):
        return False
    unit_type = str(req.get("unit_type") or "").lower()
    content_type = str(req.get("content_type") or "").lower()
    if unit_type == "form_body" or "application/x-www-form-urlencoded" in content_type:
        return True
    if payload and b"f.req=" in payload[:8192]:
        return True
    return False


def _is_gemini_stream_response(req: Dict[str, Any]) -> bool:
    if str(req.get("direction") or "").lower() != "response":
        return False
    if not _is_gemini_host(req):
        return False
    return _is_gemini_stream_generate_path(req)


def _extract_conversation_id_from_f_req(f_req_raw: str) -> str:
    outer = _loads_maybe_json(f_req_raw)
    if outer is None:
        return ""

    def scan(value: Any, depth: int = 0) -> str:
        if depth > _MAX_DEPTH:
            return ""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("c_"):
                return text
            inner = _loads_maybe_json(value)
            if inner is not None:
                return scan(inner, depth + 1)
            return ""
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip().startswith("c_"):
                    return item.strip()
            for item in value:
                found = scan(item, depth + 1)
                if found:
                    return found
        return ""

    return scan(outer, 0)


def _extract_conversation_id_from_payload(payload: bytes) -> str:
    form_text = _safe_utf8(payload)
    if "f.req=" not in form_text:
        return ""
    params = parse_qs(form_text, keep_blank_values=True, strict_parsing=False)
    values = params.get("f.req") or []
    if not values:
        return ""
    return _extract_conversation_id_from_f_req(values[0])


def _collect_conversation_ids_from_f_req(f_req_raw: str) -> List[str]:
    outer = _loads_maybe_json(f_req_raw)
    if outer is None:
        return []
    found: List[str] = []
    seen: set[str] = set()

    def scan(value: Any, depth: int = 0) -> None:
        if depth > _MAX_DEPTH:
            return
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("c_") and text not in seen:
                seen.add(text)
                found.append(text)
            inner = _loads_maybe_json(value)
            if inner is not None:
                scan(inner, depth + 1)
            return
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip().startswith("c_"):
                    uid = item.strip()
                    if uid not in seen:
                        seen.add(uid)
                        found.append(uid)
                scan(item, depth + 1)

    scan(outer, 0)
    return found


def extract_session_id(req: Dict[str, Any], payload: bytes) -> str:
    cached = str(req.get("_gemini_session_id") or "").strip()
    if cached:
        return cached

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "")
    path = str(meta.get("path") or "")
    if is_google_ai_mode_request(host, path):
        return _remember_google_ai_mode_event(req, path)

    direction = str(req.get("direction") or "").lower()
    if direction == "request":
        sid = _extract_conversation_id_from_payload(payload)
        if sid:
            req["_gemini_session_id"] = sid
        return sid

    if direction == "response":
        out_sid = str(req.get("_gemini_output_session_id") or "").strip()
        if out_sid:
            return out_sid
        _, session_id, _ = _extract_response_text(payload)
        if session_id:
            req["_gemini_output_session_id"] = session_id
            return session_id

    return ""


def collect_session_ids_for_masking_clear(
    req: Dict[str, Any], payload: bytes
) -> List[str]:
    if str(req.get("direction") or "").lower() != "request":
        return []
    form_text = _safe_utf8(payload)
    if "f.req=" not in form_text:
        return []
    params = parse_qs(form_text, keep_blank_values=True, strict_parsing=False)
    values = params.get("f.req") or []
    if not values:
        return []
    return _collect_conversation_ids_from_f_req(values[0])


def _js_utf16_len(text: str) -> int:
    length = 0
    for ch in text:
        length += 2 if ord(ch) > 0xFFFF else 1
    return length


def _gemini_frame_wrap(json_frame: str) -> str:
    inner = f"\n{json_frame}\n"
    return f"{_js_utf16_len(inner)}\n{json_frame}\n"


def _reassemble_gemini_transport_body(frames: List[str], xssi_prefix: str = "") -> bytes:
    body = xssi_prefix + "".join(_gemini_frame_wrap(frame) for frame in frames)
    return body.encode("utf-8")


def _is_valid_gemini_session_id(session_id: str) -> bool:
    sid = (session_id or "").strip()
    return bool(sid) and sid.startswith("c_") and not sid.startswith("pending:")


def _block_message_already_in_text(response_text: str, block_text: str) -> bool:
    body = (response_text or "").strip()
    notice = (block_text or "").strip()
    if not body or not notice:
        return False
    if "[AIVAX]" in body or "SNIPER AIVAX" in body:
        return True
    # block_message 앞부분이 응답에 포함되면 Gemini가 이미 차단 문구를 출력한 것.
    head = notice.replace("\r\n", "\n").strip()
    for line in head.split("\n"):
        line = line.strip()
        if len(line) >= 12 and line in body:
            return True
    if len(head) >= 24 and head[:24] in body:
        return True
    return False


def response_already_shows_block(
    req: Dict[str, Any],
    payload: bytes,
    block_text: str,
) -> bool:
    """요청 rewrite로 Gemini가 차단 문구를 이미 출력했으면 response override 생략."""
    if not _is_gemini_stream_response(req):
        return False
    text, _, _ = _extract_response_text(payload)
    return _block_message_already_in_text(text, block_text)


def _rewrite_inner_text(
    inner: Any,
    replacement_text: str,
    first_of_stream: Optional[List[bool]] = None,
) -> bool:
    """wrb.fr inner의 rc_* 텍스트를 치환한다."""
    if not isinstance(inner, list) or len(inner) <= 4:
        return False

    nodes = inner[4]
    if not isinstance(nodes, list):
        return False

    changed = False
    for node in nodes:
        if not isinstance(node, list) or len(node) < 2:
            continue
        if not (isinstance(node[0], str) and node[0].startswith("rc_")):
            continue
        text_arr = node[1]
        if not isinstance(text_arr, list):
            continue

        if first_of_stream is None:
            if not text_arr:
                text_arr.append(replacement_text)
                return True
            if isinstance(text_arr[0], str):
                text_arr[0] = replacement_text
                while len(text_arr) > 1:
                    text_arr.pop()
                return True
            return False

        if not first_of_stream[0]:
            while len(text_arr) > 1:
                text_arr.pop()
            if not text_arr:
                text_arr.append(replacement_text)
            else:
                text_arr[0] = replacement_text
            first_of_stream[0] = True
            changed = True
        else:
            text_arr.clear()
            changed = True

    return changed


def _rebuild_wrb_payload(payload: bytes, modified_frames: List[str]) -> bytes:
    has_xssi = False
    raw = payload.lstrip()
    if raw.startswith(_XSSI_PREFIX) or (raw[:1] == b")" and b"'" in raw[:10]):
        has_xssi = True

    parts: List[str] = []
    if has_xssi:
        parts.append(")]}'\n\n")

    for frame_json in modified_frames:
        frame_content = f"\n{frame_json}\n"
        size = len(frame_content)
        parts.append(f"{size}{frame_content}")

    return "".join(parts).encode("utf-8")


def rewrite_response(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
) -> Optional[Tuple[bytes, str]]:
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return None

    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    # Envoy INLINE uses unit_type=json_body for non-SSE; CT is often
    # application/json; charset=utf-8 (and sometimes text/plain).
    if not (
        unit_type in ("json_body", "raw", "raw_body", "")
        or "application/json" in content_type
        or "text/plain" in content_type
        or not content_type
    ):
        return None

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "")
    path = str(meta.get("path") or "")
    if is_google_ai_mode_request(host, path) and is_google_ai_mode_answer_path(path):
        notice = html.escape((replacement_text or "").strip())
        if not notice:
            return None
        raw_head = _safe_utf8(payload).lstrip()
        prefix = ")]}'\n\n" if raw_head.startswith(")]}'") else ""
        body = f'{prefix}<div id="aivax-notice">{notice}</div>\n'
        return body.encode("utf-8"), "adapter:google_ai_mode_notice"

    raw = _safe_utf8(payload)
    if "wrb.fr" not in raw:
        return None

    frames = _iter_wrb_frames(payload)
    if not frames:
        return None

    modified_frames: List[str] = []
    changed = False
    first_of_stream: List[bool] = [False]

    for frame_str in frames:
        stripped = frame_str.strip()
        if not stripped:
            continue
        try:
            outer = json.loads(stripped)
        except Exception:
            modified_frames.append(stripped)
            continue

        if not isinstance(outer, list):
            modified_frames.append(stripped)
            continue

        frame_changed = False
        for item in outer:
            if not (isinstance(item, list) and len(item) > 2 and item[0] == "wrb.fr"):
                continue
            inner_raw = item[2]
            if not isinstance(inner_raw, str):
                continue
            inner = _loads_maybe_json(inner_raw)
            if inner is None or not isinstance(inner, list):
                continue
            if _rewrite_inner_text(inner, replacement_text, first_of_stream):
                item[2] = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
                frame_changed = True

        if frame_changed:
            modified_frames.append(json.dumps(outer, ensure_ascii=False, separators=(",", ":")))
            changed = True
        else:
            modified_frames.append(stripped)

    if not changed:
        # Thinking-only / atypical frames: still force a single wrb.fr answer frame
        # so BLOCK response_override can replace CoT with the AIVAX notice.
        synth = _synthesize_gemini_block_wrb(
            replacement_text,
            session_id=str(req.get("_gemini_output_session_id") or req.get("_gemini_session_id") or ""),
            message_id=str(req.get("_gemini_output_message_id") or ""),
            xssi=bool(raw.lstrip().startswith(")]}'") or raw.lstrip()[:1] == ")"),
        )
        if synth:
            return synth, "adapter:gemini_policy_block_message_synth"
        return None

    result = _rebuild_wrb_payload(payload, modified_frames)
    return result, "adapter:gemini_policy_block_message"


def _synthesize_gemini_block_wrb(
    replacement_text: str,
    session_id: str = "",
    message_id: str = "",
    xssi: bool = True,
) -> Optional[bytes]:
    """Build a minimal StreamGenerate body carrying only the block notice."""
    notice = (replacement_text or "").strip()
    if not notice:
        return None
    sid = session_id if session_id.startswith("c_") else (session_id or "c_aivax_block")
    mid = message_id or "r_aivax_block"
    # Compact inner: ids at [1], rc text at [4] — matches extract/rewrite expectations.
    inner = [
        None,
        [sid, mid],
        None,
        None,
        [["rc_aivax_block", [notice], None, None, None, None, None, None, [2]]],
    ]
    outer = [["wrb.fr", None, json.dumps(inner, ensure_ascii=False, separators=(",", ":"))]]
    frame = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))
    return _reassemble_gemini_transport_body([frame], xssi_prefix=")]}'\n\n" if xssi else "")


def response_stream_done(req: Dict[str, Any], payload: bytes) -> bool:
    direction = (req.get("direction") or "").lower()
    if direction != "response":
        return False

    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    if not (
        unit_type in ("json_body", "raw", "raw_body", "")
        or "application/json" in content_type
        or "text/plain" in content_type
        or not content_type
    ):
        return False

    if _is_gemini_process_session(req):
        text, _, _ = _extract_process_session_response(payload)
        return bool(text)

    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    if is_google_ai_mode_answer_path(str(meta.get("path") or "")) and is_google_search_host(
        str(meta.get("host") or "")
    ):
        return bool(google_ai_mode_answer(payload))

    raw = _safe_utf8(payload)
    if '"di",' in raw and '"e",' in raw:
        return True

    frames = _iter_wrb_frames(payload)
    for frame_str in frames:
        try:
            outer = json.loads(frame_str.strip())
        except Exception:
            continue
        if not isinstance(outer, list):
            continue
        for item in outer:
            if not (isinstance(item, list) and len(item) > 2 and item[0] == "wrb.fr"):
                continue
            inner_raw = item[2]
            if not isinstance(inner_raw, str):
                continue
            inner = _loads_maybe_json(inner_raw)
            if not isinstance(inner, list) or len(inner) <= 4:
                continue
            nodes = inner[4]
            if not isinstance(nodes, list):
                continue
            for node in nodes:
                if not isinstance(node, list) or len(node) < 9:
                    continue
                if isinstance(node[0], str) and node[0].startswith("rc_"):
                    marker = node[8]
                    if isinstance(marker, list) and marker == [2]:
                        return True

    return False


def clear_response_override_state(session_id: str) -> None:
    if session_id:
        _GEMINI_RESPONSE_OVERRIDE_EMITTED.pop(session_id, None)


def _rewrite_first_prompt_node(value: Any, replacement_text: str, depth: int = 0) -> int:
    """
    Conservative fallback rewrite:
    - rewrite only the first prompt-like node
    - avoid touching token/id-like strings
    """
    if depth > _MAX_DEPTH:
        return 0

    if isinstance(value, list):
        if len(value) >= 2 and isinstance(value[0], str) and not isinstance(value[1], str):
            current = value[0].strip()
            if current and not _looks_like_token(current):
                if value[0] != replacement_text:
                    value[0] = replacement_text
                    return 1
                return 0
        for child in value:
            changed = _rewrite_first_prompt_node(child, replacement_text, depth + 1)
            if changed > 0:
                return changed
        return 0

    if isinstance(value, dict):
        for key in ("prompt", "query", "input"):
            current = value.get(key)
            if isinstance(current, str):
                if current != replacement_text:
                    value[key] = replacement_text
                    return 1
                return 0
        for child in value.values():
            changed = _rewrite_first_prompt_node(child, replacement_text, depth + 1)
            if changed > 0:
                return changed

    return 0


def _is_user_prompt_tuple(item: Any) -> bool:
    """Gemini user turn: ["<prompt>", 0|1|2, null, ...] — not ttl file tuples.

    Classic Flash uses sentinel 0; Pro Extended / thinking modes may use 1 or 2.
    """
    if not isinstance(item, list) or not item:
        return False
    head = item[0]
    if not isinstance(head, str):
        return False
    text = head.strip()
    if not text:
        return False
    if "/contrib_service/" in text or "ttl_1d" in text or text.startswith("c_"):
        return False
    if _looks_like_token(text):
        return False
    return len(item) >= 2 and isinstance(item[1], int) and item[1] in (0, 1, 2)


def _collect_user_prompt_tuples(value: Any, out: List[list], depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, list):
        if _is_user_prompt_tuple(value):
            out.append(value)
        for child in value:
            _collect_user_prompt_tuples(child, out, depth + 1)
    elif isinstance(value, dict):
        for child in value.values():
            _collect_user_prompt_tuples(child, out, depth + 1)


def _rewrite_last_user_prompt(value: Any, replacement_text: str) -> int:
    prompts: List[list] = []
    _collect_user_prompt_tuples(value, prompts)
    if not prompts:
        return 0
    target = prompts[-1]
    if target[0] == replacement_text:
        return 0
    target[0] = replacement_text
    return 1


_GEMINI_CONSENT_PHRASES = frozenset({"동의", "동의합니다"})
_CONSENT_TRAILING = " \t.。!！?？~…,，"


def _is_consent_phrase(text: str) -> bool:
    return str(text or "").strip().strip(_CONSENT_TRAILING).strip() in _GEMINI_CONSENT_PHRASES
_GEMINI_BLOCK_RESTORE_MARKERS = (
    "[AIVAX]",
    "[AIVAX MASKING]",
    "프롬프트 차단",
    "[AIVAX 정책 응답 처리]",
    "다음 메시지에 '동의'",
    "'동의' 또는 '동의합니다' 입력 시",
    "[입력 프롬프트]",
)


def _is_gemini_consent_restore_slot(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if _is_consent_phrase(raw):
        return True
    return any(marker in raw for marker in _GEMINI_BLOCK_RESTORE_MARKERS)


def _rewrite_consent_user_prompts(value: Any, replacement_text: str) -> int:
    """Restore 동의 and at most the nearest previous BLOCK-notice user slot.

    Walking every nested tuple used to rewrite assistant/history fragments that
    only *contain* 안내문 마커. That shrinks StreamGenerate by kilobytes and
    Google RST the HTTP/2 stream (Envoy: reset reason: protocol error).
    """
    prompts: List[list] = []
    _collect_user_prompt_tuples(value, prompts)
    if not prompts:
        return 0
    ack_idx: Optional[int] = None
    for idx in range(len(prompts) - 1, -1, -1):
        if _is_consent_phrase(str(prompts[idx][0] or "")):
            ack_idx = idx
            break
    if ack_idx is None:
        return 0
    changed = 0
    if prompts[ack_idx][0] != replacement_text:
        prompts[ack_idx][0] = replacement_text
        changed += 1
    for idx in range(ack_idx - 1, -1, -1):
        head = str(prompts[idx][0] or "")
        if not any(marker in head for marker in _GEMINI_BLOCK_RESTORE_MARKERS):
            continue
        if prompts[idx][0] != replacement_text:
            prompts[idx][0] = replacement_text
            changed += 1
        break
    return changed


def _rewrite_primary_prompt(value: Any, replacement_text: str) -> int:
    """
    Rewrite only the primary Gemini user prompt slot if found.
    Expected shape: inner[0][0] == "<prompt>".
    """
    if not isinstance(value, list) or not value:
        return 0
    first = value[0]
    if not isinstance(first, list) or not first:
        return 0
    if not isinstance(first[0], str):
        return 0
    if first[0] == replacement_text:
        return 0
    first[0] = replacement_text
    return 1


def _rewrite_f_req(
    f_req_raw: str,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[str, int]]:
    raw = _sanitize_f_req_raw(f_req_raw)
    if not raw:
        return None

    outer = _loads_maybe_json(raw)
    changed = 0

    # Prefer precise rewrite on the latest Gemini user prompt slot.
    if isinstance(outer, list):
        for idx, item in enumerate(outer):
            parsed_from_string = False
            target = item
            if isinstance(item, str):
                parsed = _loads_maybe_json(item)
                if isinstance(parsed, (dict, list)):
                    target = parsed
                    parsed_from_string = True
                else:
                    continue

            if not isinstance(target, (dict, list)):
                continue

            changed = _rewrite_last_user_prompt(target, replacement_text)
            if changed <= 0:
                changed = _rewrite_primary_prompt(target, replacement_text)
            if changed <= 0:
                changed = _rewrite_first_prompt_node(target, replacement_text, 0)
            if changed <= 0:
                continue

            if parsed_from_string:
                outer[idx] = json.dumps(target, ensure_ascii=False, separators=(",", ":"))
            break

    # Fallback for unexpected non-list outer shape.
    if outer is not None:
        if changed <= 0:
            changed = _rewrite_last_user_prompt(outer, replacement_text)
        if changed <= 0:
            changed = _rewrite_primary_prompt(outer, replacement_text)
        if changed <= 0:
            changed = _rewrite_first_prompt_node(outer, replacement_text, 0)

    if changed > 0 and outer is not None:
        updated = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))
        return updated, changed

    return _rewrite_f_req_regex_slot(raw, replacement_text)


def _maybe_log_and_register_gemini_account(req: Dict[str, Any], payload: bytes) -> bool:
    """Log + register Gemini account when email/gaia/psid can be resolved from HTML+cookie."""
    info = extract_account_info(req, payload)
    registered = _register_account_from_body_if_present(req, payload)
    if not info:
        return registered
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "").split("?")[0].strip() or "/"
    email = (info.get("email") or "").strip()
    gaia_id = (info.get("gaia_id") or "").strip()
    psid = (info.get("psid") or "").strip()
    if gaia_id.startswith("-"):
        gaia_id = gaia_id.lstrip("-")
    if email or gaia_id:
        logger.info(
            "[Account Info] gemini: path=%s email=%s name=%s gaia_id=%s psid=%s",
            path,
            email or "-",
            (info.get("name") or "-"),
            gaia_id or "-",
            psid or "-",
        )
    elif psid:
        logger.info(
            "[Account Info] gemini: path=%s psid=%s (awaiting email/gaia)",
            path,
            psid,
        )
    return registered or bool(email or gaia_id or psid)


def extract(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    direction = (req.get("direction") or "").lower()
    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = (str(meta.get("host") or "")).lower()
    path = str(meta.get("path") or "")

    if (
        "gemini.google.com" in host
        or "bard.google.com" in host
        or _is_google_list_accounts_response(req)
    ):
        _register_account_from_body_if_present(req, payload)

    # Account discovery is metadata, never an AI model response shown in the UI.
    if _is_google_list_accounts_response(req):
        info = extract_account_info(req, payload) or {}
        logger.info(
            "[Account Info] gemini: ListAccounts email=%s name=%s gaia_id=%s psid=%s",
            info.get("email") or "-",
            info.get("name") or "-",
            info.get("gaia_id") or "-",
            info.get("psid") or "-",
        )
        req["_extract_final"] = False
        return ""

    if _is_gemini_account_rpc_response(req):
        info = extract_account_info(req, payload) or {}
        logger.info(
            "[Account Info] gemini: rpc=%s email=%s gaia_id=%s psid=%s",
            _GEMINI_ACCOUNT_RPC_ID,
            info.get("email") or "-",
            info.get("gaia_id") or "-",
            info.get("psid") or "-",
        )
        req["_extract_final"] = False
        return ""

    # Gemini app: skip output filter for /, /app, /app/{id} or typography CSS body. Log account only for /, /app, /app/{id}.
    if direction == "response" and _is_account_page_response(req, payload):
        meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        path_raw = str(meta.get("path") or "").strip().lower() or "/"
        if _is_gemini_app_response_path(path_raw):
            _maybe_log_and_register_gemini_account(req, payload)
        req["_extract_final"] = False
        return ""

    if is_google_ai_mode_request(host, path):
        event_id = _remember_google_ai_mode_event(req, path)
        if direction == "response":
            if not is_google_ai_mode_answer_path(path):
                req["_extract_final"] = False
                return ""
            if event_id:
                req["_gemini_output_session_id"] = event_id
            text = google_ai_mode_answer(payload)
            req["_extract_final"] = bool(text)
            if text:
                logger.info(
                    "gemini_response ai_mode extracted_len=%d session=%s",
                    len(text),
                    event_id or "-",
                )
            return text
        if direction == "request":
            return google_ai_mode_prompt(path)

    if direction == "response" and _is_gemini_process_session(req):
        text, session_id, message_id = _extract_process_session_response(payload)
        _bind_process_session_response(req, session_id)
        if message_id:
            req["_gemini_output_message_id"] = message_id
        _note_process_session_model(req, payload, session_id)
        if text:
            # The answer grows across later long-polls. The first non-empty
            # snapshot is not the reply the user will see.
            req["_gemini_response_growing"] = True
            req["_extract_final"] = False
            logger.info(
                "gemini_response processSession extracted_len=%d session=%s",
                len(text),
                session_id or "-",
            )
            return text
        return ""

    if direction == "response":
        # Envoy INLINE uses unit_type=json_body for non-SSE (wrb.fr is often text/plain).
        if (
            unit_type in ("json_body", "raw", "raw_body", "")
            or "application/json" in content_type
            or "text/plain" in content_type
            or not content_type
        ):
            text, session_id, message_id = _extract_response_text(payload)
            if session_id and not text and _is_gemini_stream_generate_path(req):
                _remember_stream_turn_parent(req, session_id)
            if session_id:
                req["_gemini_output_session_id"] = session_id
                _maybe_remap_gemini_ui_message_id(req, session_id)
            if message_id:
                req["_gemini_output_message_id"] = message_id
            if text:
                return text
            raw = _safe_utf8(payload)
            # Avoid sending full Gemini transport frames as "response text".
            if "wrb.fr" in raw:
                return ""
            compact_head = raw.lstrip()[:20].replace("\n", "").replace("\r", "")
            if             compact_head.startswith(")]}'"):
                return ""
            # Never treat transport framing, headers, cookies, or an
            # unrecognized Gemini envelope as model output.
            return ""
        return None

    if direction != "request":
        return None

    if _is_gemini_process_session(req):
        return _extract_process_session_prompt(req, payload)

    if not _is_gemini_policy_form_request(req, payload):
        return None

    form_text = _safe_utf8(payload)
    params = parse_qs(form_text, keep_blank_values=True, strict_parsing=False)
    f_req_values = params.get("f.req")
    if not f_req_values:
        return ""

    # StreamGenerate POST: resolve account via cookie psid → account_lookup (email/gaia).
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = (str(meta.get("host") or "")).lower()
    path = str(meta.get("path") or "")
    if (_GEMINI_ACCOUNT_HOST_SUBSTR in host or "bard.google.com" in host) and "StreamGenerate" in path:
        cookie_str = str(meta.get("cookie") or "").strip()
        if cookie_str.upper() == "[REDACTED]":
            cookie_str = ""
        psid = _extract_psid_sid_from_cookie(cookie_str)
        user_id = email = ""
        if psid and _account_lookup:
            info = _account_lookup.get("gemini", psid)
            # Mirror SHM only: set AIVAX_GEMINI_ACCOUNT_WAIT_MS (Inline leaves unset → no wait).
            if (not info or not (info[0] or info[1])) and os.environ.get(
                "AIVAX_GEMINI_ACCOUNT_WAIT_MS"
            ):
                # o30O0e often lands on another SHM worker a moment later after refresh.
                info = _account_lookup.wait_for(
                    "gemini",
                    [psid],
                    env_name="AIVAX_GEMINI_ACCOUNT_WAIT_MS",
                )
            if info:
                user_id, email = info[0], info[1]
        if psid:
            if user_id or email:
                logger.info(
                    "[Account Info] gemini: StreamGenerate user_id=%s email=%s psid=%s",
                    user_id or "-",
                    email or "-",
                    psid,
                )
            else:
                logger.info(
                    "[Account Info] gemini: StreamGenerate cookie psid=%s (no email/gaia yet)",
                    psid,
                )

    prompt = _extract_prompt_from_f_req(f_req_values[0])
    sid = _extract_conversation_id_from_f_req(f_req_values[0])
    if sid:
        req["_gemini_session_id"] = sid
    if prompt and sid:
        register_gemini_turn_parent(sid, str(req.get("request_id") or ""))
    if "StreamGenerate" in path:
        logger.info(
            "gemini_prompt StreamGenerate extracted_len=%d session=%s preview=%r",
            len(prompt or ""),
            sid or "-",
            (prompt[:160] if prompt else ""),
        )
        if not (prompt or "").strip():
            _dump_empty_gemini_stream_body(req, payload, "req_no_prompt")
    _segs = _extract_gemini_ttl_segments(payload)
    if prompt:
        return prompt
    if _segs:
        return ""
    # f.req/at/rpcids are transport metadata, not user content. Returning the
    # full form here causes token/API-key false positives in the policy API.
    return ""


def extract_full_body(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    """Suricata eve / Deep INLINE response body: wrb.fr text, or Gemini app HTML account extract.

    Deep INLINE sets `_eve_full_body=true`, so account-page HTML must be handled here
    (not only in extract()). Pro Extended answers may arrive on a later StreamGenerate
    HTTP than the prompt — remap UI message_id via conversation c_*.
    """
    direction = (req.get("direction") or "").lower()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "")
    path = str(meta.get("path") or "")
    if is_google_ai_mode_request(host, path):
        event_id = _remember_google_ai_mode_event(req, path)
        if event_id:
            req["_gemini_output_session_id"] = event_id
        if direction != "response" or not is_google_ai_mode_answer_path(path):
            req["_extract_final"] = False
            return ""
        text = google_ai_mode_answer(payload)
        req["_extract_final"] = bool(text)
        if text:
            logger.info(
                "gemini_response ai_mode extracted_len=%d session=%s",
                len(text),
                event_id or "-",
            )
            return text
        return ""
    if direction == "response" and _is_gemini_account_rpc_response(req):
        _maybe_log_and_register_gemini_account(req, payload)
        req["_extract_final"] = False
        return ""
    if direction == "response" and _is_account_page_response(req, payload):
        meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        path_raw = str(meta.get("path") or "").strip().lower() or "/"
        if _is_gemini_app_response_path(path_raw):
            _maybe_log_and_register_gemini_account(req, payload)
        else:
            _register_account_from_body_if_present(req, payload)
        req["_extract_final"] = False
        return ""

    _register_account_from_body_if_present(req, payload)
    if direction != "response":
        return None
    if _is_gemini_process_session(req):
        text, session_id, message_id = _extract_process_session_response(payload)
        _bind_process_session_response(req, session_id)
        if message_id:
            req["_gemini_output_message_id"] = message_id
        # Inline reads this path, not extract(). An early snapshot carries the
        # display name and a later longer snapshot omits it.
        _note_process_session_model(req, payload, session_id)
        if text:
            req["_gemini_response_growing"] = True
            req["_extract_final"] = False
            logger.info(
                "gemini_response processSession extracted_len=%d session=%s",
                len(text),
                session_id or "-",
            )
            return text
        req["_extract_final"] = False
        return ""
    text, session_id, message_id = _extract_response_text(payload)
    if session_id and not text and _is_gemini_stream_generate_path(req):
        _remember_stream_turn_parent(req, session_id)
    if session_id:
        req["_gemini_output_session_id"] = session_id
        _maybe_remap_gemini_ui_message_id(req, session_id)
    if message_id:
        req["_gemini_output_message_id"] = message_id
    if text:
        req["_extract_final"] = True
        logger.info(
            "gemini_response StreamGenerate extracted_len=%d session=%s",
            len(text),
            session_id or "-",
        )
        return text
    if _is_gemini_stream_generate_path(req) and payload and len(payload) > 200:
        # Short thinking stub / Pro Extended handoff with no user-visible answer yet.
        _dump_empty_gemini_stream_body(req, payload, "resp_empty")
        if session_id:
            _maybe_remap_gemini_ui_message_id(req, session_id)
        req["_gemini_thinking_only"] = True
        req["_extract_final"] = False
        logger.info(
            "gemini_response StreamGenerate thinking_only_or_empty bytes=%d session=%s",
            len(payload),
            session_id or "-",
        )
    return None


def build_policy_api_payload(req: Dict[str, Any], payload: bytes, extracted_text: str) -> Optional[Dict[str, Any]]:
    direction = (req.get("direction") or "").lower()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    if direction == "response":
        user_id = ""
        email = ""
        psid = ""
        if _account_lookup:
            cookie_str = str(meta.get("cookie") or "").strip()
            if cookie_str.upper() == "[REDACTED]":
                cookie_str = ""
            psid = _extract_psid_sid_from_cookie(cookie_str) or ""
            if psid:
                info = _account_lookup.get("gemini", psid)
                if (not info or not (info[0] or info[1])) and os.environ.get(
                    "AIVAX_GEMINI_ACCOUNT_WAIT_MS"
                ):
                    info = _account_lookup.wait_for(
                        "gemini",
                        [psid],
                        env_name="AIVAX_GEMINI_ACCOUNT_WAIT_MS",
                    )
                if info:
                    user_id, email = info[0], info[1]
        response_payload = {
            "llm_output": extracted_text,
            "user_id": user_id,
            "email": email,
            "ai_service": AI_SERVICE_GEMINI,
            "client_host": str(meta.get("host") or ""),
            "session_id": str(req.get("_gemini_output_session_id") or ""),
            "message_id": str(req.get("_gemini_output_message_id") or ""),
            "model": str(req.get("_gemini_model") or ""),
        }
        response_name = _gemini_cached_name(psid)
        if response_name:
            response_payload["user_name"] = response_name
        return response_payload
    if direction != "request":
        return None
    prompt = (extracted_text or "").strip()
    file_only = bool(req.get("_gemini_file_only"))
    process_session = _is_gemini_process_session(req)
    # A text turn must not inherit a file already recorded on the file-only row.
    # The file-only row itself resolves the TTL path in this same POST.
    attachments = build_policy_attachments(
        payload,
        req,
        extra_segments=(
            _fresh_process_session_segments(payload, file_only) if process_session else None
        ),
        skip_claimed=process_session and not file_only,
    )
    if process_session and prompt and not file_only:
        fresh = claim_recent_gemini_attachments(req=req)
        if fresh:
            kept = [
                item
                for item in attachments
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            ]
            attachments = kept + fresh
            logger.info(
                "gemini processSession attachments count=%d names=%s",
                len(fresh),
                ",".join(str(item.get("name") or "")[:80] for item in fresh),
            )
    has_attachment = any(
        isinstance(item, dict)
        and bool(str(item.get("id") or item.get("name") or "").strip())
        for item in attachments
    )
    if file_only and not has_attachment:
        # The TTL alias was missing. This client's own recent upload is the file.
        fresh = claim_recent_gemini_attachments(req=req)
        if fresh:
            attachments = fresh
            has_attachment = True
            logger.info(
                "gemini processSession file_only fallback count=%d names=%s",
                len(fresh),
                ",".join(str(item.get("name") or "")[:80] for item in fresh),
            )
    if not prompt:
        if not has_attachment:
            return None
        prompt = "사용자가 파일을 업로드하였습니다."
    user_id = ""
    email = ""
    psid = ""
    if _account_lookup:
        cookie_str = str(meta.get("cookie") or "").strip()
        if cookie_str.upper() == "[REDACTED]":
            cookie_str = ""
        psid = _extract_psid_sid_from_cookie(cookie_str) or ""
        if psid:
            info = _account_lookup.get("gemini", psid)
            if info:
                user_id, email = info[0], info[1]
        # PSID is a rotating cookie correlator, not the account.
        # Identity comes from the account-page email/gaia mapping.
    # /search?udm=50 is the AI Mode shell. The same prompt is logged again,
    # with the answer, on /async/folif. Persisting the shell leaves an empty row.
    path = str(meta.get("path") or "")
    filter_list = ["input_filter", "file_block_filter", "secret_filter", "slm_filter"]
    if is_google_ai_mode_request(
        str(meta.get("host") or ""), path
    ) and not is_google_ai_mode_answer_path(path):
        filter_list = [name for name in filter_list if name != "input_filter"]
    request_payload = {
        "filter_list": filter_list,
        "prompt": prompt,
        "encoding": False,
        "user_id": user_id,
        "email": email,
        "ai_service": AI_SERVICE_GEMINI,
        "client_host": str(meta.get("host") or ""),
        "session_id": extract_session_id(req, payload),
        "attachments": attachments,
        "message_id": "",
    }
    request_name = _gemini_cached_name(psid)
    if request_name:
        request_payload["user_name"] = request_name
    return request_payload


def _rewrite_gemini_form_f_req_prompt_only(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    reason_suffix: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    """f.req의 사용자 프롬프트만 _rewrite_f_req로 교체(첨부 슬롯은 strip 시 null)."""
    direction = (req.get("direction") or "").lower()
    if direction != "request":
        return None
    if _is_gemini_process_session(req):
        rewritten_body = _rewrite_process_session_form(payload, replacement_text)
        if rewritten_body is None:
            return None
        tag = (reason_suffix or "masking").strip().lower()
        return rewritten_body, f"adapter:gemini_policy_{tag}"
    if not _is_gemini_policy_form_request(req, payload):
        return None

    form_text = _safe_utf8(payload)
    pairs = parse_qsl(form_text, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        return None

    changed = 0
    out_pairs = []
    for key, value in pairs:
        if key == "f.req":
            rewritten = _rewrite_form_f_req_value(value, replacement_text, source_prompt)
            if rewritten is not None:
                value, cnt = rewritten
                changed += cnt
        out_pairs.append((key, value))

    if changed <= 0:
        return None

    encoded_form = urlencode(out_pairs, doseq=True).encode("utf-8")
    tag = (reason_suffix or "masking").strip().lower()
    return encoded_form, f"adapter:gemini_policy_{tag}"


def _rewrite_form_f_req_value(
    f_req_raw: str,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[str, int]]:
    """Rewrite primary prompt and strip file attachments for MASK/BLOCK upstream send."""
    raw = _sanitize_f_req_raw(f_req_raw)
    if not raw:
        return None

    outer = _loads_maybe_json(raw)
    if isinstance(outer, list):
        changed = 0
        for idx, item in enumerate(outer):
            if not isinstance(item, str):
                continue
            parsed = _loads_maybe_json(item)
            if not isinstance(parsed, (dict, list)):
                continue
            prompt_changed = _rewrite_last_user_prompt(parsed, replacement_text)
            if prompt_changed <= 0:
                prompt_changed = _rewrite_primary_prompt(parsed, replacement_text)
            if prompt_changed <= 0:
                prompt_changed = _rewrite_first_prompt_node(parsed, replacement_text, 0)
            strip_cnt = _strip_parsed_f_req_inner(parsed)
            if prompt_changed > 0 or strip_cnt > 0:
                outer[idx] = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
                changed += prompt_changed if prompt_changed > 0 else 0
                if strip_cnt > 0 and prompt_changed <= 0:
                    changed += 1
        if changed > 0:
            encoded = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))
            if _loads_maybe_json(encoded) is not None:
                return encoded, changed

    rewritten = _rewrite_f_req(f_req_raw, replacement_text, source_prompt)
    base = f_req_raw
    cnt = 0
    if rewritten is not None:
        base, cnt = rewritten
    stripped, strip_cnt = strip_f_req_attachments(base)
    if cnt <= 0 and strip_cnt <= 0:
        return None
    if _loads_maybe_json(stripped) is None:
        return None
    return stripped, cnt if cnt > 0 else 1


# 안내문과 모델 지시 사이 빈 줄(휴리스틱: 일부 UI에서 아래쪽이 접히거나 잘 안 보일 수 있음; 보장 아님).
_BLOCK_SYSINST_VERTICAL_GAP = "\n" * 1


def rewrite_prompt_for_block(
    req: Dict[str, Any],
    payload: bytes,
    user_prompt: str,
    system_instruction: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    """
    BLOCK 전용: 차단 안내문(user_prompt) 뒤에 system_instruction을 붙여 primary 한 칸에 넣는다.
    inner Bard 배열의 다른 인덱스는 건드리지 않는다. 첨부 슬롯(user turn index 3)은 strip 시 null.
    MASKING은 rewrite_prompt_masking만 사용.
    """
    direction = (req.get("direction") or "").lower()
    if direction != "request":
        return None

    sys_t = (system_instruction or "").strip()
    user_t = (user_prompt or "").strip()
    if sys_t and user_t:
        combined = f"{user_t}\n{_BLOCK_SYSINST_VERTICAL_GAP}{sys_t}"
    else:
        combined = user_t or sys_t

    if _is_gemini_process_session(req):
        rewritten = _rewrite_process_session_form(payload, combined)
        if rewritten is None:
            return None
        # The file bytes already reached Google. Dropping the path is what
        # stops the model from reading a blocked attachment.
        return _strip_process_session_ttl(rewritten), "adapter:gemini_policy_block_sysinstruct"

    if not _is_gemini_policy_form_request(req, payload):
        return None

    form_text = _safe_utf8(payload)
    pairs = parse_qsl(form_text, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        return None

    src_prompt = (source_prompt or "").strip()
    changed = 0
    out_pairs = []
    for key, value in pairs:
        if key == "f.req":
            rewritten = _rewrite_form_f_req_value(value, combined, src_prompt)
            if rewritten is not None:
                value, cnt = rewritten
                changed += cnt
        out_pairs.append((key, value))

    if changed <= 0:
        return None

    encoded_form = urlencode(out_pairs, doseq=True).encode("utf-8")
    return encoded_form, "adapter:gemini_policy_block_sysinstruct"


def rewrite_prompt(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    policy_code: str,
) -> Optional[Tuple[bytes, str]]:
    code = (policy_code or "masking").strip().lower()
    return _rewrite_gemini_form_f_req_prompt_only(req, payload, replacement_text, code)


def rewrite_prompt_masking(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    """정책 MASKING 전용: f.req primary prompt만 _rewrite_f_req로 치환."""
    return _rewrite_gemini_form_f_req_prompt_only(
        req, payload, replacement_text, "masking", source_prompt
    )


def rewrite_prompt_for_consent(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    """Consent: put the original blocked prompt in 동의 and prior BLOCK-notice slots."""
    _ = source_prompt
    direction = (req.get("direction") or "").lower()
    if direction != "request":
        return None
    if _is_gemini_process_session(req):
        rewritten_body = _rewrite_process_session_form(payload, replacement_text)
        if rewritten_body is None:
            return None
        return rewritten_body, "adapter:gemini_policy_consent"
    if not _is_gemini_policy_form_request(req, payload):
        return None
    form_text = _safe_utf8(payload)
    pairs = parse_qsl(form_text, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        return None

    changed = 0
    out_pairs = []
    for key, value in pairs:
        if key == "f.req":
            rewritten = _rewrite_form_f_req_consent(value, replacement_text)
            if rewritten is not None:
                value, cnt = rewritten
                changed += cnt
        out_pairs.append((key, value))
    if changed <= 0:
        return None
    encoded_form = urlencode(out_pairs, doseq=True).encode("utf-8")
    return encoded_form, "adapter:gemini_policy_consent"


def _pb_raw_fields(buf: bytes) -> Optional[List[Tuple[int, bytes]]]:
    """(field, raw bytes including the key). None unless every byte parses."""
    out: List[Tuple[int, bytes]] = []
    index = 0
    size = len(buf or b"")
    while index < size:
        start = index
        key, index = _read_pb_varint(buf, index)
        if key is None or (key >> 3) <= 0:
            return None
        wire = key & 7
        if wire == 0:
            value, index = _read_pb_varint(buf, index)
            if value is None:
                return None
        elif wire == 2:
            length, index = _read_pb_varint(buf, index)
            if length is None or index + length > size:
                return None
            index += length
        elif wire == 1:
            index += 8
        elif wire == 5:
            index += 4
        else:
            return None
        if index > size:
            return None
        out.append((key >> 3, buf[start:index]))
    return out


def _pb_ld_body(raw_field: bytes) -> bytes:
    _, index = _read_pb_varint(raw_field, 0)
    length, index = _read_pb_varint(raw_field, index)
    return raw_field[index : index + (length or 0)]


def _process_session_prompt_extras(buf: bytes) -> List[bytes]:
    fields = _pb_raw_fields(buf)
    if not fields:
        return []
    for field, raw in fields:
        if field != 2:
            continue
        inner = _pb_raw_fields(_pb_ld_body(raw))
        if not inner:
            continue
        if not any(f == 9 for f, _ in inner):
            continue
        return [r for f, r in inner if f not in (1, 9)]
    return []


def _user_turn_slots(value: Any) -> List[list]:
    prompts: List[list] = []
    _collect_user_prompt_tuples(value, prompts)
    return [item for item in prompts if len(item) >= 4]


def _f_req_inner_values(f_req_raw: str) -> List[Any]:
    outer = _loads_maybe_json(_sanitize_f_req_raw(f_req_raw))
    if not isinstance(outer, list):
        return []
    values = []
    for item in outer:
        if isinstance(item, str):
            parsed = _loads_maybe_json(item)
            if isinstance(parsed, (dict, list)):
                values.append(parsed)
    return values


def consent_attachment_state(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    """File references of the blocked turn. The upload already reached Google.

    Gemini upload ids expire, so a late 동의 may still be refused upstream.
    """
    if (req.get("direction") or "").lower() != "request":
        return None
    if _is_gemini_process_session(req):
        for key, value in parse_qsl(_safe_utf8(payload), keep_blank_values=True):
            if not _PROCESS_SESSION_DATA_KEY_RE.match(key):
                continue
            buf = _b64_decode_loose(value)
            if not _parse_process_session_prompt(buf):
                continue
            extras = _process_session_prompt_extras(buf)
            return {"kind": "process_session", "extras": extras} if extras else None
        return None
    if not _is_gemini_policy_form_request(req, payload):
        return None
    values = parse_qs(_safe_utf8(payload), keep_blank_values=True).get("f.req") or []
    if not values:
        return None
    for parsed in _f_req_inner_values(values[0]):
        for item in reversed(_user_turn_slots(parsed)):
            if item[3] is not None and item[3] != []:
                return {"kind": "f_req", "slot": json.loads(json.dumps(item[3]))}
    return None


def restore_consent_attachments(
    req: Dict[str, Any],
    payload: bytes,
    state: Any,
    prompt: str = "",
) -> Optional[bytes]:
    if not isinstance(state, dict):
        return None
    pairs = parse_qsl(_safe_utf8(payload), keep_blank_values=True, strict_parsing=False)
    if not pairs:
        return None
    restored = False
    out_pairs = []
    if state.get("kind") == "process_session" and _is_gemini_process_session(req):
        extras = list(state.get("extras") or [])
        for key, value in pairs:
            if not restored and _PROCESS_SESSION_DATA_KEY_RE.match(key):
                buf = _b64_decode_loose(value)
                fields = _pb_raw_fields(buf) if _parse_process_session_prompt(buf) else None
                if fields:
                    rebuilt = bytearray()
                    for field, raw in fields:
                        if field == 2 and not restored:
                            inner = _pb_raw_fields(_pb_ld_body(raw)) or []
                            head = [r for f, r in inner if f == 1]
                            tail = [r for f, r in inner if f != 1]
                            rebuilt += _encode_pb_ld(2, b"".join(head + extras + tail))
                            restored = True
                        else:
                            rebuilt += raw
                    value = base64.b64encode(bytes(rebuilt)).decode("ascii")
            out_pairs.append((key, value))
    elif state.get("kind") == "f_req" and _is_gemini_policy_form_request(req, payload):
        slot = state.get("slot")
        for key, value in pairs:
            if key == "f.req" and not restored:
                outer = _loads_maybe_json(_sanitize_f_req_raw(value))
                if isinstance(outer, list):
                    for idx, item in enumerate(outer):
                        if not isinstance(item, str):
                            continue
                        parsed = _loads_maybe_json(item)
                        if not isinstance(parsed, (dict, list)):
                            continue
                        turns: List[list] = []
                        _collect_user_prompt_tuples(parsed, turns)
                        same = [t for t in turns if prompt and str(t[0]) == prompt]
                        turns = same or turns
                        if not turns:
                            continue
                        target = turns[-1]
                        while len(target) < 4:
                            target.append(None)
                        target[3] = json.loads(json.dumps(slot))
                        outer[idx] = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
                        restored = True
                        break
                    if restored:
                        value = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))
            out_pairs.append((key, value))
    if not restored:
        return None
    return urlencode(out_pairs, doseq=True).encode("utf-8")


def _rewrite_form_f_req_consent(
    f_req_raw: str,
    replacement_text: str,
) -> Optional[Tuple[str, int]]:
    raw = _sanitize_f_req_raw(f_req_raw)
    if not raw:
        return None
    outer = _loads_maybe_json(raw)
    if isinstance(outer, list):
        changed = 0
        for idx, item in enumerate(outer):
            if not isinstance(item, str):
                continue
            parsed = _loads_maybe_json(item)
            if not isinstance(parsed, (dict, list)):
                continue
            prompt_changed = _rewrite_consent_user_prompts(parsed, replacement_text)
            if prompt_changed <= 0:
                prompt_changed = _rewrite_last_user_prompt(parsed, replacement_text)
            if prompt_changed > 0:
                outer[idx] = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
                changed += prompt_changed
        if changed > 0:
            encoded = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))
            if _loads_maybe_json(encoded) is not None:
                return encoded, changed
    if outer is not None:
        changed = _rewrite_consent_user_prompts(outer, replacement_text)
        if changed <= 0:
            changed = _rewrite_last_user_prompt(outer, replacement_text)
        if changed > 0:
            encoded = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))
            return encoded, changed
    return None
