"""
ChatGPT file upload and policy attachment assembly.

Adapter owns the full upload lifecycle (init → binary PUT → policy attachments).
file_upload_manager is storage/registry only; ai_policy_engine must not re-resolve attachments.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import file_upload_manager as _fum

logger = logging.getLogger(__name__)

POLICY_ATTACHMENTS_FINAL = True

_DATA_URL_RE = re.compile(r"^data:([^;,]+)?(?:;[^,]*)?,(.+)$", re.DOTALL | re.IGNORECASE)
_CODEX_FILE_PART_TYPES = frozenset({"input_file", "input_image", "file", "image"})


def _last_codex_user_message(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    inp = obj.get("input")
    if not isinstance(inp, list):
        return None
    for msg in reversed(inp):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").lower()
        mtype = str(msg.get("type") or "message").lower()
        if mtype in ("message", "") and role == "user":
            return msg
    return None


def _decode_data_url(url: str) -> Tuple[str, bytes]:
    m = _DATA_URL_RE.match(str(url or "").strip())
    if not m:
        return "", b""
    mime = str(m.group(1) or "application/octet-stream").strip()
    try:
        return mime, base64.b64decode(m.group(2).strip())
    except Exception:
        return mime, b""


_FILE_BLOCK_RE = re.compile(
    r"<(?:file|code_selection)\b([^>]*)>(.*?)</(?:file|code_selection)>",
    re.IGNORECASE | re.DOTALL,
)
_FILE_ATTR_RE = re.compile(
    r"""(?:path|name|file|filename)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_ACTIVE_FILE_RE = re.compile(r"^## Active file:[ \t]*(.+)$", re.MULTILINE)
_ACTIVE_SELECTION_RE = re.compile(
    r"## Active selection of the file:\n(.*?)(?=\n## |\Z)",
    re.DOTALL,
)
_PATH_ATTR_RE = re.compile(r"""path\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_TOOL_CALL_TYPES = frozenset({"function_call", "custom_tool_call"})
_TOOL_OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})
_READ_HINT_RE = re.compile(
    r"(?:Get-Content|\bcat\b|\btype\b|pdftotext|\bread_text\b|\.read\(|\bopen\()",
    re.IGNORECASE,
)
_QUOTED_PATH_RE = re.compile(r"""["']([^"'\r\n]+\.[A-Za-z0-9]{1,8})["']""")
_FILE_NAME_KEYS = ("path", "file_path", "filename", "file", "target_file", "target")


def _safe_attachment_name(path: str, fallback: str) -> str:
    raw = str(path or "").replace("\\", "/").strip()
    base = os.path.basename(raw).strip().strip(".")
    base = re.sub(r"[\x00-\x1f/]+", "_", base)
    if not base or base in {".", ".."}:
        return fallback
    return base[:180]


def _decode_base64_blob(value: str) -> bytes:
    compact = re.sub(r"\s+", "", str(value or ""))
    if len(compact) < 80:
        return b""
    try:
        raw = base64.b64decode(compact, validate=False)
    except Exception:
        return b""
    if not raw or len(raw) < 32:
        return b""
    return raw


def _codex_inline_from_part(part: Dict[str, Any]) -> Optional[Tuple[str, bytes, str]]:
    req_name = str(part.get("filename") or part.get("file_name") or part.get("name") or "")
    req_mime = str(part.get("mime_type") or part.get("mimeType") or "")
    image_url = part.get("image_url")
    url_str = ""
    if isinstance(image_url, str):
        url_str = image_url.strip()
    elif isinstance(image_url, dict):
        url_str = str(image_url.get("url") or "").strip()
    if url_str.startswith("data:"):
        mime, raw = _decode_data_url(url_str)
        if raw:
            mime = mime or req_mime or "image/png"
            name = req_name or f"chatgpt_app_image.{mime.split('/', 1)[-1] or 'png'}"
            return name, raw, mime
    file_data = part.get("file_data") or part.get("data")
    if isinstance(file_data, str) and file_data.strip():
        blob = file_data.strip()
        if blob.startswith("data:"):
            mime, raw = _decode_data_url(blob)
        else:
            mime, raw = req_mime, _decode_base64_blob(blob)
        if raw:
            mime = mime or req_mime or "application/octet-stream"
            name = req_name or "chatgpt_app_file"
            return name, raw, mime
    image_val = part.get("image")
    if isinstance(image_val, str) and image_val.strip():
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", image_val.strip()))
        except Exception:
            raw = b""
        if raw:
            mime = req_mime or "image/png"
            name = req_name or "chatgpt_app_image.png"
            return name, raw, mime
    return None


def _text_file_payloads(text: str) -> List[Tuple[str, bytes, str]]:
    """File bodies Codex inlines into the user text, outside the visible request."""
    raw = text or ""
    found: List[Tuple[str, bytes, str]] = []
    for match in _FILE_BLOCK_RE.finditer(raw):
        attrs = match.group(1) or ""
        body = (match.group(2) or "").strip("\n")
        named = _FILE_ATTR_RE.search(attrs)
        name = _safe_attachment_name(named.group(1) if named else "", "")
        if name and body.strip():
            found.append((name, body.encode("utf-8"), "text/plain"))
    active = _ACTIVE_FILE_RE.search(raw)
    selection = _ACTIVE_SELECTION_RE.search(raw)
    if active and selection:
        body = (selection.group(1) or "").strip()
        if body and not body.startswith("[Selection truncated"):
            name = _safe_attachment_name(active.group(1).strip(), "")
            if name:
                found.append((name, body.encode("utf-8"), "text/plain"))
    return found


def _store_inline_file(
    name: str,
    raw: bytes,
    mime: str,
    seen: set[str],
    policy: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    if digest in seen:
        return None
    seen.add(digest)
    upload_id = "chatgpt_inline_" + digest[:16]
    saved_path = _fum.save_inline_upload(
        upload_id,
        name or "chatgpt_app_file",
        raw,
        policy,
        allow_unknown_extension=True,
    )
    if not saved_path:
        return None
    saved = _fum.get_saved_attachment(upload_id, fallback_name=name, fallback_mime=mime)
    if not saved:
        return None
    entry = dict(saved)
    entry["name"] = name or str(entry.get("name") or "")
    entry["mime_type"] = mime or str(entry.get("mime_type") or "")
    return entry


def _last_user_parts(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    msg = _last_codex_user_message(obj)
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if isinstance(content, list):
        return [part for part in content if isinstance(part, dict)]
    return []


def _payloads_from_user_parts(parts: List[Dict[str, Any]]) -> List[Tuple[str, bytes, str]]:
    found: List[Tuple[str, bytes, str]] = []
    texts: List[str] = []
    pending_name = ""
    for part in parts:
        ptype = str(part.get("type") or "").lower()
        text = part.get("text")
        if isinstance(text, str) and text.strip() and ptype not in _CODEX_FILE_PART_TYPES:
            texts.append(text)
            named = _PATH_ATTR_RE.search(text)
            if named:
                pending_name = _safe_attachment_name(named.group(1), pending_name)
        if ptype in _CODEX_FILE_PART_TYPES:
            parsed = _codex_inline_from_part(part)
            if parsed:
                name, raw, mime = parsed
                if not name or name.startswith("chatgpt_app_"):
                    name = pending_name or name
                found.append((name, raw, mime))
                pending_name = ""
    for name, raw, mime in _text_file_payloads("\n".join(texts)):
        found.append((name, raw, mime))
    return found


def _save_payloads(payloads: List[Tuple[str, bytes, str]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    policy = _fum.collection_policy_from_request(None)
    for name, raw, mime in payloads:
        entry = _store_inline_file(name, raw, mime, seen, policy)
        if entry:
            out.append(entry)
    if out:
        logger.info(
            "chatgpt codex files saved=%d names=%s",
            len(out),
            [str(item.get("name") or "")[:80] for item in out[:6]],
        )
    return out


def _save_codex_inline_attachments(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Persist files the user turn actually sends: images, input_file, inlined text."""
    return _save_payloads(_payloads_from_user_parts(_last_user_parts(obj)))


def _tool_arguments(call: Dict[str, Any]) -> Dict[str, Any]:
    raw = call.get("arguments")
    if raw is None:
        raw = call.get("input")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {"command": raw}
        return parsed if isinstance(parsed, dict) else {"command": raw}
    if isinstance(raw, str) and raw.strip():
        return {"command": raw}
    return {}


def _filename_from_tool_call(call: Dict[str, Any]) -> str:
    args = _tool_arguments(call)
    for key in _FILE_NAME_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            name = _safe_attachment_name(val, "")
            if name:
                return name
    command = str(args.get("command") or args.get("cmd") or "")
    match = _QUOTED_PATH_RE.search(command)
    if match:
        return _safe_attachment_name(match.group(1), "")
    return ""


def _tool_command_text(call: Dict[str, Any]) -> str:
    args = _tool_arguments(call)
    name = str(call.get("name") or "")
    command = str(args.get("command") or args.get("cmd") or "")
    return f"{name}\n{command}"


def _output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: List[str] = []
        for item in output:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _payloads_from_tool_output(call: Dict[str, Any], item: Dict[str, Any]) -> List[Tuple[str, bytes, str]]:
    output = item.get("output")
    found: List[Tuple[str, bytes, str]] = []
    parts: List[Dict[str, Any]] = []
    if isinstance(output, list):
        parts = [part for part in output if isinstance(part, dict)]
    elif isinstance(output, dict):
        parts = [output]
    elif isinstance(output, str) and output.strip().startswith("{"):
        try:
            parsed = json.loads(output)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            parts = [parsed]
        elif isinstance(parsed, list):
            parts = [part for part in parsed if isinstance(part, dict)]
    filename = _filename_from_tool_call(call)
    for part in parts:
        parsed_part = _codex_inline_from_part(part)
        if not parsed_part:
            continue
        name, raw, mime = parsed_part
        if not name or name.startswith("chatgpt_app_"):
            name = filename or name
        found.append((name, raw, mime))
    text = _output_text(output).strip()
    if text and not found:
        command = _tool_command_text(call)
        looks_read = bool(_READ_HINT_RE.search(command))
        magic = text.startswith("%PDF")
        if filename and (magic or (looks_read and len(text) >= 80)):
            found.append((filename, text.encode("utf-8"), "text/plain"))
    return found


def iter_codex_tool_file_payloads(obj: Dict[str, Any]) -> List[Tuple[str, bytes, str]]:
    """Files a Codex tool result sends back to the model on the follow-up turn."""
    items = obj.get("input")
    if not isinstance(items, list):
        return []
    calls: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").lower() not in _TOOL_CALL_TYPES:
            continue
        call_id = str(item.get("call_id") or item.get("id") or "")
        if call_id:
            calls[call_id] = item
    found: List[Tuple[str, bytes, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").lower() not in _TOOL_OUTPUT_TYPES:
            continue
        call_id = str(item.get("call_id") or "")
        found.extend(_payloads_from_tool_output(calls.get(call_id) or {}, item))
    return found


def codex_tool_followup_has_files(obj: Dict[str, Any]) -> bool:
    return bool(iter_codex_tool_file_payloads(obj))


def save_codex_tool_output_files(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _save_payloads(iter_codex_tool_file_payloads(obj))


def _strip_last_codex_user_attachments(obj: Dict[str, Any]) -> int:
    msg = _last_codex_user_message(obj)
    if not isinstance(msg, dict):
        return 0
    content = msg.get("content")
    if not isinstance(content, list):
        return 0
    new_parts: List[Any] = []
    removed = 0
    for part in content:
        if isinstance(part, dict) and str(part.get("type") or "").lower() in _CODEX_FILE_PART_TYPES:
            removed += 1
            continue
        new_parts.append(part)
    if removed:
        msg["content"] = new_parts
    return removed


def strip_request_attachments(obj: Any) -> int:
    """Remove attachment refs from ChatGPT request JSON before upstream MASK/BLOCK send."""
    if not isinstance(obj, dict):
        return 0
    removed = 0

    top = obj.get("attachments")
    if isinstance(top, list) and top:
        removed += len(top)
        obj["attachments"] = []

    for key in ("file_ids", "attachment_ids"):
        val = obj.get(key)
        if isinstance(val, list) and val:
            removed += len(val)
            obj[key] = []

    messages = obj.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            meta = msg.get("metadata")
            if isinstance(meta, dict):
                att = meta.get("attachments")
                if isinstance(att, list) and att:
                    removed += len(att)
                    meta["attachments"] = []
            content = msg.get("content")
            if isinstance(content, dict):
                parts = content.get("parts")
                if isinstance(parts, list):
                    new_parts: List[Any] = []
                    for part in parts:
                        if isinstance(part, str):
                            new_parts.append(part)
                            continue
                        if isinstance(part, dict):
                            if any(
                                part.get(k)
                                for k in ("file_id", "id", "asset_pointer", "image_url", "mime_type")
                            ):
                                removed += 1
                                continue
                            if isinstance(part.get("text"), str) or isinstance(part.get("content"), str):
                                new_parts.append(part)
                            else:
                                removed += 1
                            continue
                        new_parts.append(part)
                    if len(new_parts) != len(parts):
                        content["parts"] = new_parts
            elif isinstance(content, list):
                new_list: List[Any] = []
                for item in content:
                    if isinstance(item, str):
                        new_list.append(item)
                    elif isinstance(item, dict) and (
                        isinstance(item.get("text"), str) or isinstance(item.get("content"), str)
                    ):
                        new_list.append(item)
                    elif isinstance(item, dict):
                        removed += 1
                    else:
                        new_list.append(item)
                if len(new_list) != len(content):
                    msg["content"] = new_list

    removed += _strip_last_codex_user_attachments(obj)
    return removed

_UPLOAD_INIT_FILENAME_BY_REQUEST: Dict[str, str] = {}
_UPLOAD_INIT_MAP_LIMIT = 2048
# When /backend-api/files response is lost (mirror 304/empty), PUT still has the uuid path.
# Keep recent init filenames so the next oaiusercontent PUT can claim the display name.
_UPLOAD_INIT_PENDING_FILENAMES: List[str] = []
_UPLOAD_INIT_PENDING_LIMIT = 64


def _safe_utf8(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _queue_pending_init_filename(filename: str) -> None:
    name = str(filename or "").strip()
    if not name:
        return
    _UPLOAD_INIT_PENDING_FILENAMES.append(name)
    while len(_UPLOAD_INIT_PENDING_FILENAMES) > _UPLOAD_INIT_PENDING_LIMIT:
        _UPLOAD_INIT_PENDING_FILENAMES.pop(0)


def _claim_pending_init_filename() -> str:
    while _UPLOAD_INIT_PENDING_FILENAMES:
        name = str(_UPLOAD_INIT_PENDING_FILENAMES.pop(0) or "").strip()
        if name:
            return name
    return ""


def _path_without_query(path: str) -> str:
    return str(path or "").split("?", 1)[0]


def _gpt_uid_from_uuid_path(raw_id: str) -> str:
    return "file_" + raw_id.replace("-", "")


def _is_chatgpt_upload_init(host: str, path: str) -> bool:
    if "chatgpt.com" not in (host or ""):
        return False
    clean_path = _path_without_query(path)
    return clean_path in ("/backend-api/files", "/backend-anon/files")


def _is_chatgpt_file_auxiliary_path(host: str, path: str) -> bool:
    """library/process_upload_stream/download 등 — 정책 input/output filter 대상 아님."""
    if "chatgpt.com" not in (host or ""):
        return False
    clean_path = _path_without_query(path)
    if clean_path in ("/backend-api/files", "/backend-anon/files"):
        return False
    return clean_path.startswith("/backend-api/files/") or clean_path.startswith(
        "/backend-anon/files/"
    )


def _json_obj(payload: bytes) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _extract_upload_init_filename(payload: bytes) -> str:
    obj = _json_obj(payload)
    if not obj:
        return ""
    for key in ("file_name", "filename", "name"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_upload_init_declared_size(payload: bytes) -> int:
    obj = _json_obj(payload)
    if not obj:
        return 0
    for key in ("file_size", "filesize", "size", "bytes"):
        value = obj.get(key)
        if value is None:
            continue
        try:
            size = int(value)
        except Exception:
            continue
        if size > 0:
            return size
    return 0


def _maybe_decompress_init_payload(payload: bytes) -> bytes:
    """Best-effort decompress when Envoy tees br/gzip without usable CE."""
    if not payload:
        return payload
    head = payload.lstrip()[:1]
    if head in (b"{", b"[") or payload.lstrip()[:5].lower() == b"data:":
        return payload
    try:
        import brotli

        out = brotli.decompress(payload)
        if out and out.lstrip()[:1] in (b"{", b"["):
            return out
    except Exception:
        pass
    if len(payload) >= 2 and payload[:2] == b"\x1f\x8b":
        try:
            import gzip

            return gzip.decompress(payload)
        except Exception:
            pass
    return payload


def _extract_upload_response_id(payload: bytes) -> str:
    payload = _maybe_decompress_init_payload(payload)
    obj = _json_obj(payload)
    if not obj:
        # Preview helps diagnose br/gzip leftovers or non-JSON init responses.
        preview = _safe_utf8(payload)[:180].replace("\n", " ")
        logger.info(
            "GPT upload init response not JSON (len=%d) preview=%r",
            len(payload or b""),
            preview,
        )
        return ""

    for key in ("file_id", "id", "fileId"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # Nested shapes: {"file":{"id":...}}, {"data":{"file_id":...}}
    for nest_key in ("file", "data", "result", "payload"):
        nested = obj.get(nest_key)
        if not isinstance(nested, dict):
            continue
        for key in ("file_id", "id", "fileId"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        upload_url = nested.get("upload_url") or nested.get("uploadUrl")
        if isinstance(upload_url, str):
            m = re.search(r"/files/([^/?]+)/raw", upload_url)
            if m:
                return _gpt_uid_from_uuid_path(m.group(1))
            m = re.search(r"/(file-[A-Za-z0-9_-]+)", upload_url)
            if m:
                return m.group(1)

    upload_url = obj.get("upload_url") or obj.get("uploadUrl")
    if isinstance(upload_url, str):
        m = re.search(r"/files/([^/?]+)/raw", upload_url)
        if m:
            return _gpt_uid_from_uuid_path(m.group(1))
        m = re.search(r"/(file-[A-Za-z0-9_-]+)", upload_url)
        if m:
            return m.group(1)

    preview = _safe_utf8(payload)[:180].replace("\n", " ")
    logger.info(
        "GPT upload init response JSON missing file id keys=%s preview=%r",
        ",".join(sorted(str(k) for k in obj.keys())[:20]),
        preview,
    )
    return ""


def _remember_upload_init_filename(request_id: str, filename: str) -> None:
    if not request_id or not filename:
        return
    _UPLOAD_INIT_FILENAME_BY_REQUEST[request_id] = filename
    while len(_UPLOAD_INIT_FILENAME_BY_REQUEST) > _UPLOAD_INIT_MAP_LIMIT:
        oldest = next(iter(_UPLOAD_INIT_FILENAME_BY_REQUEST))
        _UPLOAD_INIT_FILENAME_BY_REQUEST.pop(oldest, None)


def _register_aliases_from_upload_url(uid: str, upload_url: str) -> None:
    """Map upload_url path tokens → file_id so regional PUT paths resolve."""
    if not uid or not upload_url:
        return
    try:
        from urllib.parse import urlparse

        parsed = urlparse(str(upload_url))
    except Exception:
        return
    path = str(parsed.path or "")
    tokens: List[str] = []
    for part in path.split("/"):
        part = str(part or "").strip()
        if len(part) >= 8:
            tokens.append(part)
    # Query may carry file name / id fragments on some Azure SAS URLs.
    for m in re.finditer(r"(file_[A-Za-z0-9]+|file-[A-Za-z0-9_-]+)", str(upload_url)):
        tokens.append(m.group(1))
    seen: set[str] = set()
    for tok in tokens:
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        _fum.register_upload_ttl_segment(tok, uid)
        _fum.register_upload_server_alias(tok, uid)
    if tokens:
        logger.info(
            "GPT upload url aliases uid=%s host=%s tokens=%s",
            uid,
            str(parsed.netloc or "")[:80],
            ",".join(tokens[:8]),
        )


def _extract_upload_id_from_path(host: str, path: str) -> str:
    if "oaiusercontent.com" not in (host or ""):
        return ""
    m = re.search(r"/files/([^/?]+)/raw", path)
    if m:
        uid = _gpt_uid_from_uuid_path(m.group(1))
        logger.info("GPT upload (uuid-path): path_id=%s uid=%s", m.group(1), uid)
        return uid
    # Newer ChatGPT ids use file_… (underscore); older file-… (hyphen).
    m = re.search(r"/(file_[A-Za-z0-9]+)", path)
    if m:
        uid = m.group(1)
        logger.info("GPT upload (file_underscore): uid=%s", uid)
        return uid
    m = re.search(r"/(file-[A-Za-z0-9_-]+)", path)
    if m:
        uid = m.group(1)
        logger.info("GPT upload (file-prefix): uid=%s", uid)
        return uid
    # Resolve tokens registered from init upload_url (regional CDN paths).
    for part in str(path or "").split("/"):
        part = part.strip()
        if len(part) < 8:
            continue
        aliased = _fum.lookup_upload_id_by_ttl_segment(part)
        if aliased:
            logger.info("GPT upload (url-alias): token=%s uid=%s", part[:64], aliased)
            return aliased
        aliased = _fum.lookup_upload_id_by_registered_server_uuid(part)
        if aliased:
            logger.info("GPT upload (uuid-alias): token=%s uid=%s", part[:64], aliased)
            return aliased
    return ""


def _strip_attachment_id_extension(path_str: str) -> str:
    """Remove basename extension from saved-path attachment id (e.g. .../file_xxx.pdf → .../file_xxx)."""
    p = str(path_str or "").strip()
    if not p:
        return p
    root, ext = os.path.splitext(p)
    return root if ext else p


def _normalize_attachments(value: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            size_value = item.get("size") or 0
            try:
                size = int(size_value)
            except Exception:
                size = 0
            row = {
                "id": str(item.get("id") or item.get("file_id") or item.get("upload_id") or "").strip(),
                "size": size,
                "name": str(item.get("name") or item.get("file_name") or item.get("filename") or "").strip(),
                "mime_type": str(item.get("mime_type") or item.get("mimeType") or ""),
            }
            # 파일이 없는 턴의 자리표시자({id:"", name:""})는 첨부가 아니다.
            if not row["id"] and not row["name"]:
                continue
            out.append(row)
    return out


def _attachments_from_messages(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    messages = obj.get("messages")
    if not isinstance(messages, list):
        return raw
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        meta = msg.get("metadata")
        if not isinstance(meta, dict):
            continue
        att_list = meta.get("attachments")
        if not isinstance(att_list, list):
            continue
        for item in att_list:
            if not isinstance(item, dict):
                continue
            aid = str(item.get("id") or "")
            if aid:
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
            raw.append(item)
    return raw


def _policy_request_attachments(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    top = obj.get("attachments")
    if isinstance(top, list) and len(top) > 0:
        return _normalize_attachments(top)
    nested = _attachments_from_messages(obj)
    if nested:
        return _normalize_attachments(nested)
    inline = _save_codex_inline_attachments(obj)
    tool = save_codex_tool_output_files(obj) if _last_input_is_tool_output(obj) else []
    merged = list(inline)
    seen_ids = {str(item.get("id") or "") for item in merged}
    for item in tool:
        item_id = str(item.get("id") or "")
        if item_id and item_id in seen_ids:
            continue
        merged.append(item)
        if item_id:
            seen_ids.add(item_id)
    if merged:
        return merged
    return _normalize_attachments([])


def _last_input_is_tool_output(obj: Dict[str, Any]) -> bool:
    items = obj.get("input")
    if not isinstance(items, list) or not items:
        return False
    last = items[-1]
    if not isinstance(last, dict):
        return False
    return str(last.get("type") or "").lower() in _TOOL_OUTPUT_TYPES


def _finalize_policy_attachment(item: Dict[str, Any]) -> Dict[str, Any]:
    upload_id = str(item.get("id") or "").strip()
    req_name = str(item.get("name") or "")
    req_mime = str(item.get("mime_type") or "")
    # Inline saves already resolved id to a local path. Do not look that path up
    # as an upload id, or the pipeline receives an empty attachment.
    if upload_id and os.path.isfile(upload_id):
        try:
            size = int(item.get("size") or 0) or os.path.getsize(upload_id)
        except OSError:
            size = 0
        return {
            "id": upload_id,
            "size": size,
            "name": req_name or os.path.basename(upload_id),
            "mime_type": req_mime,
        }

    saved = _fum.get_saved_attachment(
        upload_id,
        fallback_name=req_name,
        fallback_mime=req_mime,
    )
    if not saved and upload_id:
        # Upload PUT may still be flushing when conversation turn arrives.
        saved = _fum.wait_for_saved_attachment(
            upload_id,
            fallback_name=req_name,
            fallback_mime=req_mime,
        )
    if saved:
        name = req_name
        if not name:
            name = _fum.get_registered_upload_filename(upload_id) or str(saved.get("name") or "")
        return {
            "id": _strip_attachment_id_extension(str(saved.get("id") or "")),
            "size": int(saved.get("size") or 0),
            "name": name,
            "mime_type": req_mime,
        }

    try:
        size = int(item.get("size") or 0)
    except Exception:
        size = 0
    # Never emit a bare upload_id as attachments[].id — file_block_filter needs a
    # local filesystem path. Prefer the canonical tmpfs path so inspect can open
    # it if the file appears mid-request; empty id when upload_id unknown.
    if upload_id:
        expected = _fum.expected_saved_path(upload_id)
        logger.warning(
            "chatgpt policy attachment not on disk yet: uid=%s expected=%s",
            upload_id[:64],
            expected[:120],
        )
        return {
            "id": _strip_attachment_id_extension(expected) if expected else "",
            "size": size,
            "name": req_name or _fum.get_registered_upload_filename(upload_id),
            "mime_type": req_mime,
        }
    return {
        "id": "",
        "size": size,
        "name": req_name,
        "mime_type": req_mime,
    }


def build_policy_attachments(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build final file_block_filter attachments; no engine-side re-resolution."""
    attachments = _policy_request_attachments(obj)
    out = [_finalize_policy_attachment(item) for item in attachments]
    return [
        row
        for row in out
        if str(row.get("id") or "").strip() or str(row.get("name") or "").strip()
    ]


def _handle_oaiusercontent_upload(
    req: Dict[str, Any],
    payload: bytes,
    upload_id: str,
) -> Dict[str, Any]:
    direction = str(req.get("direction") or "").lower()
    unit_type = str(req.get("unit_type") or "").lower()
    

    if direction == "response":
        return _fum.build_upload_result(upload_id=upload_id, handled=True)

    if direction != "request" or unit_type not in ("raw_body", "form_body", "json_body"):
        
        return _fum.build_upload_result(upload_id=upload_id, handled=True)

    policy = _fum.collection_policy_from_request(req)
    upload_filename = _fum.get_registered_upload_filename(upload_id)
    if not upload_filename:
        # Init response often missing on live mirror (status=304/empty). Claim newest pending name.
        pending_name = _claim_pending_init_filename()
        if pending_name:
            if _fum.register_upload_mapping(upload_id, pending_name, policy):
                upload_filename = pending_name
                logger.info(
                    "GPT upload pending-filename claimed: uid=%s name=%s",
                    upload_id,
                    pending_name,
                )
            else:
                _queue_pending_init_filename(pending_name)
    saved_path = ""

    if payload:
        try:
            saved_path = _fum.append_upload_payload(upload_id, payload, policy)
            if not saved_path:
                why = _fum.collection_denied_reason(upload_id) or "collection_policy"
                return _fum.build_skipped_upload_result(
                    upload_id,
                    upload_filename,
                    why,
                )
        except Exception:
            logger.exception("GPT upload save failed: upload_id=%s", upload_id)
            return _fum.build_upload_result(upload_id=upload_id, upload_filename=upload_filename, handled=True)

    effective_name = upload_filename or upload_id[:48]
    return _fum.build_upload_result(
        upload_id=upload_id,
        upload_filename=effective_name,
        saved_path=saved_path,
        handled=True,
    )


def normalize_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    path = str(meta.get("path") or "")
    direction = str(req.get("direction") or "").lower()

    if _is_chatgpt_file_auxiliary_path(host, path):
        return _fum.build_upload_result(handled=True)

    if _is_chatgpt_upload_init(host, path):
        request_id = str(req.get("request_id") or "")
        if direction == "request":
            filename = _extract_upload_init_filename(payload)
            declared_size = _extract_upload_init_declared_size(payload)
            policy = _fum.collection_policy_from_request(req)
            # 확장자/크기 차단은 pipeline file_block_filter가 담당 (engine 선차단 비활성).
            # Gate at ChatGate-held init: deny unknown ext / oversize before upstream.
            # if filename:
            #     ok, reason = _fum.check_file_collection_policy(
            #         filename,
            #         declared_size,
            #         policy,
            #         current_size=0,
            #         allow_unknown_extension=False,
            #     )
            #     if not ok:
            #         logger.info(
            #             "GPT upload init denied name=%s size=%d reason=%s",
            #             filename,
            #             declared_size,
            #             reason,
            #         )
            #         return _fum.build_skipped_upload_result("", filename, reason)
            _ = (declared_size, policy)  # kept for future engine-side gate
            _remember_upload_init_filename(request_id, filename)
            _queue_pending_init_filename(filename)
            return _fum.build_upload_result(upload_filename=filename, handled=True)

        if direction == "response":
            uid = _extract_upload_response_id(payload)
            filename = _UPLOAD_INIT_FILENAME_BY_REQUEST.pop(request_id, "") if request_id else ""
            if uid and filename:
                policy = _fum.collection_policy_from_request(req)
                if not _fum.register_upload_mapping(uid, filename, policy):
                    why = _fum.collection_denied_reason(uid) or "collection_policy"
                    return _fum.build_skipped_upload_result(uid, filename, why)
                # Log upload_url host so we can see where the binary PUT lands.
                try:
                    obj = _json_obj(_maybe_decompress_init_payload(payload)) or {}
                    upload_url = str(obj.get("upload_url") or obj.get("uploadUrl") or "")
                    from urllib.parse import urlparse

                    host_u = urlparse(upload_url).netloc if upload_url else ""
                    path_u = urlparse(upload_url).path if upload_url else ""
                    logger.info(
                        "GPT upload init mapped uid=%s name=%s upload_host=%s upload_path=%s keys=%s",
                        uid,
                        filename,
                        host_u or "-",
                        (path_u or "-")[:120],
                        ",".join(sorted(str(k) for k in obj.keys())[:20]),
                    )
                    _register_aliases_from_upload_url(uid, upload_url)
                except Exception:
                    logger.exception("GPT upload init alias register failed uid=%s", uid)
                # Response arrived with id — drop one matching pending slot if present.
                try:
                    _UPLOAD_INIT_PENDING_FILENAMES.remove(filename)
                except ValueError:
                    pass
            elif filename and not uid:
                # Keep pending so later PUT can claim; already queued on request.
                logger.info(
                    "GPT upload init response missing file id (mirror empty/304); pending name=%s",
                    filename,
                )
            return _fum.build_upload_result(upload_id=uid, upload_filename=filename, handled=True)

        return _fum.build_upload_result(handled=True)

    if "oaiusercontent.com" not in host:
        return None

    upload_id = _extract_upload_id_from_path(host, path)
    if not upload_id:
        
        return None

    return _handle_oaiusercontent_upload(req, payload, upload_id)


# Public entry points share file_upload_manager.UPLOAD_STATE_LOCK so the
# policy loop can run normalize/persist on a worker without racing extract.
_fum.bind_upload_state_lock(globals())
