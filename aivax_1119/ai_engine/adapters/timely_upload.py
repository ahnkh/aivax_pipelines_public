"""
Timely file bytes.

Observed on timelyai.io from 10.0.50.60:

  POST /api/generate-file-path-info   ← stored name p_CHAT_{ulid}.ext + original name + blob URL
  (PUT  https://{account}.blob.core.windows.net/...  ← browser → Azure, not on timelyai.io)
  POST /api/update-file-status        ← upload finished
  GET  /proxy/api/file/{bucket}/files/by/name/{stored}/get-url  ← signed read URL
  GET  /api/download-file?filename={stored}  ← 302 to the blob (or bytes)
  POST /timely/api/stream             ← chat send, then subscribe

The blob PUT never crosses timelyai.io, so the bytes are pulled from the signed
URL Timely returned. Only files announced by generate-file-path-info become
attachments; download-file is also used for chat-history previews.

The adapter runs on the policy event loop, so the pull happens on a worker
thread as soon as the upload is finished and the chat send only waits briefly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import file_upload_manager as _fum

logger = logging.getLogger(__name__)

POLICY_ATTACHMENTS_FINAL = True

_LOCK = threading.Lock()
_PENDING: Dict[str, List[Dict[str, Any]]] = {}
_TURN_REQUEST: Dict[str, Tuple[str, float]] = {}
_TTL_SEC = 900.0
_MAX_FILES_PER_CLIENT = 16
_FETCH_MAX_BYTES = 32 * 1024 * 1024
_FETCH_TIMEOUT_S = 15.0
_CLAIM_WAIT_S = 4.0
_STORED_NAME_RE = re.compile(r"^p_[A-Za-z]+_[0-9A-Za-z]{20,}\.[0-9A-Za-z]{1,8}$")
_GENERIC_NAME_RE = re.compile(r"^[^/\\?#]{1,200}\.[0-9A-Za-z]{1,8}$")
_GET_URL_RE = re.compile(r"/files/by/name/([^/]+)/get-url", re.IGNORECASE)
_FILE_KEYS = (
    "filename",
    "file_name",
    "fileName",
    "originalName",
    "original_name",
    "originalFilename",
    "originalFileName",
    "displayName",
    "display_name",
    "name",
    "path",
    "filePath",
    "file_path",
    "key",
    "objectName",
    "object_name",
)
_NAME_SUFFIXES = (
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".txt",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".hwp",
    ".hwpx",
    ".csv",
    ".avif",
    ".zip",
    ".json",
    ".md",
    ".heic",
    ".tif",
    ".tiff",
)
_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".txt": "text/plain",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".hwp": "application/x-hwp",
    ".hwpx": "application/x-hwpx",
    ".csv": "text/csv",
    ".avif": "image/avif",
    ".zip": "application/zip",
    ".json": "application/json",
    ".md": "text/markdown",
    ".heic": "image/heic",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def _meta(req: Dict[str, Any]) -> Dict[str, Any]:
    meta = req.get("meta")
    return meta if isinstance(meta, dict) else {}


def _raw_path(req: Dict[str, Any]) -> str:
    return str(_meta(req).get("path") or "")


def _path(req: Dict[str, Any]) -> str:
    return _raw_path(req).split("?", 1)[0]


def _client_ip(req: Dict[str, Any]) -> str:
    meta = _meta(req)
    return str(meta.get("client_ip") or meta.get("clientIp") or "").strip()


def _query_filename(req: Dict[str, Any]) -> str:
    raw = _raw_path(req)
    if "?" not in raw:
        return ""
    values = parse_qs(raw.split("?", 1)[1], keep_blank_values=False)
    for key in ("filename", "fileName", "name", "file"):
        items = values.get(key) or []
        if items and str(items[0]).strip():
            return os.path.basename(unquote(str(items[0]).strip()))
    return ""


def is_download_path(path: str) -> bool:
    base = (path or "").split("?", 1)[0].rstrip("/").lower()
    return base == "/api/download-file" or base.endswith("/api/download-file")


def is_file_meta_path(path: str) -> bool:
    base = (path or "").split("?", 1)[0].lower()
    return (
        "/api/generate-file-path-info" in base
        or "/api/update-file-status" in base
        or "/proxy/api/file/" in base
        or ("/vector-store/" in base and "/files" in base)
    )


def _is_generate_path(path: str) -> bool:
    return "/api/generate-file-path-info" in (path or "").lower()


def _is_update_path(path: str) -> bool:
    return "/api/update-file-status" in (path or "").lower()


def _get_url_name(path: str) -> str:
    match = _GET_URL_RE.search(path or "")
    if not match:
        return ""
    return os.path.basename(unquote(match.group(1)))


def is_stored_name(name: str) -> bool:
    return bool(_STORED_NAME_RE.match(str(name or "").strip()))


def _looks_like_filename(value: str, generic: bool = False) -> bool:
    raw = str(value or "").strip()
    if not raw or raw.startswith(("http://", "https://")):
        return False
    name = os.path.basename(raw)
    if not name or len(name) > 240:
        return False
    if generic and _GENERIC_NAME_RE.match(name):
        return True
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in _NAME_SUFFIXES)


def _mime_for(name: str, content_type: str = "") -> str:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct and ct not in ("application/octet-stream", "application/json", "text/plain", "text/html"):
        return ct
    ext = os.path.splitext(name)[1].lower()
    return _MIME.get(ext, ct)


def _json_obj(payload: bytes) -> Optional[Any]:
    if not payload:
        return None
    stripped = payload.lstrip()
    if not stripped.startswith((b"{", b"[")):
        return None
    try:
        return json.loads(payload.decode("utf-8", errors="replace"))
    except Exception:
        return None


def _dict_names(obj: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for key, value in obj.items():
        if str(key) in _FILE_KEYS and isinstance(value, str) and _looks_like_filename(value, True):
            names.append(os.path.basename(value.strip()))
    return names


def _walk_names(obj: Any, found: List[str], depth: int = 0) -> None:
    if depth > 8 or obj is None:
        return
    if isinstance(obj, dict):
        found.extend(_dict_names(obj))
        for key, value in obj.items():
            if str(key) in _FILE_KEYS and isinstance(value, str):
                continue
            _walk_names(value, found, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:64]:
            _walk_names(item, found, depth + 1)
    elif isinstance(obj, str) and _looks_like_filename(obj):
        found.append(os.path.basename(obj.strip()))


def _unique(names: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _is_blob_url(value: str) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host.endswith(".blob.core.windows.net")


def _url_name(url: str) -> str:
    try:
        return os.path.basename(unquote(urlparse(url).path or ""))
    except Exception:
        return ""


def _walk_urls(obj: Any, out: List[Tuple[str, List[str]]], depth: int = 0) -> None:
    """Collect (blob_url, names in the same JSON object)."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        local = _dict_names(obj)
        for value in obj.values():
            if isinstance(value, str) and _is_blob_url(value):
                out.append((value.strip(), local))
            elif isinstance(value, (dict, list)):
                _walk_urls(value, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:64]:
            if isinstance(item, str) and _is_blob_url(item):
                out.append((item.strip(), []))
            else:
                _walk_urls(item, out, depth + 1)
    elif isinstance(obj, str) and _is_blob_url(obj):
        out.append((obj.strip(), []))


def _walk_dicts(obj: Any, out: List[Dict[str, Any]], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, dict):
        out.append(obj)
        for value in obj.values():
            _walk_dicts(value, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:64]:
            _walk_dicts(item, out, depth + 1)


def _announced_files(obj: Any) -> List[Tuple[str, str, str]]:
    """(stored_name, original_name, blob_url) from generate-file-path-info."""
    rows: Dict[str, Dict[str, str]] = {}
    dicts: List[Dict[str, Any]] = []
    _walk_dicts(obj, dicts)
    for item in dicts:
        names = _unique(_dict_names(item))
        stored = [name for name in names if is_stored_name(name)]
        originals = [name for name in names if not is_stored_name(name)]
        urls = [v.strip() for v in item.values() if isinstance(v, str) and _is_blob_url(v)]
        for name in stored:
            row = rows.setdefault(name, {"original": "", "url": ""})
            if len(stored) == 1 and len(originals) == 1 and not row["original"]:
                row["original"] = originals[0]
            for url in urls:
                if not row["url"] and (len(stored) == 1 or _url_name(url) == name):
                    row["url"] = url
    all_names: List[str] = []
    _walk_names(obj, all_names)
    all_names = _unique(all_names)
    url_pairs: List[Tuple[str, List[str]]] = []
    _walk_urls(obj, url_pairs)
    for url, _local in url_pairs:
        name = _url_name(url)
        if is_stored_name(name):
            rows.setdefault(name, {"original": "", "url": ""})
            if not rows[name]["url"]:
                rows[name]["url"] = url
    stored_all = [name for name in all_names if is_stored_name(name)]
    original_all = [name for name in all_names if not is_stored_name(name)]
    for name in stored_all:
        rows.setdefault(name, {"original": "", "url": ""})
    if len(rows) == 1:
        only = next(iter(rows))
        if not rows[only]["original"] and len(original_all) == 1:
            rows[only]["original"] = original_all[0]
        if not rows[only]["url"] and len(url_pairs) == 1:
            rows[only]["url"] = url_pairs[0][0]
    return [(name, row["original"], row["url"]) for name, row in rows.items()]


def _log_shape(path: str, payload: bytes) -> None:
    obj = _json_obj(payload)
    if obj is None:
        return
    names: List[str] = []
    _walk_names(obj, names)
    hosts: List[str] = []
    pairs: List[Tuple[str, List[str]]] = []
    _walk_urls(obj, pairs)
    for url, _local in pairs:
        host = urlparse(url).netloc
        if host and host not in hosts:
            hosts.append(host)
    logger.info(
        "timely file meta path=%s names=%s url_hosts=%s bytes=%d",
        path,
        _unique(names)[:8],
        hosts[:6],
        len(payload),
    )


def _turn_ts(row: Any) -> float:
    if isinstance(row, dict):
        return float(row.get("ts") or 0)
    if isinstance(row, tuple) and len(row) > 1:
        return float(row[1] or 0)
    return 0.0


def _prune(now: float) -> None:
    for ip, rows in list(_PENDING.items()):
        kept = [row for row in rows if now - float(row.get("ts") or 0) <= _TTL_SEC]
        if kept:
            _PENDING[ip] = kept
        else:
            _PENDING.pop(ip, None)
    for ip, row in list(_TURN_REQUEST.items()):
        if now - _turn_ts(row) > _TTL_SEC:
            _TURN_REQUEST.pop(ip, None)


def _blank_turn(request_id: str, now: float, sse_anchor: bool) -> Dict[str, Any]:
    return {
        "request_id": request_id,
        "prompt": "",
        "attachments": [],
        "prompt_logged": False,
        "sse_anchor": sse_anchor,
        "ts": now,
    }


def _is_stream_request(req: Dict[str, Any]) -> bool:
    base = _path(req).lower()
    return "/timely/api/stream" in base or "/api-ai/v3/llm-completion" in base


def _find_row(ip: str, stored: str) -> Optional[Dict[str, Any]]:
    key = (stored or "").lower()
    for row in _PENDING.get(ip, []):
        if str(row.get("stored") or "").lower() == key:
            return row
    return None


def _new_row(stored: str, original: str, policy: Any, now: float) -> Dict[str, Any]:
    display = original or stored
    return {
        "upload_id": f"timely-{stored}",
        "stored": stored,
        "name": display,
        "mime": _mime_for(display) or _mime_for(stored),
        "ts": now,
        "policy": policy,
        "urls": [],
        "state": "announced",
        "uploaded": False,
        "event": threading.Event(),
    }


def _announce(req: Dict[str, Any], payload: bytes) -> None:
    obj = _json_obj(payload)
    ip = _client_ip(req)
    if obj is None or not ip:
        return
    files = _announced_files(obj)
    if not files:
        return
    policy = _fum.collection_policy_from_request(req)
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        rows = _PENDING.setdefault(ip, [])
        for stored, original, url in files:
            row = _find_row(ip, stored)
            if row is None:
                row = _new_row(stored, original, policy, now)
                rows.append(row)
            elif original and row["name"] == row["stored"]:
                row["name"] = original
                row["mime"] = _mime_for(original) or row["mime"]
            if url and url not in row["urls"]:
                row["urls"].append(url)
        _PENDING[ip] = rows[-_MAX_FILES_PER_CLIENT:]
    logger.info(
        "timely upload announced client=%s files=%s",
        ip,
        [(stored, original or "-", bool(url)) for stored, original, url in files][:8],
    )


def _fetch_blob(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "aivax-timely-collect/1", "Accept": "*/*"},
    )
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S) as resp:
        final = str(getattr(resp, "geturl", lambda: url)() or url)
        if not _is_blob_url(final):
            raise ValueError("blob_redirect_denied")
        data = resp.read(_FETCH_MAX_BYTES + 1)
    if len(data) > _FETCH_MAX_BYTES:
        raise ValueError(f"blob_too_large:{len(data)}")
    return data


def _save_bytes(row: Dict[str, Any], data: bytes, source: str, ip: str) -> bool:
    saved = _fum.save_inline_upload(row["upload_id"], row["name"], data, row.get("policy"))
    if not saved:
        logger.info(
            "timely file save skipped name=%s bytes=%d source=%s",
            row["name"],
            len(data),
            source,
        )
        return False
    logger.info(
        "timely file saved name=%s stored=%s bytes=%d source=%s path=%s client=%s",
        row["name"],
        row["stored"],
        len(data),
        source,
        saved,
        ip or "-",
    )
    return True


def _next_url_locked(row: Dict[str, Any], tried: List[str]) -> str:
    for url in reversed(row["urls"]):
        if url not in tried:
            return url
    return ""


def _fetch_worker(row: Dict[str, Any], ip: str) -> None:
    last_error = ""
    tried: List[str] = []
    while True:
        with _LOCK:
            url = _next_url_locked(row, tried)
            if not url:
                # get-url may add a read URL while a write-only URL was failing.
                row["state"] = "failed"
                row["event"].set()
                break
        tried.append(url)
        try:
            data = _fetch_blob(url)
        except Exception as exc:
            last_error = f"{type(exc).__name__}:{str(exc)[:80]}"
            continue
        if data and _save_bytes(row, data, f"blob:{urlparse(url).netloc}", ip):
            with _LOCK:
                row["state"] = "saved"
                row["event"].set()
            return
    logger.warning(
        "timely file fetch failed name=%s stored=%s urls=%d error=%s",
        row["name"],
        row["stored"],
        len(row["urls"]),
        last_error or "-",
    )


def _start_fetch_locked(row: Dict[str, Any], ip: str) -> None:
    if row["state"] in ("fetching", "saved") or not row["urls"]:
        return
    row["state"] = "fetching"
    row["event"] = threading.Event()
    threading.Thread(
        target=_fetch_worker,
        args=(row, ip),
        name="timely-blob-fetch",
        daemon=True,
    ).start()


def _on_uploaded(req: Dict[str, Any]) -> None:
    ip = _client_ip(req)
    if not ip:
        return
    with _LOCK:
        _prune(time.monotonic())
        for row in _PENDING.get(ip, []):
            if row["state"] == "announced":
                row["uploaded"] = True
                _start_fetch_locked(row, ip)


def _on_get_url(req: Dict[str, Any], payload: bytes) -> None:
    ip = _client_ip(req)
    stored = _get_url_name(_path(req))
    obj = _json_obj(payload)
    if not ip or not stored or obj is None:
        return
    pairs: List[Tuple[str, List[str]]] = []
    _walk_urls(obj, pairs)
    if not pairs:
        return
    with _LOCK:
        _prune(time.monotonic())
        row = _find_row(ip, stored)
        if row is None:
            return
        for url, _local in pairs:
            if url not in row["urls"]:
                row["urls"].append(url)
        if row["state"] == "failed":
            row["state"] = "announced"
        _start_fetch_locked(row, ip)


def _save_download(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    filename = _query_filename(req)
    if not filename or not payload:
        return None
    if _json_obj(payload) is not None:
        _log_shape(_path(req), payload)
        return None
    ip = _client_ip(req)
    with _LOCK:
        _prune(time.monotonic())
        row = _find_row(ip, filename)
        if row is not None and row["state"] in ("saved", "fetching"):
            return {"handled": True, "skipped": True, "skip_reason": "timely_already_collected"}
        if row is not None:
            row["state"] = "fetching"
    if row is None:
        # Chat-history preview, not a file of the next turn.
        return {"handled": True, "skipped": True, "skip_reason": "timely_history_preview"}
    mime = _mime_for(filename, str(req.get("content_type") or ""))
    ok = _save_bytes(row, payload, "download-file", ip)
    with _LOCK:
        row["state"] = "saved" if ok else "failed"
        if ok:
            row["mime"] = row["mime"] or mime
        row["event"].set()
    return {
        "handled": True,
        "upload_id": row["upload_id"],
        "upload_filename": row["name"],
        "filename": row["name"],
        "saved_path": _fum.expected_saved_path(row["upload_id"]) if ok else "",
        "saved_files": (
            [{"upload_id": row["upload_id"], "filename": row["name"]}] if ok else []
        ),
        "skipped": not ok,
        "skip_reason": "" if ok else "collection_policy",
    }


def has_pending_files(req: Dict[str, Any]) -> bool:
    ip = _client_ip(req)
    if not ip:
        return False
    with _LOCK:
        _prune(time.monotonic())
        return bool(_PENDING.get(ip))


def remember_turn_request(req: Dict[str, Any]) -> None:
    """Bind a chat send to this client. A title that already arrived keeps its id.

    Non-stream calls (file meta, account) must not take the id. A 72-byte side
    request was doing that, so the model answer merged away from the file input.
    """
    if not _is_stream_request(req):
        return
    ip = _client_ip(req)
    request_id = str(req.get("request_id") or "").strip()
    if not ip or not request_id:
        return
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        turn = _TURN_REQUEST.get(ip)
        if isinstance(turn, dict) and turn.get("sse_anchor") and turn.get("request_id"):
            turn["sse_anchor"] = False
            turn["ts"] = now
            req["_timely_ui_message_id"] = turn["request_id"]
            return
        _TURN_REQUEST[ip] = _blank_turn(request_id, now, False)


def pending_turn_prompt(req: Dict[str, Any]) -> str:
    """User text from an earlier SSE title. A send consumes it once."""
    ip = _client_ip(req)
    if not ip:
        return ""
    with _LOCK:
        _prune(time.monotonic())
        turn = _TURN_REQUEST.get(ip)
        if not isinstance(turn, dict) or turn.get("prompt_on_send"):
            return ""
        text = str(turn.get("prompt") or "")
        if text:
            turn["prompt_on_send"] = True
        return text


def note_sse_prompt(req: Dict[str, Any], prompt: str) -> str:
    """Return the message id that should own this title, or empty to suppress it.

    A chat send that already logged the same text does not get a second input.
    A send that logged only the file is reused so the title replaces that row
    instead of opening a new one. A title that arrives first becomes the id the
    following send must keep.
    """
    text = (prompt or "").strip()
    ip = _client_ip(req)
    request_id = str(req.get("request_id") or "").strip()
    if not text or not ip or not request_id:
        return ""
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        turn = _TURN_REQUEST.get(ip)
        if isinstance(turn, dict) and turn.get("request_id") and not turn.get("sse_anchor"):
            # A start body that already logged the typed prompt owns this turn.
            # The later chat title is generated and must not replace that row.
            if turn.get("prompt_logged"):
                return ""
            turn["prompt"] = text
            turn["ts"] = now
            req["_timely_turn_attachments"] = list(turn.get("attachments") or [])
            return str(turn["request_id"])
        _TURN_REQUEST[ip] = _blank_turn(request_id, now, True)
        _TURN_REQUEST[ip]["prompt"] = text
        return request_id


def remember_turn_attachments(req: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    ip = _client_ip(req)
    if not ip or not rows:
        return
    with _LOCK:
        turn = _TURN_REQUEST.get(ip)
        if not isinstance(turn, dict):
            return
        turn["attachments"] = [dict(row) for row in rows if isinstance(row, dict)]
        turn["ts"] = time.monotonic()


def remember_turn_model(req: Dict[str, Any], model: str) -> None:
    """Keep the start-body model on this turn so the answer log uses the same id."""
    name = (model or "").strip()
    if not name:
        return
    req["_llm_model"] = name
    req["_timely_model"] = name
    ip = _client_ip(req)
    if not ip:
        return
    with _LOCK:
        turn = _TURN_REQUEST.get(ip)
        if not isinstance(turn, dict):
            return
        turn["model"] = name
        turn["ts"] = time.monotonic()


def mark_turn_prompt_logged(req: Dict[str, Any], prompt: str) -> None:
    ip = _client_ip(req)
    if not ip:
        return
    with _LOCK:
        turn = _TURN_REQUEST.get(ip)
        if not isinstance(turn, dict):
            return
        if prompt:
            turn["prompt"] = prompt
        turn["prompt_logged"] = True
        turn["ts"] = time.monotonic()


def apply_turn_request(req: Dict[str, Any]) -> None:
    """Point the subscribe response log at the chat-send request id."""
    ip = _client_ip(req)
    if not ip:
        return
    with _LOCK:
        _prune(time.monotonic())
        turn = _TURN_REQUEST.pop(ip, None)
    if not isinstance(turn, dict) or not turn.get("request_id"):
        return
    req["_timely_ui_message_id"] = turn["request_id"]
    if turn.get("attachments") and not req.get("_timely_turn_attachments"):
        req["_timely_turn_attachments"] = list(turn["attachments"])
    if turn.get("model"):
        # The start body names the model. An SSE model string must not replace it.
        req["_llm_model"] = turn["model"]
        req["_timely_model"] = turn["model"]
    if turn.get("prompt_logged"):
        req["_timely_input_already_logged"] = True


def normalize_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    direction = str(req.get("direction") or "").lower()
    path = _path(req)
    if direction == "response" and is_download_path(path):
        return _save_download(req, payload)
    if payload and direction in ("request", "response") and is_file_meta_path(path):
        _log_shape(path, payload)
        if direction == "response" and _is_generate_path(path):
            _announce(req, payload)
        elif direction == "response" and _is_update_path(path):
            _on_uploaded(req)
        elif direction == "response" and _get_url_name(path):
            _on_get_url(req, payload)
        return None
    content_type = str(req.get("content_type") or "").lower()
    if direction == "request" and "multipart/form-data" in content_type and payload:
        parts = _fum.parse_multipart(payload, content_type)
        files: List[Tuple[str, bytes]] = []
        for part in parts or []:
            body = part.get("body") or b""
            name = str(part.get("filename") or "").strip()
            if body and (name or part.get("name") == "file"):
                files.append((name or "timely-upload.bin", body if isinstance(body, bytes) else bytes(body)))
        if files:
            policy = _fum.collection_policy_from_request(req)
            result = _fum.create_pending_upload_batch_from_files(
                str(req.get("request_id") or ""),
                files,
                "timely",
                policy,
            )
            ip = _client_ip(req)
            now = time.monotonic()
            with _LOCK:
                _prune(now)
                rows = _PENDING.setdefault(ip, []) if ip else []
                for saved in result.get("saved_files") or []:
                    if not isinstance(saved, dict) or not ip:
                        continue
                    name = str(saved.get("filename") or "")
                    row = _new_row(name, name, policy, now)
                    row["upload_id"] = str(saved.get("upload_id") or row["upload_id"])
                    row["state"] = "saved"
                    row["event"].set()
                    rows.append(row)
                if ip:
                    _PENDING[ip] = rows[-_MAX_FILES_PER_CLIENT:]
            return result
    return None


def _attachment_row(row: Dict[str, Any]) -> Dict[str, Any]:
    upload_id = str(row.get("upload_id") or "")
    name = str(row.get("name") or "")
    mime = str(row.get("mime") or "")
    saved = _fum.get_saved_attachment(upload_id, fallback_name=name, fallback_mime=mime)
    if saved:
        return {
            "id": str(saved.get("id") or ""),
            "size": int(saved.get("size") or 0),
            "name": name or str(saved.get("name") or ""),
            "mime_type": mime or str(saved.get("mime_type") or ""),
        }
    return {"id": "", "size": 0, "name": name, "mime_type": mime}


def claim_policy_attachments(req: Dict[str, Any], payload: bytes) -> List[Dict[str, Any]]:
    """Attach this client's newly uploaded files to the chat send."""
    ip = _client_ip(req)
    wanted: List[str] = []
    obj = _json_obj(payload)
    if obj is not None:
        _walk_names(obj, wanted)
    wanted_keys = {name.lower() for name in wanted}
    with _LOCK:
        _prune(time.monotonic())
        rows = list(_PENDING.get(ip, []))
        if not rows:
            return []
        chosen = [
            row
            for row in rows
            if str(row.get("stored") or "").lower() in wanted_keys
            or str(row.get("name") or "").lower() in wanted_keys
        ]
        if not chosen:
            chosen = rows
        chosen_ids = {id(row) for row in chosen}
        _PENDING[ip] = [row for row in rows if id(row) not in chosen_ids]
        if not _PENDING[ip]:
            _PENDING.pop(ip, None)
        for row in chosen:
            if row["state"] in ("announced", "failed"):
                _start_fetch_locked(row, ip)
        waits = [row["event"] for row in chosen if row["state"] == "fetching"]
    deadline = time.monotonic() + _CLAIM_WAIT_S
    for event in waits:
        event.wait(max(0.0, deadline - time.monotonic()))
    out: List[Dict[str, Any]] = []
    missing: List[str] = []
    for row in chosen:
        item = _attachment_row(row)
        if item.get("id") and item.get("name"):
            out.append(item)
        else:
            missing.append(str(row.get("name") or row.get("stored") or ""))
    logger.info(
        "timely attachments claimed client=%s request_id=%s attached=%s missing=%s",
        ip or "-",
        req.get("request_id") or "-",
        [item["name"] for item in out][:8],
        missing[:8],
    )
    return out


# Public entry points share file_upload_manager.UPLOAD_STATE_LOCK so the
# policy loop can run normalize/persist on a worker without racing extract.
_fum.bind_upload_state_lock(globals())
