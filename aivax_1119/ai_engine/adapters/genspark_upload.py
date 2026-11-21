"""
Genspark chat file upload.

Browser attach and the tool CLI both land on www.genspark.ai:

  POST/PUT path containing "upload", or /api/files, or any multipart POST
  response JSON may carry file_wrapper_url / file_name / upload_url

The file bytes are stored here. The later ask_proxy turn references the
wrapper URL or the file name, and that reference becomes the policy attachment.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
from collections import deque
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import file_upload_manager as _fum

logger = logging.getLogger(__name__)

POLICY_ATTACHMENTS_FINAL = True

_INDEX: Dict[str, str] = {}
_INDEX_ORDER: deque[str] = deque()
_INDEX_LIMIT = 512
_URL_RE = re.compile(r"(/api/files/[^\s\"'\\]+)")
_URL_KEYS = ("file_wrapper_url", "file_url", "private_url", "url", "upload_url")


def _meta(req: Dict[str, Any]) -> Dict[str, Any]:
    meta = req.get("meta")
    return meta if isinstance(meta, dict) else {}


def _path(req: Dict[str, Any]) -> str:
    return str(_meta(req).get("path") or "")


def _path_base(req: Dict[str, Any]) -> str:
    base = _path(req).split("?", 1)[0].rstrip("/")
    return base or "/"


def _host(req: Dict[str, Any]) -> str:
    return str(_meta(req).get("host") or "").split(":", 1)[0].lower()


def _is_genspark_host(req: Dict[str, Any]) -> bool:
    host = _host(req)
    return host == "genspark.ai" or host.endswith(".genspark.ai")


def is_genspark_upload_path(path: str) -> bool:
    base = str(path or "").split("?", 1)[0].rstrip("/").lower() or "/"
    if "upload" in base:
        return True
    return base == "/api/files" or base.startswith("/api/files/") or base == "/api/file" or base.startswith("/api/file/")


def _is_upload_request(req: Dict[str, Any]) -> bool:
    if str(req.get("direction") or "").lower() != "request" or not _is_genspark_host(req):
        return False
    method = str(_meta(req).get("method") or "").upper()
    if method and method not in ("POST", "PUT"):
        return False
    if is_genspark_upload_path(_path(req)):
        return True
    return "multipart/form-data" in str(req.get("content_type") or "").lower()


def _is_upload_response(req: Dict[str, Any]) -> bool:
    if str(req.get("direction") or "").lower() != "response" or not _is_genspark_host(req):
        return False
    return is_genspark_upload_path(_path(req)) or "multipart/form-data" in str(req.get("content_type") or "").lower()


def _remember(key: str, upload_id: str) -> None:
    token = str(key or "").strip()
    uid = str(upload_id or "").strip()
    if not token or not uid:
        return
    if token not in _INDEX:
        _INDEX_ORDER.append(token)
        while len(_INDEX_ORDER) > _INDEX_LIMIT:
            _INDEX.pop(_INDEX_ORDER.popleft(), None)
    _INDEX[token] = uid


def _lookup(key: str) -> str:
    return str(_INDEX.get(str(key or "").strip()) or "")


def _json_obj(payload: bytes) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(payload.decode("utf-8", errors="replace"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _query_name(path: str) -> str:
    query = parse_qs(urlsplit(str(path or "")).query)
    for key in ("upload_path", "filename", "file_name", "name"):
        values = query.get(key) or []
        if values and str(values[0]).strip():
            return str(values[0]).strip().rsplit("/", 1)[-1]
    return ""


def _file_parts(payload: bytes, content_type: str) -> List[tuple]:
    is_multipart = "multipart/form-data" in content_type.lower()
    if not is_multipart and payload[:2] == b"--" and b"Content-Disposition" in payload[:800]:
        is_multipart = True
    if not is_multipart:
        return []
    found = []
    for part in _fum.parse_multipart(payload, content_type):
        body = part.get("body") or b""
        if not isinstance(body, (bytes, bytearray)) or not body:
            continue
        filename = str(part.get("filename") or "").strip()
        if filename or str(part.get("name") or "") in ("file", "upload", "blob"):
            found.append((filename, bytes(body)))
    return found


def _walk_file_refs(node: Any, found: List[Dict[str, str]]) -> None:
    if isinstance(node, dict):
        url = ""
        name = ""
        for key, value in node.items():
            if not isinstance(value, str):
                continue
            lowered = str(key).lower()
            if lowered in _URL_KEYS and ("/api/files/" in value or value.startswith("http")):
                url = url or value.strip()
            if lowered in ("file_name", "filename") and value.strip() and len(value.strip()) <= 180:
                name = name or value.strip()
            elif lowered == "name" and url and value.strip() and "/" not in value and len(value.strip()) <= 180:
                name = name or value.strip()
        if url or name:
            found.append({"url": url, "name": name})
        for value in node.values():
            _walk_file_refs(value, found)
    elif isinstance(node, list):
        for item in node[:40]:
            _walk_file_refs(item, found)
    elif isinstance(node, str) and "/api/files/" in node:
        for match in _URL_RE.findall(node):
            found.append({"url": match.rstrip(").,]}>"), "name": ""})


def _bind_saved(upload_id: str, filename: str, urls: List[str]) -> None:
    _remember(upload_id, upload_id)
    if filename:
        _remember(filename, upload_id)
        _remember(filename.lower(), upload_id)
    for url in urls:
        token = str(url or "").strip()
        if not token:
            continue
        _remember(token, upload_id)
        path = urlsplit(token).path or token
        _remember(path, upload_id)


def _resolve_upload_id(ref: Dict[str, str]) -> str:
    url = str(ref.get("url") or "").strip()
    name = str(ref.get("name") or "").strip()
    for key in (url, urlsplit(url).path if url else "", name, name.lower()):
        found = _lookup(key)
        if found:
            return found
    return ""


def normalize_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    direction = str(req.get("direction") or "").lower()
    if direction == "response":
        return _normalize_response(req, payload)
    if not _is_upload_request(req) or not payload:
        return None

    request_id = str(req.get("request_id") or "").strip()
    content_type = str(req.get("content_type") or "")
    parts = _file_parts(payload, content_type)
    if not parts:
        obj = _json_obj(payload)
        name = ""
        if isinstance(obj, dict):
            name = str(obj.get("name") or obj.get("file_name") or obj.get("filename") or "").strip()
        name = name or _query_name(_path(req))
        if name:
            _remember(f"req:{request_id}", name)
            logger.info("genspark upload meta name=%s path=%s", name, _path_base(req))
        return None

    if not parts[0][0]:
        hinted = _lookup(f"req:{request_id}") or _query_name(_path(req))
        if hinted:
            parts = [(hinted, parts[0][1])] + parts[1:]
    try:
        policy = _fum.collection_policy_from_request(req)
        result = _fum.create_pending_upload_batch_from_files(request_id, parts, "genspark", policy)
    except Exception:
        logger.exception("genspark upload save failed")
        return None
    saved = result.get("saved_files") if isinstance(result.get("saved_files"), list) else []
    for row, (filename, body) in zip(saved, parts):
        if not isinstance(row, dict):
            continue
        upload_id = str(row.get("upload_id") or "")
        _bind_saved(upload_id, filename or str(row.get("filename") or ""), [])
        logger.info(
            "genspark upload saved name=%s id=%s bytes=%d path=%s",
            filename or row.get("filename") or "-",
            upload_id,
            len(body),
            _path_base(req),
        )
    return result if result.get("handled") else None


def _normalize_response(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    if not _is_upload_response(req):
        return None
    request_id = str(req.get("request_id") or "").strip()
    obj = _json_obj(payload)
    refs: List[Dict[str, str]] = []
    if obj is not None:
        _walk_file_refs(obj, refs)
    names = [ref["name"] for ref in refs if ref.get("name")]
    hinted = _lookup(f"req:{request_id}")
    if hinted and hinted not in names:
        names.insert(0, hinted)
    policy = _fum.collection_policy_from_request(req)
    finalized = _fum.finalize_pending_uploads(request_id, names, policy)
    saved = finalized.get("saved_files") if isinstance(finalized.get("saved_files"), list) else []
    urls = [ref["url"] for ref in refs if "/api/files/" in str(ref.get("url") or "")]
    for row in saved:
        if not isinstance(row, dict):
            continue
        upload_id = str(row.get("upload_id") or "")
        filename = str(row.get("filename") or "") or (names[0] if names else "")
        _bind_saved(upload_id, filename, urls)
    upload_urls = [ref["url"] for ref in refs if str(ref.get("url") or "").startswith("http")]
    for target in upload_urls:
        split = urlsplit(target)
        logger.info(
            "genspark upload target host=%s path=%s wrapper=%s",
            split.netloc,
            split.path,
            urls[0] if urls else "-",
        )
    if saved or urls or names:
        logger.info(
            "genspark upload response files=%d names=%s wrappers=%s path=%s",
            len(saved),
            names[:4],
            urls[:4],
            _path_base(req),
        )
        return finalized if saved else None
    return None


_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]*/[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]*)(?:;[^,]*)*;base64,(?P<data>[A-Za-z0-9_\-/+=\s]+)$",
    re.IGNORECASE | re.DOTALL,
)
def _safe_media_type(value: Any) -> str:
    media_type = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", media_type):
        return media_type
    return "application/octet-stream"


def _filename_for_media(media_type: str, ordinal: int) -> str:
    """Generate a usable filename only when the Genspark wire payload has none."""
    extension = mimetypes.guess_extension(media_type, strict=False) or ".bin"
    # Python may return a compound extension; it remains a real extension
    # understood by the OS, unlike the former `.image-png` label.
    if not re.fullmatch(r"\.[A-Za-z0-9.+-]{1,20}", extension):
        extension = ".bin"
    return f"attachment-{ordinal}{extension}"


def _source_filename(node: Any) -> str:
    """Return the filename supplied by Genspark, never infer one from URL text."""
    if not isinstance(node, dict):
        return ""
    for key in ("file_name", "filename", "name"):
        value = node.get(key)
        if not isinstance(value, str):
            continue
        candidate = os.path.basename(value.replace("\\", "/")).strip()
        if candidate and len(candidate) <= 180 and not any(ord(char) < 32 for char in candidate):
            return candidate
    for value in node.values():
        if isinstance(value, dict):
            filename = _source_filename(value)
            if filename:
                return filename
    return ""


def _decode_data_uri(value: str) -> Optional[Tuple[str, bytes]]:
    text = str(value or "").strip()
    if not text:
        return None
    match = _DATA_URI_RE.match(text)
    if not match:
        return None
    try:
        raw = base64.b64decode(
            re.sub(r"\s+", "", match.group("data")).replace("-", "+").replace("_", "/"),
            validate=False,
        )
    except Exception:
        return None
    if not raw:
        return None
    return _safe_media_type(match.group("mime")), raw


def _current_user_content_parts(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    messages = obj.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "").strip().lower() in ("user", "human"):
                content = message.get("content")
                return [item for item in content if isinstance(item, dict)] if isinstance(content, list) else []
    return []


def _collect_inline_binary_attachments(obj: Dict[str, Any]) -> List[Tuple[str, bytes, str]]:
    """Extract Genspark's current-turn structured content parts.

    Observed protocol: `messages[-1].content[]` has an `image_url` part whose
    nested `url` is a `data:<MIME>;base64,...` URI. Each part is an attachment
    occurrence, so order and duplicate files are retained for multi-select.
    """
    found: List[Tuple[str, bytes, str]] = []
    
    def data_uri_in_part(node: Any) -> Optional[Tuple[str, bytes]]:
        if isinstance(node, str):
            return _decode_data_uri(node)
        if isinstance(node, dict):
            for key in ("url", "source", "data"):
                decoded = data_uri_in_part(node.get(key))
                if decoded:
                    return decoded
            for value in node.values():
                if isinstance(value, dict):
                    decoded = data_uri_in_part(value)
                    if decoded:
                        return decoded
        return None

    for part in _current_user_content_parts(obj):
        decoded = data_uri_in_part(part)
        if not decoded:
            continue
        media_type, raw = decoded
        source_name = _source_filename(part)
        filename = source_name or _filename_for_media(media_type, len(found) + 1)
        found.append((filename, raw, media_type))
    return found


def _saved_attachment_rows(
    saved_files: List[Dict[str, Any]],
    media_types: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for index, item in enumerate(saved_files):
        if not isinstance(item, dict):
            continue
        upload_id = str(item.get("upload_id") or "")
        name = str(item.get("filename") or "")
        media_type = str((media_types or [""])[index] if index < len(media_types or []) else "")
        saved = (
            _fum.get_saved_attachment(upload_id, fallback_name=name, fallback_mime=media_type)
            if upload_id
            else None
        )
        if not saved:
            continue
        rows.append(
            {
                "id": str(saved.get("id") or ""),
                "size": int(saved.get("size") or 0),
                "name": name or str(saved.get("name") or ""),
                "mime_type": media_type or str(saved.get("mime_type") or ""),
            }
        )
    return rows


def build_policy_attachments(obj: Dict[str, Any], req: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    refs: List[Dict[str, str]] = []
    _walk_file_refs(obj, refs)
    attachments: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        upload_id = _resolve_upload_id(ref)
        if not upload_id or upload_id in seen:
            continue
        saved = _fum.get_saved_attachment(upload_id, fallback_name=str(ref.get("name") or ""))
        if not saved:
            continue
        seen.add(upload_id)
        attachments.append(
            {
                "id": str(saved.get("id") or ""),
                "size": int(saved.get("size") or 0),
                "name": str(ref.get("name") or saved.get("name") or ""),
                "mime_type": str(saved.get("mime_type") or ""),
            }
        )
    inline_attachments = _collect_inline_binary_attachments(obj)
    if inline_attachments:
        request_id = str((req or {}).get("request_id") or "genspark-inline")
        try:
            policy = _fum.collection_policy_from_request(req)
            saved = _fum.create_pending_upload_batch_from_files(
                request_id,
                [(name, raw) for name, raw, _ in inline_attachments],
                "genspark",
                policy,
            )
        except Exception:
            logger.exception("genspark inline image save failed")
            saved = {}
        rows = _saved_attachment_rows(
            saved.get("saved_files") if isinstance(saved, dict) else [],
            [media_type for _, _, media_type in inline_attachments],
        )
        logger.info(
            "genspark inline attachment count=%d saved=%d media_types=%s",
            len(inline_attachments),
            len(rows),
            [media_type for _, _, media_type in inline_attachments],
        )
        attachments.extend(rows)
    return attachments
