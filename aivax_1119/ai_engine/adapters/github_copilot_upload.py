"""
GitHub Copilot file upload and policy attachment assembly.

Flow (Business plan, OpenAI Responses API):
  Init:  POST api.business.githubcopilot.com/files  (JSON file_name → file_id / upload_url)
  Binary: PUT *.oaiusercontent.com/files/{uuid}/raw or /file-{id}
  Chat:  POST api.business.githubcopilot.com/responses  (input[] with input_image / input_file)
  Skip:  telemetry, /_ping, api.github.com /copilot_internal/*

Adapter owns upload + attachment resolution; file_upload_manager is storage only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

import file_upload_manager as _fum

logger = logging.getLogger(__name__)

POLICY_ATTACHMENTS_FINAL = True

_STUB_ATTACHMENT = [{"id": "", "size": 0, "name": "", "mime_type": ""}]
_UPLOAD_INIT_FILENAME_BY_REQUEST: Dict[str, str] = {}
_UPLOAD_INIT_MAP_LIMIT = 512
_DATA_URL_RE = re.compile(r"^data:([^;,]+)?(?:;[^,]*)?,(.+)$", re.DOTALL | re.IGNORECASE)
_DATA_IMAGE_IN_TEXT_RE = re.compile(
    r"data:(image/[^;\s\"'\\<>]+)(?:;[^,\s\"'\\<>]*)?,([A-Za-z0-9+/=]+)",
    re.IGNORECASE,
)
_FILE_ID_TOKEN_RE = re.compile(r"file-[A-Za-z0-9_-]{8,}|file_[A-Za-z0-9]{8,}")
_AT_FILE_REF_RE = re.compile(
    r"@file:\s*([^\n<]+?\.(?:png|jpe?g|gif|webp|bmp|heic|heif|pdf|tiff?))",
    re.IGNORECASE,
)
_IMAGE_FILE_EXTENSIONS = frozenset(
    {".jpeg", ".jpg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".pdf", ".tif", ".tiff"}
)
_PLACEHOLDER_UPLOAD_PREFIX = "copilot_atfile_"
_COPILOT_ATTACH_PREFIX = "copilot_attach_"
_COPILOT_ATTACH_PENDING_PREFIX = "copilot_attach_pending_"
# Only these ids may steal oaiusercontent from the default gpt adapter.
# ChatGPT also registers file_* ids via /backend-api/files — must not route those here.
_COPILOT_OWNED_UPLOAD_IDS: set[str] = set()
_COPILOT_OWNED_UPLOAD_LIMIT = 4096
_MIN_REAL_IMAGE_BYTES = 128
_ATTACHMENTS_BLOCK_RE = re.compile(r"<attachments>\s*(.*?)\s*</attachments>", re.DOTALL | re.IGNORECASE)
_GH_ATTACHMENT_URL_RE = re.compile(
    r"github(?:usercontent)?\.com/(?:github-copilot/)?chat/attachments/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_ATTACHMENT_ID_ANYWHERE_RE = re.compile(
    r"/(?:chat/)?attachments/([A-Za-z0-9_-]{8,64})",
    re.IGNORECASE,
)
_IMAGE_NAME_IN_TEXT_RE = re.compile(
    r"([\w\s\-().]+\.(?:png|jpe?g|gif|webp|bmp|heic|heif|tiff?))",
    re.IGNORECASE,
)



def _path_raw(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("path") or "").split("#", 1)[0] or "/"


def _path_base(req: Dict[str, Any]) -> str:
    return _path_raw(req).split("?", 1)[0].rstrip("/") or "/"


def _host(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("host") or "").lower()


def _safe_utf8(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _json_obj(payload: bytes) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _gpt_uid_from_uuid_path(raw_id: str) -> str:
    return "file_" + raw_id.replace("-", "")


def _mime_from_name(name: str) -> str:
    mt, _ = mimetypes.guess_type(str(name or ""))
    return mt or ""


def is_github_copilot_skip_upload_path(req: Dict[str, Any]) -> bool:
    """Paths that must not enter file upload or chat policy pipelines."""
    host = _host(req)
    path = _path_base(req)
    if "telemetry." in host and path == "/telemetry":
        return True
    if path in ("/_ping",):
        return True
    if "api.github.com" in host:
        path = str(_path_base(req) or "").lower()
        if "/copilot_internal/" in path:
            return True
        return False
    return False


def _is_copilot_chat_attachment_upload(host: str, path: str) -> bool:
    if "uploads.github.com" not in (host or ""):
        return False
    base = str(path or "").split("?", 1)[0].rstrip("/")
    return base.endswith("/copilot/chat/attachments")


def _is_github_copilot_attachment_download(host: str, path: str) -> bool:
    if "github.com" not in (host or ""):
        return False
    return "github-copilot/chat/attachments" in str(path or "")


def _parse_attachment_upload_query(path: str) -> Tuple[str, str]:
    parts = str(path or "").split("?", 1)
    if len(parts) < 2:
        return "", ""
    params = parse_qs(parts[1], keep_blank_values=False)
    name = str((params.get("name") or [""])[0] or "").strip()
    mime = str(
        (params.get("content_type") or params.get("content-type") or [""])[0] or ""
    ).strip()
    return name, mime


def _pending_attachment_upload_id(request_id: str) -> str:
    rid = str(request_id or "").strip() or uuid_hint()
    return f"{_COPILOT_ATTACH_PENDING_PREFIX}{hashlib.sha256(rid.encode()).hexdigest()[:20]}"


def uuid_hint() -> str:
    return hashlib.sha256(str(time.time()).encode()).hexdigest()[:12]


def _copilot_attachment_upload_id(server_uuid: str) -> str:
    return f"{_COPILOT_ATTACH_PREFIX}{str(server_uuid or '').strip()}"


def _extract_chat_attachment_uuid_from_url(url: str) -> str:
    match = _GH_ATTACHMENT_URL_RE.search(str(url or ""))
    return str(match.group(1) or "").strip() if match else ""


def _extract_chat_attachment_response_uuid(payload: bytes) -> str:
    obj = _json_obj(payload)
    if not isinstance(obj, dict):
        return ""
    return _extract_chat_attachment_uuid_from_url(str(obj.get("url") or ""))


def _extract_attachment_uuid_from_path(path: str) -> str:
    match = re.search(r"github-copilot/chat/attachments/([A-Za-z0-9_-]+)", str(path or ""), re.I)
    return str(match.group(1) or "").strip() if match else ""


def _has_attachments_marker(obj: Dict[str, Any]) -> bool:
    snippet = _last_user_message_blob(obj)
    return bool(re.search(r"<\s*attachments\b", snippet, re.IGNORECASE))


def _attachments_block_blob(obj: Dict[str, Any]) -> str:
    snippet = _last_user_message_blob(obj)
    if not snippet:
        return ""
    match = _ATTACHMENTS_BLOCK_RE.search(snippet)
    if match:
        return str(match.group(1) or "").strip()
    open_match = re.search(
        r"<\s*attachments\b[^>]*>(.*?)(?=<\s*/?\s*(?:userRequest|context|attachments)\b|$)",
        snippet,
        re.DOTALL | re.IGNORECASE,
    )
    return str(open_match.group(1) or "").strip() if open_match else ""


def _collect_view_image_paths(obj: Dict[str, Any]) -> List[str]:
    inp = obj.get("input")
    if not isinstance(inp, list):
        return []
    seen: set[str] = set()
    out: List[str] = []
    for item in inp:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").lower() != "function_call":
            continue
        if str(item.get("name") or "").strip().lower() != "view_image":
            continue
        args_raw = item.get("arguments")
        args: Dict[str, Any] = {}
        if isinstance(args_raw, dict):
            args = args_raw
        elif isinstance(args_raw, str) and args_raw.strip():
            try:
                parsed = json.loads(args_raw)
                if isinstance(parsed, dict):
                    args = parsed
            except Exception:
                pass
        file_path = str(args.get("filePath") or args.get("filepath") or "").strip()
        if not file_path:
            continue
        key = file_path.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(file_path)
    return out


def _collect_image_paths_from_tool_outputs(obj: Dict[str, Any]) -> List[str]:
    inp = obj.get("input")
    if not isinstance(inp, list):
        return []
    seen: set[str] = set()
    out: List[str] = []
    for item in inp:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").lower() not in ("function_call_output", "custom_tool_call_output"):
            continue
        output = item.get("output")
        text = output if isinstance(output, str) else ""
        if not text:
            continue
        for match in _IMAGE_NAME_IN_TEXT_RE.finditer(text):
            path = str(match.group(1) or "").strip()
            if not path:
                continue
            name = os.path.basename(path)
            if not is_image_like_filename(name):
                continue
            key = path.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
    return out


def _wire_attachment_stub(name: str, upload_id: str = "") -> Dict[str, Any]:
    clean_name = os.path.basename(str(name or "").strip()) or "copilot_image_attachment.png"
    return {
        "id": upload_id,
        "size": 0,
        "name": clean_name,
        "mime_type": _mime_from_name(clean_name),
    }


def _walk_json_for_urls(val: Any, out: List[str]) -> None:
    if isinstance(val, dict):
        for key, item in val.items():
            key_l = str(key or "").lower()
            if key_l in ("url", "imageurl", "image_url", "uri", "href", "src") and isinstance(item, str):
                text = item.strip()
                if text:
                    out.append(text)
            else:
                _walk_json_for_urls(item, out)
    elif isinstance(val, list):
        for item in val:
            _walk_json_for_urls(item, out)


def _parse_json_candidates(blob: str) -> List[Any]:
    text = str(blob or "").strip()
    if not text:
        return []
    out: List[Any] = []
    for candidate in (text, text.strip("`")):
        try:
            parsed = json.loads(candidate)
            out.append(parsed)
        except Exception:
            pass
    for match in re.finditer(r"\{[^{}]*(?:imageUrl|image_url|url)[^{}]*\}", text, re.I):
        try:
            out.append(json.loads(match.group(0)))
        except Exception:
            pass
    return out


def _extract_attachment_id_from_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    for pattern in (_GH_ATTACHMENT_URL_RE, _ATTACHMENT_ID_ANYWHERE_RE):
        match = pattern.search(text)
        if match:
            return str(match.group(1) or "").strip()
    return ""


def _collect_image_names_from_blob(blob: str) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for match in _IMAGE_NAME_IN_TEXT_RE.finditer(str(blob or "")):
        name = os.path.basename(str(match.group(1) or "").strip())
        if not name or not is_image_like_filename(name):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _collect_wire_attachment_uuids(obj: Dict[str, Any]) -> List[str]:
    """Collect GitHub Copilot chat attachment UUIDs from the latest user turn."""
    snippet = _last_user_message_blob(obj)
    block = _attachments_block_blob(obj)
    if not snippet and not block:
        return []
    seen: set[str] = set()
    out: List[str] = []

    def _add_uid(uid: str) -> None:
        token = str(uid or "").strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)

    for blob in (block, snippet):
        if not blob:
            continue
        for match in _GH_ATTACHMENT_URL_RE.finditer(blob):
            _add_uid(str(match.group(1) or ""))
        for match in _ATTACHMENT_ID_ANYWHERE_RE.finditer(blob):
            _add_uid(str(match.group(1) or ""))
        for parsed in _parse_json_candidates(blob):
            urls: List[str] = []
            _walk_json_for_urls(parsed, urls)
            for url in urls:
                _add_uid(_extract_attachment_id_from_url(url))
    return out


def _attachments_from_copilot_wire_context(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Resolve Copilot drag-drop / wire attachment signals (marker, UUID, view_image)."""
    marker = _has_attachments_marker(obj)
    block = _attachments_block_blob(obj)
    if not marker and not block and not _collect_wire_attachment_uuids(obj):
        view_paths = _collect_view_image_paths(obj)
        tool_paths = _collect_image_paths_from_tool_outputs(obj)
        if not view_paths and not tool_paths:
            return []

    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _append(entry: Optional[Dict[str, Any]]) -> None:
        if not entry:
            return
        key = str(entry.get("id") or entry.get("name") or "").lower()
        if not key or key in seen:
            return
        seen.add(key)
        out.append(entry)

    for uid in _collect_wire_attachment_uuids(obj):
        saved = _resolve_copilot_attachment_uuid(uid)
        if saved:
            _append(saved)
            continue
        _append(_wire_attachment_stub(uid, _copilot_attachment_upload_id(uid)))

    for name in _collect_image_names_from_blob(block):
        _append(_wire_attachment_stub(name))

    for file_path in _collect_view_image_paths(obj):
        _append(_wire_attachment_stub(file_path))

    for file_path in _collect_image_paths_from_tool_outputs(obj):
        _append(_wire_attachment_stub(file_path))

    if marker and not out:
        _append(_wire_attachment_stub("copilot_image_attachment.png"))
    return out


def _resolve_copilot_attachment_uuid(uuid: str) -> Optional[Dict[str, Any]]:
    uid = str(uuid or "").strip()
    if not uid:
        return None
    for upload_id in (
        _copilot_attachment_upload_id(uid),
        uid,
        *_fum.lookup_upload_ids_by_registered_filename(uid),
    ):
        if is_placeholder_upload_id(upload_id):
            continue
        saved = _resolve_file_ref(upload_id)
        if is_real_saved_attachment(saved):
            return saved
    alias = _fum.lookup_upload_id_by_registered_server_uuid(uid)
    if alias and not is_placeholder_upload_id(alias):
        saved = _resolve_file_ref(alias)
        if is_real_saved_attachment(saved):
            return saved
    return None


def _attachments_from_wire_uuids(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _attachments_from_copilot_wire_context(obj)


def _is_copilot_upload_init(host: str, path: str) -> bool:
    if "githubcopilot.com" not in (host or ""):
        return False
    clean = str(path or "").split("?", 1)[0].rstrip("/")
    return clean in ("/files", "/v1/files")


def _is_copilot_file_auxiliary_path(host: str, path: str) -> bool:
    if "githubcopilot.com" not in (host or ""):
        return False
    clean = str(path or "").split("?", 1)[0]
    if clean.rstrip("/") in ("/files", "/v1/files"):
        return False
    return "/files/" in clean


def _extract_upload_init_filename(payload: bytes) -> str:
    obj = _json_obj(payload)
    if not obj:
        return ""
    for key in ("file_name", "filename", "name"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_upload_response_id(payload: bytes) -> str:
    obj = _json_obj(payload)
    if not obj:
        return ""

    for key in ("file_id", "id"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    upload_url = obj.get("upload_url")
    if isinstance(upload_url, str):
        m = re.search(r"/files/([^/?]+)/raw", upload_url)
        if m:
            return _gpt_uid_from_uuid_path(m.group(1))
    return ""


def _remember_upload_init_filename(request_id: str, filename: str) -> None:
    if not request_id or not filename:
        return
    _UPLOAD_INIT_FILENAME_BY_REQUEST[request_id] = filename
    while len(_UPLOAD_INIT_FILENAME_BY_REQUEST) > _UPLOAD_INIT_MAP_LIMIT:
        oldest = next(iter(_UPLOAD_INIT_FILENAME_BY_REQUEST))
        _UPLOAD_INIT_FILENAME_BY_REQUEST.pop(oldest, None)


def _extract_upload_id_from_path(host: str, path: str) -> str:
    if "oaiusercontent.com" not in (host or ""):
        return ""
    m = re.search(r"/files/([^/?]+)/raw", path)
    if m:
        uid = _gpt_uid_from_uuid_path(m.group(1))
        logger.info("copilot upload (uuid-path): path_id=%s uid=%s", m.group(1), uid)
        return uid
    m = re.search(r"/(file-[A-Za-z0-9_-]+)", path)
    if m:
        uid = m.group(1)
        logger.info("copilot upload (file-prefix): uid=%s", uid)
        return uid
    return ""


def _resolve_file_ref(ref: str) -> Optional[Dict[str, Any]]:
    """Map Copilot/OpenAI file_id variants to saved attachment metadata."""
    rid = str(ref or "").strip()
    if not rid:
        return None

    saved = _fum.get_saved_attachment(rid)
    if saved:
        name = _fum.get_registered_upload_filename(rid) or str(saved.get("name") or "")
        mime = str(saved.get("mime_type") or "") or _mime_from_name(name)
        return {
            "id": str(saved.get("id") or rid),
            "size": int(saved.get("size") or 0),
            "name": name or os.path.basename(str(saved.get("id") or rid)),
            "mime_type": mime,
        }

    alias_id = _fum.lookup_upload_id_by_registered_server_uuid(rid)
    if alias_id:
        saved = _fum.get_saved_attachment(alias_id)
        if saved:
            name = _fum.get_registered_upload_filename(alias_id) or str(saved.get("name") or "")
            mime = str(saved.get("mime_type") or "") or _mime_from_name(name)
            return {
                "id": str(saved.get("id") or alias_id),
                "size": int(saved.get("size") or 0),
                "name": name,
                "mime_type": mime,
            }

    if rid.startswith("file-") and not rid.startswith("file_"):
        hex_id = "file_" + rid[5:].replace("-", "")
        saved = _fum.get_saved_attachment(hex_id)
        if saved:
            name = _fum.get_registered_upload_filename(hex_id) or str(saved.get("name") or "")
            mime = str(saved.get("mime_type") or "") or _mime_from_name(name)
            return {
                "id": str(saved.get("id") or hex_id),
                "size": int(saved.get("size") or 0),
                "name": name,
                "mime_type": mime,
            }

    return None


def _ext_from_mime(mime: str) -> str:
    mt = str(mime or "").split(";", 1)[0].strip().lower()
    ext = mimetypes.guess_extension(mt) or ""
    if ext == ".jpe":
        return ".jpg"
    return ext


def _decode_data_url(url: str) -> Tuple[str, bytes]:
    m = _DATA_URL_RE.match(str(url or "").strip())
    if not m:
        return "", b""
    mime = str(m.group(1) or "application/octet-stream").strip()
    try:
        return mime, base64.b64decode(m.group(2).strip())
    except Exception:
        return mime, b""


def _looks_like_image_bytes(raw: bytes) -> bool:
    if not raw:
        return False
    if raw[:4] == b"\x89PNG":
        return True
    if raw[:2] == b"\xff\xd8":
        return True
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if raw[:4] == b"RIFF" and len(raw) > 12 and raw[8:12] == b"WEBP":
        return True
    return False


def _inline_upload_id(data: bytes) -> str:
    return "copilot_inline_" + hashlib.sha256(data).hexdigest()[:16]


def _inline_image_from_part(part: Dict[str, Any]) -> Optional[Tuple[str, bytes, str]]:
    req_name = str(part.get("filename") or part.get("file_name") or part.get("name") or "")
    req_mime = str(part.get("mime_type") or part.get("mimeType") or "")

    image_val = part.get("image")
    if isinstance(image_val, str) and image_val.strip():
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", image_val.strip()))
        except Exception:
            raw = b""
        if raw and (_looks_like_image_bytes(raw) or req_mime.startswith("image/")):
            mime = req_mime or "image/png"
            name = req_name or f"copilot_image{_ext_from_mime(mime)}"
            return name, raw, mime

    source = part.get("source")
    if isinstance(source, dict) and str(source.get("type") or "").lower() == "base64":
        data = source.get("data")
        mime = str(source.get("media_type") or source.get("mime_type") or req_mime or "image/png")
        if isinstance(data, str) and data.strip():
            try:
                raw = base64.b64decode(data.strip())
            except Exception:
                raw = b""
            if raw:
                name = req_name or f"copilot_image{_ext_from_mime(mime)}"
                return name, raw, mime

    image_url = part.get("image_url")
    url_str = ""
    if isinstance(image_url, str):
        url_str = image_url.strip()
    elif isinstance(image_url, dict):
        url_str = str(image_url.get("url") or "").strip()

    if url_str.startswith("data:"):
        mime, raw = _decode_data_url(url_str)
        if raw:
            name = req_name or f"copilot_image{_ext_from_mime(mime or req_mime)}"
            return name, raw, mime or req_mime or _mime_from_name(name)

    file_data = part.get("file_data")
    if isinstance(file_data, str) and file_data.strip().startswith("data:"):
        mime, raw = _decode_data_url(file_data.strip())
        if raw and (str(mime or req_mime).startswith("image/") or _looks_like_image_bytes(raw)):
            name = req_name or f"copilot_image{_ext_from_mime(mime or req_mime)}"
            return name, raw, mime or req_mime or _mime_from_name(name)

    for key in ("data", "b64_json", "base64", "content"):
        val = part.get(key)
        if not isinstance(val, str) or len(val) < 64:
            continue
        try:
            raw = base64.b64decode(val.strip())
        except Exception:
            continue
        if _looks_like_image_bytes(raw):
            mime = req_mime or _mime_from_name(req_name) or "image/png"
            name = req_name or f"copilot_image{_ext_from_mime(mime)}"
            return name, raw, mime
    return None


def _scan_input_item_types(obj: Dict[str, Any]) -> List[str]:
    types: List[str] = []
    inp = obj.get("input")
    if not isinstance(inp, list):
        return types
    for item in inp:
        if isinstance(item, dict):
            itype = str(item.get("type") or "").strip()
            if itype:
                types.append(itype)
            else:
                types.append(f"role:{str(item.get('role') or '').strip() or 'unknown'}")
    return types


def _iter_input_parts(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten Responses API input[] including top-level function_call_output items."""
    parts: List[Dict[str, Any]] = []
    inp = obj.get("input")
    if not isinstance(inp, list):
        return parts
    for item in inp:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").lower()
        if item_type in ("function_call_output", "custom_tool_call_output"):
            output = item.get("output")
            if isinstance(output, list):
                parts.extend(x for x in output if isinstance(x, dict))
            elif isinstance(output, dict):
                parts.append(output)
            elif isinstance(output, str) and output.strip():
                parts.append({"type": "input_text", "text": output})
        content = item.get("content")
        if isinstance(content, list):
            parts.extend(x for x in content if isinstance(x, dict))
        elif isinstance(content, str) and content.strip():
            parts.append({"type": "input_text", "text": content})
    return parts


def _iter_message_parts(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten chat/completions messages[] multimodal content."""
    parts: List[Dict[str, Any]] = []
    messages = obj.get("messages")
    if not isinstance(messages, list):
        return parts
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            parts.append({"type": "text", "text": content})
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict):
                parts.append(part)
            elif isinstance(part, str) and part.strip():
                parts.append({"type": "text", "text": part})
    return parts


def _attachments_from_message_parts(obj: Dict[str, Any], req: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    policy = _fum.collection_policy_from_request(req)

    def _append(entry: Optional[Dict[str, Any]]) -> None:
        if not entry:
            return
        key = str(entry.get("id") or entry.get("name") or "")
        if key and key in seen:
            return
        if key:
            seen.add(key)
        out.append(entry)

    for part in _iter_message_parts(obj):
        ptype = str(part.get("type") or "").lower()
        if ptype in ("input_file", "input_image", "file", "image"):
            parsed = _inline_image_from_part(part)
            if parsed:
                entry = _persist_image_tuple(*parsed, policy)
                _append(entry)
            continue
        if ptype == "image_url":
            url_obj = part.get("image_url")
            url = url_obj.get("url") if isinstance(url_obj, dict) else url_obj
            if isinstance(url, str) and url.startswith("data:"):
                mime, raw = _decode_data_url(url)
                if raw:
                    name = f"copilot_image{_ext_from_mime(mime or 'image/png')}"
                    entry = _persist_image_tuple(name, raw, mime or "image/png", policy)
                    _append(entry)
            elif isinstance(url, str) and url.strip():
                uid = _extract_chat_attachment_uuid_from_url(url)
                if uid:
                    _append(_resolve_copilot_attachment_uuid(uid))
        text = part.get("text")
        if isinstance(text, str):
            for name, raw, mime in _images_from_text_blob(text):
                entry = _persist_image_tuple(name, raw, mime, policy)
                _append(entry)
    return out


def _scan_content_part_types(obj: Dict[str, Any]) -> List[str]:
    types: List[str] = []
    inp = obj.get("input")
    if not isinstance(inp, list):
        return types
    for part in _iter_input_parts(obj):
        if isinstance(part, dict):
            types.append(str(part.get("type") or ""))
    return types


def _blob_attachment_tuple(item: Dict[str, Any]) -> Optional[Tuple[str, bytes, str]]:
    typ = str(item.get("type") or "").lower()
    if typ not in ("blob", "image"):
        return None
    data = item.get("data")
    if not isinstance(data, str) or not data.strip():
        return None
    mime = str(item.get("mimeType") or item.get("mime_type") or "image/png")
    name = str(
        item.get("displayName")
        or item.get("display_name")
        or item.get("filename")
        or item.get("name")
        or f"copilot_image{_ext_from_mime(mime)}"
    )
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", data.strip()))
    except Exception:
        return None
    if not raw:
        return None
    if mime.startswith("image/") or _looks_like_image_bytes(raw):
        return name, raw, mime
    return None


def _images_from_raw_b64_in_text(text: str) -> List[Tuple[str, bytes, str]]:
    """Fallback when Copilot embeds raw base64 without a data: URL prefix."""
    found: List[Tuple[str, bytes, str]] = []
    blob = text or ""
    markers = (
        ("iVBORw0KGgo", "image/png", ".png"),
        ("/9j/", "image/jpeg", ".jpg"),
        ("R0lGOD", "image/gif", ".gif"),
        ("UklGR", "image/webp", ".webp"),
    )
    for marker, mime, ext in markers:
        start = 0
        while True:
            idx = blob.find(marker, start)
            if idx < 0:
                break
            end = idx
            while end < len(blob):
                ch = blob[end]
                if ch.isalnum() or ch in "+/=":
                    end += 1
                    continue
                break
            chunk = blob[idx:end]
            if len(chunk) >= 32:
                try:
                    raw = base64.b64decode(chunk + "=" * ((4 - len(chunk) % 4) % 4))
                except Exception:
                    raw = b""
                if raw and _looks_like_image_bytes(raw):
                    found.append((f"copilot_image{ext}", raw, mime))
                    break
            start = idx + len(marker)
    return found


def _images_from_raw_binary_text(text: str) -> List[Tuple[str, bytes, str]]:
    """Extract images embedded as raw bytes inside JSON string values (unicode escapes)."""
    if not text or len(text) < 16:
        return []
    raw = text.encode("latin-1", errors="ignore")
    found: List[Tuple[str, bytes, str]] = []

    png_sig = b"\x89PNG\r\n\x1a\n"
    idx = raw.find(png_sig)
    if idx >= 0:
        iend = raw.find(b"IEND\xaeB`\x82", idx)
        blob = raw[idx : iend + 8] if iend >= 0 else raw[idx : idx + 20_000_000]
        if _looks_like_image_bytes(blob):
            found.append(("copilot_image.png", blob, "image/png"))

    jpg_sig = b"\xff\xd8\xff"
    idx = raw.find(jpg_sig)
    if idx >= 0:
        end = raw.find(b"\xff\xd9", idx + 3)
        blob = raw[idx : end + 2] if end >= 0 else raw[idx : idx + 20_000_000]
        if len(blob) >= 8:
            found.append(("copilot_image.jpg", blob, "image/jpeg"))

    gif_sig = (b"GIF87a", b"GIF89a")
    for sig in gif_sig:
        idx = raw.find(sig)
        if idx >= 0:
            blob = raw[idx : idx + 20_000_000]
            if _looks_like_image_bytes(blob):
                found.append(("copilot_image.gif", blob, "image/gif"))
                break

    return found


def _images_from_text_blob(text: str) -> List[Tuple[str, bytes, str]]:
    found: List[Tuple[str, bytes, str]] = []
    for m in _DATA_IMAGE_IN_TEXT_RE.finditer(text or ""):
        mime = str(m.group(1) or "image/png")
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)))
        except Exception:
            continue
        if len(raw) < 8:
            continue
        if _looks_like_image_bytes(raw) or mime.startswith("image/"):
            found.append((f"copilot_image{_ext_from_mime(mime)}", raw, mime))
    found.extend(_images_from_raw_b64_in_text(text))
    found.extend(_images_from_raw_binary_text(text))
    return found


def _walk_strings(obj: Any, *, min_len: int = 0) -> List[str]:
    out: List[str] = []
    if isinstance(obj, str):
        if len(obj) >= min_len:
            out.append(obj)
    elif isinstance(obj, dict):
        for val in obj.values():
            out.extend(_walk_strings(val, min_len=min_len))
    elif isinstance(obj, list):
        for val in obj:
            out.extend(_walk_strings(val, min_len=min_len))
    return out


def _last_user_text_lengths(obj: Dict[str, Any]) -> List[int]:
    lengths: List[int] = []
    inp = obj.get("input")
    if not isinstance(inp, list):
        return lengths
    for msg in reversed(inp):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").lower() != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            return lengths
        for part in content:
            if isinstance(part, dict) and str(part.get("type") or "") == "input_text":
                text = part.get("text")
                if isinstance(text, str):
                    lengths.append(len(text))
        return lengths
    return lengths


def _collect_file_id_tokens(obj: Dict[str, Any], payload: bytes = b"") -> List[str]:
    blob = payload.decode("utf-8", errors="ignore") if payload else json.dumps(obj, ensure_ascii=False)
    tokens = _FILE_ID_TOKEN_RE.findall(blob)
    embedded = _fum.lookup_upload_ids_embedded_in_text(blob)
    seen: set[str] = set()
    out: List[str] = []
    for tok in tokens + embedded:
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _last_user_part_keys(obj: Dict[str, Any]) -> List[List[str]]:
    inp = obj.get("input")
    if not isinstance(inp, list):
        return []
    for msg in reversed(inp):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").lower() != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            return []
        return [sorted(str(k) for k in part.keys()) for part in content if isinstance(part, dict)]
    return []


def is_image_like_filename(name: str) -> bool:
    ext = os.path.splitext(str(name or "").strip())[1].lower()
    return ext in _IMAGE_FILE_EXTENSIONS


def is_placeholder_upload_id(upload_id: str) -> bool:
    return str(upload_id or "").startswith(_PLACEHOLDER_UPLOAD_PREFIX)


def is_real_saved_attachment(saved: Optional[Dict[str, Any]]) -> bool:
    if not saved:
        return False
    upload_id = str(saved.get("id") or "")
    if is_placeholder_upload_id(upload_id):
        return False
    if is_placeholder_upload_id(os.path.basename(upload_id)):
        return False
    return int(saved.get("size") or 0) >= _MIN_REAL_IMAGE_BYTES


def _mark_copilot_owned_upload(upload_id: str) -> None:
    uid = str(upload_id or "").strip()
    if not uid or is_placeholder_upload_id(uid):
        return
    _COPILOT_OWNED_UPLOAD_IDS.add(uid)
    while len(_COPILOT_OWNED_UPLOAD_IDS) > _COPILOT_OWNED_UPLOAD_LIMIT:
        _COPILOT_OWNED_UPLOAD_IDS.pop()


def resolve_oai_upload_service_id(host: str, path: str) -> Optional[str]:
    """Route oaiusercontent PUT to Copilot only when Copilot init claimed this upload_id."""
    upload_id = _extract_upload_id_from_path(host, path)
    if not upload_id or is_placeholder_upload_id(upload_id):
        return None
    owned = upload_id in _COPILOT_OWNED_UPLOAD_IDS
    
    if owned:
        return "github_copilot"
    return None


def _last_user_message_blob(obj: Dict[str, Any], limit: int = 50000) -> str:
    """Full text of the latest user turn (includes <attachments> outside <userRequest>)."""
    inp = obj.get("input")
    if not isinstance(inp, list):
        return ""
    for msg in reversed(inp):
        if not isinstance(msg, dict) or str(msg.get("role") or "").lower() != "user":
            continue
        content = msg.get("content")
        parts: List[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif part.get("type") in ("input_image", "input_file", "image", "file"):
                    for key in ("filename", "file_name", "name"):
                        val = part.get(key)
                        if isinstance(val, str) and val.strip():
                            parts.append(f"@file:{val.strip()}")
        return "\n".join(parts)[:limit]
    return ""


def user_text_has_image_attachment(text: str) -> bool:
    blob = str(text or "")
    if not blob:
        return False
    if re.search(r"<\s*attachments\b", blob, re.IGNORECASE):
        return True
    if _GH_ATTACHMENT_URL_RE.search(blob):
        return True
    if "data:image" in blob or "iVBORw0KGgo" in blob or "\x89PNG" in blob:
        return True
    for match in _AT_FILE_REF_RE.finditer(blob):
        if is_image_like_filename(str(match.group(1) or "")):
            return True
    return False


def _last_user_request_blob(obj: Dict[str, Any], limit: int = 20000) -> str:
    inp = obj.get("input")
    if not isinstance(inp, list):
        return ""
    for msg in reversed(inp):
        if not isinstance(msg, dict) or str(msg.get("role") or "").lower() != "user":
            continue
        content = msg.get("content")
        parts: List[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif part.get("type") in ("input_image", "input_file", "image", "file"):
                    for key in ("filename", "file_name", "name"):
                        val = part.get(key)
                        if isinstance(val, str) and val.strip():
                            parts.append(f"@file:{val.strip()}")
        blob = "\n".join(parts)
        m = re.search(r"<userRequest>\s*(.*?)\s*</userRequest>", blob, re.DOTALL | re.IGNORECASE)
        snippet = (m.group(1) if m else blob).strip()
        return snippet[:limit]
    return ""


def _collect_at_file_refs(obj: Dict[str, Any]) -> List[str]:
    """Collect @file: refs from the latest user turn only (not full chat history)."""
    snippet = _last_user_request_blob(obj)
    if not snippet:
        return []
    seen: set[str] = set()
    out: List[str] = []
    for match in _AT_FILE_REF_RE.finditer(snippet):
        fname = str(match.group(1) or "").strip().strip("`")
        if fname and fname not in seen:
            seen.add(fname)
            out.append(fname)
    return out


def _resolve_at_file_by_filename(filename: str) -> Optional[Dict[str, Any]]:
    for upload_id in _fum.lookup_upload_ids_by_registered_filename(filename):
        if is_placeholder_upload_id(upload_id):
            continue
        saved = _resolve_file_ref(upload_id)
        if is_real_saved_attachment(saved):
            return saved
    hint = os.path.basename(str(filename or "").strip()).lower()
    if not hint:
        return None
    for upload_id, (reg_name, _) in list(getattr(_fum, "_upload_id_map", {}).items()):
        if is_placeholder_upload_id(upload_id):
            continue
        base = os.path.basename(str(reg_name or "")).lower()
        if base == hint:
            saved = _resolve_file_ref(upload_id)
            if is_real_saved_attachment(saved):
                return saved
    return None


def _attachments_from_at_file_refs(
    obj: Dict[str, Any],
    req: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for fname in _collect_at_file_refs(obj):
        if not is_image_like_filename(fname):
            continue
        saved = _resolve_at_file_by_filename(fname)
        if saved:
            key = str(saved.get("id") or fname)
            if key in seen:
                continue
            seen.add(key)
            out.append(saved)
            continue
        name = os.path.basename(fname.strip()) or fname.strip()
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": "",
                "size": 0,
                "name": name,
                "mime_type": _mime_from_name(name),
            }
        )
    return out


def _client_scope_key(req: Optional[Dict[str, Any]]) -> str:
    if not isinstance(req, dict):
        return ""
    meta = req.get("meta")
    if not isinstance(meta, dict):
        return ""
    client_ip = str(meta.get("client_ip") or meta.get("clientIp") or "").strip()
    if not client_ip:
        return ""
    return f"github_copilot:{client_ip}"


def _last_user_request_snippet(obj: Dict[str, Any], limit: int = 160) -> str:
    inp = obj.get("input")
    if not isinstance(inp, list):
        return ""
    for msg in reversed(inp):
        if not isinstance(msg, dict) or str(msg.get("role") or "").lower() != "user":
            continue
        content = msg.get("content")
        parts: List[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
        blob = "\n".join(parts)
        m = re.search(r"<userRequest>\s*(.*?)\s*</userRequest>", blob, re.DOTALL | re.IGNORECASE)
        snippet = (m.group(1) if m else blob).strip()
        return snippet[:limit]
    return ""


def _content_shape_fingerprint(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    inp = obj.get("input")
    if not isinstance(inp, list):
        return rows
    for idx, msg in enumerate(inp):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        content = msg.get("content")
        if isinstance(content, str):
            rows.append(
                {
                    "idx": idx,
                    "role": role,
                    "content_kind": "str",
                    "len": len(content),
                    "has_png": "\x89PNG" in content,
                    "has_data_image": "data:image" in content,
                }
            )
            continue
        if not isinstance(content, list):
            rows.append({"idx": idx, "role": role, "content_kind": type(content).__name__})
            continue
        for pidx, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            tlen = len(text) if isinstance(text, str) else 0
            rows.append(
                {
                    "idx": idx,
                    "part": pidx,
                    "role": role,
                    "type": str(part.get("type") or ""),
                    "keys": sorted(str(k) for k in part.keys()),
                    "len": tlen,
                    "has_png": isinstance(text, str) and "\x89PNG" in text,
                    "has_data_image": isinstance(text, str) and "data:image" in text,
                }
            )
    rows.sort(key=lambda r: int(r.get("len") or 0), reverse=True)
    return rows[:8]


def _large_string_locations(obj: Dict[str, Any], threshold: int = 3000) -> List[Dict[str, Any]]:
    locs: List[Dict[str, Any]] = []

    def _walk(o: Any, path: str) -> None:
        if isinstance(o, str):
            if len(o) >= threshold:
                locs.append(
                    {
                        "path": path,
                        "len": len(o),
                        "has_png": "\x89PNG" in o,
                        "has_data_image": "data:image" in o,
                        "has_user_attach": "user-attachments" in o,
                    }
                )
            return
        if isinstance(o, dict):
            for key, val in o.items():
                _walk(val, f"{path}.{key}")
        elif isinstance(o, list):
            for i, val in enumerate(o):
                _walk(val, f"{path}[{i}]")

    _walk(obj, "root")
    locs.sort(key=lambda x: int(x.get("len") or 0), reverse=True)
    return locs[:8]


def _recent_registry_attachments(max_age_sec: float = 180.0) -> List[Dict[str, Any]]:
    now = time.monotonic()
    out: List[Tuple[float, Dict[str, Any]]] = []
    for upload_id, (_, ts) in list(getattr(_fum, "_upload_id_map", {}).items()):
        if is_placeholder_upload_id(upload_id):
            continue
        if now - float(ts or 0) > max_age_sec:
            continue
        saved = _fum.get_saved_attachment(upload_id)
        if is_real_saved_attachment(saved):
            out.append((float(ts or 0), dict(saved)))
    out.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in out[:3]]


def _payload_image_markers(obj: Dict[str, Any], payload: bytes = b"") -> Dict[str, Any]:
    raw = payload or b""
    if not raw and isinstance(obj, dict):
        try:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception:
            raw = b""
    text = raw.decode("utf-8", errors="ignore")
    parsed_blob = json.dumps(obj, ensure_ascii=False) if isinstance(obj, dict) else ""
    binary_png = any("\x89PNG" in s for s in _walk_strings(obj, min_len=16))
    prev_id = str(obj.get("previous_response_id") or "").strip() if isinstance(obj, dict) else ""
    return {
        "top_level_keys": sorted(str(k) for k in obj.keys()) if isinstance(obj, dict) else [],
        "has_previous_response_id": bool(prev_id),
        "previous_response_id_prefix": prev_id[:24],
        "input_message_count": len(obj.get("input") or []) if isinstance(obj, dict) else 0,
        "user_request_snippet": _last_user_request_snippet(obj) if isinstance(obj, dict) else "",
        "has_attachments_key": isinstance(obj, dict) and isinstance(obj.get("attachments"), list),
        "attachments_len": len(obj.get("attachments") or []) if isinstance(obj, dict) else 0,
        "data_image_count": text.count("data:image"),
        "png_b64_marker": text.count("iVBORw0KGgo"),
        "jpeg_b64_marker": text.count("/9j/"),
        "unicode_png_escape": text.count("\\u0089\\u0050"),
        "wire_input_image": text.count("input_image"),
        "wire_user_attachments": text.count("user-attachments"),
        "wire_image_png": text.count("image/png"),
        "binary_png_in_parsed": binary_png,
        "file_id_token_count": len(_FILE_ID_TOKEN_RE.findall(parsed_blob)),
        "registry_recent_count": len(_recent_registry_attachments()),
        "at_file_refs": _collect_at_file_refs(obj) if isinstance(obj, dict) else [],
        "at_file_image_count": len(
            [f for f in _collect_at_file_refs(obj) if is_image_like_filename(f)]
        )
        if isinstance(obj, dict)
        else 0,
        "at_file_unresolved_count": sum(
            1
            for a in (_attachments_from_at_file_refs(obj) if isinstance(obj, dict) else [])
            if int(a.get("size") or 0) == 0
        ),
        "input_item_types": _scan_input_item_types(obj) if isinstance(obj, dict) else [],
        "has_attachments_tag": bool(
            re.search(r"<\s*attachments\b", _last_user_message_blob(obj), re.IGNORECASE)
        )
        if isinstance(obj, dict)
        else False,
        "gh_attachment_url_count": len(
            _GH_ATTACHMENT_URL_RE.findall(_last_user_message_blob(obj))
        )
        if isinstance(obj, dict)
        else 0,
        "attachments_block_preview": _attachments_block_blob(obj)[:180] if isinstance(obj, dict) else "",
        "attachments_block_image_names": _collect_image_names_from_blob(_attachments_block_blob(obj))
        if isinstance(obj, dict)
        else [],
        "attachments_marker_empty": bool(
            _has_attachments_marker(obj) and not _attachments_block_blob(obj)
        )
        if isinstance(obj, dict)
        else False,
        "view_image_paths": _collect_view_image_paths(obj) if isinstance(obj, dict) else [],
        "tool_output_image_paths": _collect_image_paths_from_tool_outputs(obj)
        if isinstance(obj, dict)
        else [],
        "last_user_message_len": len(_last_user_message_blob(obj)) if isinstance(obj, dict) else 0,
        "last_user_text_lens": _last_user_text_lengths(obj) if isinstance(obj, dict) else [],
        "last_user_part_keys": _last_user_part_keys(obj) if isinstance(obj, dict) else [],
        "top_content_parts": _content_shape_fingerprint(obj) if isinstance(obj, dict) else [],
        "large_strings": _large_string_locations(obj) if isinstance(obj, dict) else [],
    }


def _iter_attachment_dicts(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    top = obj.get("attachments")
    if isinstance(top, list):
        items.extend(x for x in top if isinstance(x, dict))
    inp = obj.get("input")
    if isinstance(inp, list):
        for msg in inp:
            if not isinstance(msg, dict):
                continue
            att = msg.get("attachments")
            if isinstance(att, list):
                items.extend(x for x in att if isinstance(x, dict))
    return items


def _persist_image_tuple(name: str, raw: bytes, mime: str, policy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    upload_id = _inline_upload_id(raw)
    saved_path = _fum.save_inline_upload(upload_id, name, raw, policy)
    if not saved_path:
        return None
    saved = _fum.get_saved_attachment(upload_id, fallback_name=name, fallback_mime=mime)
    if not saved:
        return None
    entry = dict(saved)
    entry["mime_type"] = mime or str(entry.get("mime_type") or "")
    entry["name"] = name or str(entry.get("name") or "")
    return entry


def _save_inline_attachments(
    obj: Dict[str, Any],
    payload: bytes = b"",
    req: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Decode inline images from the full Responses JSON tree and recent upload registry."""
    out: List[Dict[str, Any]] = []
    seen_hashes: set[str] = set()
    policy = _fum.collection_policy_from_request(req)

    def _add(name: str, raw: bytes, mime: str) -> None:
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen_hashes:
            return
        seen_hashes.add(digest)
        entry = _persist_image_tuple(name, raw, mime, policy)
        if entry:
            out.append(entry)

    meta = req.get("meta") if isinstance(req, dict) and isinstance(req.get("meta"), dict) else {}
    upload_id = str(meta.get("upload_id") or "").strip()
    if upload_id:
        saved = _resolve_file_ref(upload_id)
        if is_real_saved_attachment(saved):
            digest = str(saved.get("id") or upload_id)
            if digest not in seen_hashes:
                seen_hashes.add(digest)
                out.append(saved)

    for saved in _recent_registry_attachments():
        digest = str(saved.get("id") or "")
        if digest and digest not in seen_hashes:
            seen_hashes.add(digest)
            out.append(saved)

    scope_key = _client_scope_key(req)
    if scope_key:
        for upload_id in _fum.pop_pending_uploads_for_scope(scope_key):
            saved = _resolve_file_ref(upload_id)
            if is_real_saved_attachment(saved):
                digest = str(saved.get("id") or upload_id)
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)
                out.append(saved)

    request_id = str(req.get("request_id") or "").strip() if isinstance(req, dict) else ""
    claim_pending = getattr(_fum, "_claim_pending_upload_ids", None)
    if request_id and callable(claim_pending):
        for upload_id in claim_pending(request_id):
            saved = _resolve_file_ref(upload_id)
            if is_real_saved_attachment(saved):
                digest = str(saved.get("id") or upload_id)
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)
                out.append(saved)

    for item in _iter_attachment_dicts(obj):
        parsed = _blob_attachment_tuple(item)
        if parsed:
            _add(*parsed)

    for part in _iter_input_parts(obj):
        ptype = str(part.get("type") or "").lower()
        if ptype in ("input_file", "input_image", "file", "image"):
            parsed = _inline_image_from_part(part)
            if parsed:
                _add(*parsed)
        text = part.get("text")
        if isinstance(text, str):
            for name, raw, mime in _images_from_text_blob(text):
                _add(name, raw, mime)
        parsed = _blob_attachment_tuple(part)
        if parsed:
            _add(*parsed)

    for part in _iter_message_parts(obj):
        ptype = str(part.get("type") or "").lower()
        if ptype in ("input_file", "input_image", "file", "image"):
            parsed = _inline_image_from_part(part)
            if parsed:
                _add(*parsed)
        if ptype == "image_url":
            url_obj = part.get("image_url")
            url = url_obj.get("url") if isinstance(url_obj, dict) else url_obj
            if isinstance(url, str) and url.startswith("data:"):
                mime, raw = _decode_data_url(url)
                if raw:
                    _add(f"copilot_image{_ext_from_mime(mime or 'image/png')}", raw, mime or "image/png")
        text = part.get("text")
        if isinstance(text, str):
            for name, raw, mime in _images_from_text_blob(text):
                _add(name, raw, mime)

    for ref in _collect_file_id_tokens(obj, payload):
        saved = _resolve_file_ref(ref)
        if is_real_saved_attachment(saved):
            digest = str(saved.get("id") or ref)
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            out.append(saved)

    for text in _walk_strings(obj, min_len=32):
        for name, raw, mime in _images_from_text_blob(text):
            _add(name, raw, mime)

    return out


def _collect_attachment_refs(obj: Dict[str, Any]) -> List[Dict[str, str]]:
    refs: List[Dict[str, str]] = []
    seen: set[str] = set()

    for part in _iter_input_parts(obj):
        ptype = str(part.get("type") or "").lower()
        if ptype not in ("input_file", "input_image", "file", "image"):
            continue
        req_mime = str(part.get("mime_type") or part.get("mimeType") or "")
        req_name = str(part.get("filename") or part.get("file_name") or part.get("name") or "")
        for key in ("file_id", "id", "attachment_id"):
            val = part.get(key)
            if isinstance(val, str) and val.strip() and val.strip() not in seen:
                seen.add(val.strip())
                refs.append({"ref": val.strip(), "mime_type": req_mime, "name": req_name})
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            for key in ("file_id", "id", "url"):
                val = image_url.get(key)
                if isinstance(val, str) and val.strip() and val.strip() not in seen:
                    seen.add(val.strip())
                    refs.append({"ref": val.strip(), "mime_type": req_mime, "name": req_name})
        elif isinstance(image_url, str):
            u = image_url.strip()
            if u and not u.startswith("data:") and u not in seen:
                seen.add(u)
                refs.append({"ref": u, "mime_type": req_mime, "name": req_name})

    for part in _iter_message_parts(obj):
        ptype = str(part.get("type") or "").lower()
        if ptype == "image_url":
            url_obj = part.get("image_url")
            url = url_obj.get("url") if isinstance(url_obj, dict) else url_obj
            if isinstance(url, str) and url.strip() and not url.startswith("data:") and url not in seen:
                seen.add(url.strip())
                refs.append({"ref": url.strip(), "mime_type": "", "name": ""})
    return refs


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
    saved_path = ""

    if payload:
        try:
            saved_path = _fum.append_upload_payload(upload_id, payload, policy)
            if not saved_path:
                return _fum.build_skipped_upload_result(
                    upload_id,
                    upload_filename,
                    "collection_policy",
                )
        except Exception:
            logger.exception("copilot upload save failed: upload_id=%s", upload_id)
            return _fum.build_upload_result(upload_id=upload_id, upload_filename=upload_filename, handled=True)

    effective_name = upload_filename or upload_id[:48]
    result = _fum.build_upload_result(
        upload_id=upload_id,
        upload_filename=effective_name,
        saved_path=saved_path,
        handled=True,
    )
    scope_key = _client_scope_key(req)
    if scope_key and payload and saved_path:
        _fum.remember_pending_uploads_for_scope(scope_key, [upload_id])
    return result


def normalize_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    if is_github_copilot_skip_upload_path(req):
        return _fum.build_upload_result(handled=True)

    host = _host(req)
    path = str(_path_base(req))
    direction = str(req.get("direction") or "").lower()

    if _is_copilot_file_auxiliary_path(host, path):
        return _fum.build_upload_result(handled=True)

    raw_path = _path_raw(req)
    if _is_copilot_chat_attachment_upload(host, raw_path):
        request_id = str(req.get("request_id") or "")
        if direction == "request" and payload:
            filename, mime = _parse_attachment_upload_query(raw_path)
            pending_id = _pending_attachment_upload_id(request_id)
            policy = _fum.collection_policy_from_request(req)
            saved_path = _fum.save_inline_upload(
                pending_id,
                filename or "copilot_attachment.bin",
                payload,
                policy,
                allow_unknown_extension=not bool(filename),
            )
            if filename:
                _remember_upload_init_filename(request_id, filename)
            scope_key = _client_scope_key(req)
            if scope_key and saved_path:
                _fum.remember_pending_uploads_for_scope(scope_key, [pending_id])
            
            return _fum.build_upload_result(
                upload_id=pending_id,
                upload_filename=filename or pending_id,
                saved_path=saved_path,
                handled=True,
            )

        if direction == "response":
            server_uuid = _extract_chat_attachment_response_uuid(payload)
            filename = _UPLOAD_INIT_FILENAME_BY_REQUEST.pop(request_id, "") if request_id else ""
            pending_id = _pending_attachment_upload_id(request_id) if request_id else ""
            final_id = _copilot_attachment_upload_id(server_uuid) if server_uuid else ""
            if pending_id and final_id and pending_id != final_id:
                _fum.rekey_upload_id(pending_id, final_id)
            if final_id and filename:
                policy = _fum.collection_policy_from_request(req)
                if not _fum.get_registered_upload_filename(final_id):
                    _fum.register_upload_mapping(final_id, filename, policy)
                _mark_copilot_owned_upload(final_id)
                _fum.register_upload_server_alias(server_uuid, final_id)
                scope_key = _client_scope_key(req)
                if scope_key:
                    _fum.remember_pending_uploads_for_scope(scope_key, [final_id])
            
            return _fum.build_upload_result(
                upload_id=final_id or pending_id,
                upload_filename=filename,
                handled=True,
            )

        return _fum.build_upload_result(handled=True)

    if _is_github_copilot_attachment_download(host, raw_path):
        server_uuid = _extract_attachment_uuid_from_path(raw_path)
        upload_id = _copilot_attachment_upload_id(server_uuid) if server_uuid else ""
        if direction == "response" and upload_id and payload:
            existing = _fum.get_saved_attachment(upload_id)
            if is_real_saved_attachment(existing):
                return _fum.build_upload_result(upload_id=upload_id, handled=True)
            policy = _fum.collection_policy_from_request(req)
            ext = os.path.splitext(raw_path.split("?", 1)[0])[1]
            filename = f"{server_uuid}{ext}" if ext else server_uuid
            saved_path = _fum.save_inline_upload(
                upload_id,
                filename,
                payload,
                policy,
                allow_unknown_extension=True,
            )
            if server_uuid:
                _fum.register_upload_server_alias(server_uuid, upload_id)
            
            return _fum.build_upload_result(
                upload_id=upload_id,
                upload_filename=filename,
                saved_path=saved_path,
                handled=True,
            )
        return _fum.build_upload_result(handled=True)

    if _is_copilot_upload_init(host, path):
        request_id = str(req.get("request_id") or "")
        if direction == "request":
            filename = _extract_upload_init_filename(payload)
            _remember_upload_init_filename(request_id, filename)
            
            return _fum.build_upload_result(upload_filename=filename, handled=True)

        if direction == "response":
            uid = _extract_upload_response_id(payload)
            filename = _UPLOAD_INIT_FILENAME_BY_REQUEST.pop(request_id, "") if request_id else ""
            if uid and filename:
                policy = _fum.collection_policy_from_request(req)
                if not _fum.register_upload_mapping(uid, filename, policy):
                    return _fum.build_skipped_upload_result(uid, filename, "collection_policy")
                _mark_copilot_owned_upload(uid)
                scope_key = _client_scope_key(req)
                if scope_key:
                    _fum.remember_pending_uploads_for_scope(scope_key, [uid])
            
            return _fum.build_upload_result(upload_id=uid, upload_filename=filename, handled=True)

        return _fum.build_upload_result(handled=True)

    if "oaiusercontent.com" in host:
        upload_id = _extract_upload_id_from_path(host, path)
        if upload_id:
            result = _handle_oaiusercontent_upload(req, payload, upload_id)
            
            return result

    return None


def build_policy_attachments(
    obj: Dict[str, Any],
    payload: bytes = b"",
    req: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Resolve file refs and inline base64 images from Copilot Responses API input[]."""
    if not isinstance(obj, dict):
        return list(_STUB_ATTACHMENT)

    markers = _payload_image_markers(obj, payload)
    part_types = _scan_content_part_types(obj)
    inline_saved = _save_inline_attachments(obj, payload, req)
    wire_saved = _attachments_from_wire_uuids(obj)
    at_file_saved = _attachments_from_at_file_refs(obj, req)
    refs = _collect_attachment_refs(obj)

    out: List[Dict[str, Any]] = list(inline_saved)
    seen_ids: set[str] = {str(a.get("id") or "") for a in out if str(a.get("id") or "")}
    for item in wire_saved:
        aid = str(item.get("id") or "")
        if aid and aid in seen_ids:
            continue
        if aid:
            seen_ids.add(aid)
        out.append(item)
    for item in at_file_saved:
        aid = str(item.get("id") or "")
        if aid and aid in seen_ids:
            continue
        if aid:
            seen_ids.add(aid)
        out.append(item)
    for item in refs:
        ref = item.get("ref") or ""
        req_mime = str(item.get("mime_type") or "")
        req_name = str(item.get("name") or "")
        saved = _resolve_file_ref(ref)
        if saved:
            if req_name and not saved.get("name"):
                saved["name"] = req_name
            if req_mime and not saved.get("mime_type"):
                saved["mime_type"] = req_mime
            out.append(saved)
        else:
            out.append(
                {
                    "id": ref,
                    "size": 0,
                    "name": req_name,
                    "mime_type": req_mime or _mime_from_name(req_name),
                }
            )

    

    if not out:
        return list(_STUB_ATTACHMENT)
    return out


def _strip_raw_binary_images_from_text(text: str) -> Tuple[str, int]:
    removed = 0
    out = text or ""
    for extractor, marker in (
        (_images_from_raw_binary_text, "[AIVAX IMAGE REMOVED]"),
    ):
        for _name, raw, _mime in extractor(out):
            if not raw:
                continue
            try:
                chunk = raw.decode("latin-1")
            except Exception:
                continue
            if chunk in out:
                out = out.replace(chunk, marker, 1)
                removed += 1
    return out, removed


def _strip_data_images_from_text(text: str) -> Tuple[str, int]:
    removed = 0
    if not text or "data:image" not in text:
        return text, removed

    def _repl(m: re.Match[str]) -> str:
        nonlocal removed
        removed += 1
        return "[AIVAX IMAGE REMOVED]"

    return _DATA_IMAGE_IN_TEXT_RE.sub(_repl, text), removed


def _strip_attachments_block_from_text(text: str) -> Tuple[str, int]:
    removed = 0
    out = text or ""
    while True:
        m = _ATTACHMENTS_BLOCK_RE.search(out)
        if not m:
            break
        out = out[: m.start()] + "[AIVAX ATTACHMENTS REMOVED]" + out[m.end() :]
        removed += 1
    return out, removed


def _strip_at_file_refs_from_text(text: str) -> Tuple[str, int]:
    removed = 0

    def _repl(_m: re.Match[str]) -> str:
        nonlocal removed
        removed += 1
        return ""

    cleaned = _AT_FILE_REF_RE.sub(_repl, text or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, removed


def strip_request_attachments(obj: Any) -> int:
    """Remove file/image parts and inline image data from Copilot Responses API input[]."""
    if not isinstance(obj, dict):
        return 0
    removed = 0

    top = obj.get("attachments")
    if isinstance(top, list) and top:
        removed += len(top)
        obj["attachments"] = []

    inp = obj.get("input")
    if not isinstance(inp, list):
        return removed

    for msg in inp:
        if not isinstance(msg, dict):
            continue
        att = msg.get("attachments")
        if isinstance(att, list) and att:
            removed += len(att)
            msg["attachments"] = []
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_parts: List[Any] = []
        for part in content:
            if not isinstance(part, dict):
                new_parts.append(part)
                continue
            ptype = str(part.get("type") or "").lower()
            if ptype in ("input_file", "input_image", "file", "image"):
                removed += 1
                continue
            if ptype == "input_text" and isinstance(part.get("text"), str):
                new_text, n = _strip_data_images_from_text(str(part.get("text") or ""))
                new_text, n2 = _strip_raw_binary_images_from_text(new_text)
                new_text, n3 = _strip_attachments_block_from_text(new_text)
                new_text, n4 = _strip_at_file_refs_from_text(new_text)
                n += n2 + n3 + n4
                if n:
                    part = dict(part)
                    part["text"] = new_text
                    removed += n
            new_parts.append(part)
        if len(new_parts) != len(content):
            msg["content"] = new_parts
    return removed


# Public entry points share file_upload_manager.UPLOAD_STATE_LOCK so the
# policy loop can run normalize/persist on a worker without racing extract.
_fum.bind_upload_state_lock(globals())
