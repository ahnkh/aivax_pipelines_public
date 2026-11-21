"""
Perplexity file upload and policy attachment assembly.

Flow (pcap /backup/pcap/perp_file.pcap, 2026+):
  POST www.perplexity.ai/rest/uploads/batch_create_upload_urls
    {"files": {"<client_uuid>": {"filename", "content_type", "file_size", ...}, ...}}
  response
    {"file_uuids": ["<server_uuid>", ...]}   # same order as files object keys
  PUT/POST ppl-ai-file-upload.s3.amazonaws.com
    /web/direct-files/attachments/images/<user>/<file_uuid>/<slug>
  chat POST /rest/sse/perplexity_ask
    params.attachments: ["https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/.../<file_uuid>/..."]

Collection strategy (2026-09-03, UI-safe):
  1) Primary — ask-time S3 HTTPS GET (`_ensure_saved_from_s3_url`) when
     params.attachments has a URL. S3 PUT may stay Fast (no Deep divert /
     no CIDR) so upload UI cannot hairpin-RST.
  2) Optional — hostname Deep for `ppl-ai-file-upload*` only (no broad
     `deep_path_cidrs`). When decryptor cleartexts those hosts, PUT is teed
     via `normalize_upload` and ask-time fetch is a no-op if already on disk.
     Broad us-east-1 S3 CIDRs remain OPS emergency / lab only — they break UI
     when S3 is still TLS on Deep.

Adapter owns upload + attachment resolution; file_upload_manager is storage only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

import file_upload_manager as _fum

logger = logging.getLogger(__name__)

POLICY_ATTACHMENTS_FINAL = True

_ATTACHMENT_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ATTACHMENT_UUID_FIND_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# pcap: /web/direct-files/attachments/images/<user>/<uuid>/<slug>
# also tolerate legacy /attachments/<digits>/<uuid>/ and /images/<seg>/<uuid>/
_FILE_UUID_PATH_RE = re.compile(
    r"(?:/web/direct-files)?/attachments/(?:images/)?[^/]+/"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/",
    re.IGNORECASE,
)
_FILE_UUID_PATH_LEGACY_RE = re.compile(
    r"/images/[^/]+/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/",
    re.IGNORECASE,
)
_ASCII_SLUG_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)

_PPLX_FILE_META: Dict[str, Dict[str, Any]] = {}
_PPLX_FILE_META_ORDER: deque[str] = deque()
_PPLX_FILE_META_LIMIT = 512

# batch_create: request_id / visitor scope → ordered client_ids (zip with file_uuids)
_PENDING_BATCH_CLIENT_IDS: Dict[str, List[str]] = {}
_PENDING_BATCH_CLIENT_IDS_BY_SCOPE: Dict[str, List[str]] = {}
_PENDING_BATCH_LIMIT = 256
_PENDING_BATCH_FIFO: deque[str] = deque()
_PENDING_BATCH_FIFO_LIMIT = 128
_S3_FETCH_TIMEOUT_S = 8.0
_S3_FETCH_MAX_BYTES = 32 * 1024 * 1024


def _safe_utf8(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _meta(req: Dict[str, Any]) -> Dict[str, Any]:
    meta = req.get("meta")
    return meta if isinstance(meta, dict) else {}


def _meta_host(req: Dict[str, Any]) -> str:
    return str(_meta(req).get("host") or "").lower()


def _meta_path(req: Dict[str, Any]) -> str:
    return str(_meta(req).get("path") or "")


def _path_base(path: str) -> str:
    return str(path or "").split("?", 1)[0]


def _is_s3_slug_filename(name: str) -> bool:
    base = os.path.basename(str(name or "").strip().replace("\\", "/"))
    if not base or re.search(r"[^\x00-\x7F]", base):
        return False
    return bool(_ASCII_SLUG_FILENAME_RE.match(base))


def _preferred_filename(*names: str) -> str:
    best = ""
    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        if not best:
            best = name
            continue
        if _is_s3_slug_filename(best) and not _is_s3_slug_filename(name):
            best = name
    return best


def _extract_named_cookie(cookie_str: str, name: str) -> str:
    if not cookie_str or not name:
        return ""
    m = re.search(r"(?:^|;\s*)" + re.escape(name) + r"=([^;\s]+)", cookie_str)
    return unquote(m.group(1).strip()) if m else ""


def _pplx_upload_scope(req: Dict[str, Any]) -> str:
    """Visitor/cookie scope so Inline request_id-up-* mismatch still zips batch resp."""
    meta = _meta(req)
    visitor = str(meta.get("pplx_visitor_id") or meta.get("pplx-visitor-id") or "").strip()
    if not visitor:
        visitor = _extract_named_cookie(str(meta.get("cookie") or ""), "pplx.visitor-id")
    account = str(meta.get("x-pplx-account") or meta.get("x_pplx_account") or "").strip()
    if visitor:
        return f"pplx:{visitor}"
    if account:
        return f"pplx-acct:{account}"
    return ""


def _is_perplexity_web_host(host: str) -> bool:
    return "perplexity.ai" in str(host or "").lower()


def is_perplexity_s3_host(host: str) -> bool:
    """True for Perplexity upload origins (global + regional S3 / CloudFront / R2)."""
    h = str(host or "").lower().split(":")[0].strip().rstrip(".")
    if "ppl-ai-file-upload" not in h:
        return False
    return (
        "amazonaws.com" in h
        or "cloudfront.net" in h
        or h.endswith(".r2.dev")
        or "r2.cloudflarestorage.com" in h
    )


def _s3_https_url_allowed(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    return is_perplexity_s3_host(parsed.netloc)


def _fetch_s3_object_bytes(url: str, max_bytes: int) -> bytes:
    """GET an already-uploaded object (ask-time primary path when PUT was Fast)."""
    cap = max(1, min(int(max_bytes or _S3_FETCH_MAX_BYTES), _S3_FETCH_MAX_BYTES))
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": "aivax-perplexity-collect/1",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=_S3_FETCH_TIMEOUT_S) as resp:
        final = str(getattr(resp, "geturl", lambda: url)() or url)
        if not _s3_https_url_allowed(final):
            raise ValueError("s3_redirect_denied")
        data = resp.read(cap + 1)
    if len(data) > cap:
        raise ValueError(f"s3_too_large:{len(data)}>{cap}")
    return data


def _ensure_saved_from_s3_url(
    req: Dict[str, Any],
    upload_id: str,
    file_name: str,
    mime_type: str,
    url: str,
) -> None:
    """Ask-time primary collect: pull object bytes if S3 PUT never hit Deep tee."""
    uid = str(upload_id or "").strip()
    href = str(url or "").strip()
    if not uid:
        return
    if _fum.get_saved_attachment(uid, fallback_name=file_name, fallback_mime=mime_type):
        return
    # Prefer ask URL; fall back to meta-cached URL (batch_create / prior ask).
    candidates: List[str] = []
    for candidate in (href, str(_lookup_file_meta(uid).get("url") or "").strip()):
        if candidate and candidate not in candidates and _s3_https_url_allowed(candidate):
            candidates.append(candidate)
    if not candidates:
        return
    policy = _fum.collection_policy_from_request(req)
    try:
        maxsize = int(policy.get("maxsize") or 0) or _S3_FETCH_MAX_BYTES
    except Exception:
        maxsize = _S3_FETCH_MAX_BYTES
    data = b""
    last_err: Optional[BaseException] = None
    used = ""
    for candidate in candidates:
        try:
            data = _fetch_s3_object_bytes(candidate, maxsize)
            used = candidate
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as exc:
            last_err = exc
            logger.warning(
                "perplexity s3 origin fetch failed uid=%s err=%s url=%s",
                uid[:64],
                exc,
                candidate[:120],
            )
    if not data:
        if last_err is not None:
            logger.info(
                "perplexity s3 origin fetch exhausted uid=%s candidates=%d (PUT tee / hostname Deep may still fill later)",
                uid[:64],
                len(candidates),
            )
        return
    name = file_name or _filename_from_url_or_path(used or href) or f"{uid}.bin"
    saved = _fum.save_inline_upload(
        uid, name, data, policy, allow_unknown_extension=True
    )
    logger.info(
        "perplexity s3 origin fetch saved uid=%s bytes=%d path=%s (ask-time primary)",
        uid[:64],
        len(data),
        (saved or "")[:120],
    )


def _is_perplexity_s3_host(host: str) -> bool:
    return is_perplexity_s3_host(host)


def _http_netloc(value: str) -> str:
    text = str(value or "").strip()
    if not text.lower().startswith(("http://", "https://")):
        return ""
    try:
        return str(urlparse(text).netloc or "").split("@")[-1].split(":")[0].lower()
    except Exception:
        return ""


def _collect_http_hosts(obj: Any, limit: int = 12) -> List[str]:
    """Unique http(s) hosts from a JSON tree — log destinations, never signed query params."""
    out: List[str] = []
    seen: set[str] = set()

    def walk(node: Any, depth: int = 0) -> None:
        if len(out) >= limit or depth > 6:
            return
        if isinstance(node, str):
            host = _http_netloc(node)
            if host and host not in seen:
                seen.add(host)
                out.append(host)
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)
            return
        if isinstance(node, list):
            for value in node[:32]:
                walk(value, depth + 1)

    walk(obj)
    return out


def _log_upload_destinations(kind: str, obj: Any) -> None:
    hosts = _collect_http_hosts(obj)
    if hosts:
        logger.info("perplexity %s upload destinations hosts=%s", kind, hosts)
    else:
        logger.info("perplexity %s upload destinations hosts=[]", kind)


def _is_batch_create_path(path: str) -> bool:
    return "batch_create_upload_urls" in _path_base(path).lower()


def _extract_file_uuid_from_path(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    if _ATTACHMENT_UUID_RE.match(text):
        return text
    for pattern in (_FILE_UUID_PATH_RE, _FILE_UUID_PATH_LEGACY_RE):
        match = pattern.search(text)
        if match:
            return str(match.group(1) or "").strip()
    # Bare path segment that is a UUID
    for part in text.split("/"):
        token = part.split("?", 1)[0].strip()
        if _ATTACHMENT_UUID_RE.match(token):
            return token
    return ""


def _filename_from_url_or_path(value: str) -> str:
    try:
        path = unquote(urlparse(str(value or "")).path or "")
    except Exception:
        path = str(value or "").split("?", 1)[0]
    return os.path.basename(path.replace("\\", "/"))


def _store_file_meta(
    upload_id: str,
    file_name: str = "",
    size_bytes: int = 0,
    mime_type: str = "",
    url: str = "",
) -> None:
    uid = str(upload_id or "").strip().lower()
    if not uid:
        return
    existing = _PPLX_FILE_META.get(uid, {})
    name = _preferred_filename(str(existing.get("file_name") or ""), file_name)
    try:
        size = max(int(existing.get("size_bytes") or 0), int(size_bytes or 0))
    except Exception:
        size = int(existing.get("size_bytes") or 0)
    mime = str(mime_type or existing.get("mime_type") or "").strip()
    stored_url = str(url or existing.get("url") or "").strip()
    if uid not in _PPLX_FILE_META:
        _PPLX_FILE_META_ORDER.append(uid)
        while len(_PPLX_FILE_META_ORDER) > _PPLX_FILE_META_LIMIT:
            oldest = _PPLX_FILE_META_ORDER.popleft()
            _PPLX_FILE_META.pop(oldest, None)
    _PPLX_FILE_META[uid] = {
        "upload_id": upload_id,
        "file_name": name,
        "size_bytes": size,
        "mime_type": mime,
        "url": stored_url,
    }


def _lookup_file_meta(upload_id: str) -> Dict[str, Any]:
    uid = str(upload_id or "").strip().lower()
    if not uid:
        return {}
    return dict(_PPLX_FILE_META.get(uid, {}))


def _register_filename(
    upload_id: str,
    filename: str,
    policy: Optional[Dict[str, Any]] = None,
) -> None:
    uid = str(upload_id or "").strip()
    name = str(filename or "").strip()
    if not uid or not name:
        return
    existing = _fum.get_registered_upload_filename(uid)
    chosen = _preferred_filename(existing, name)
    if not chosen:
        return
    _fum.register_upload_mapping(uid, chosen, policy, allow_unknown_extension=True)


def _parse_batch_request_files(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    files = obj.get("files")
    if not isinstance(files, dict):
        return []
    items: List[Dict[str, Any]] = []
    # Preserve JSON object key order (pcap zip with file_uuids[]).
    for client_id, row in files.items():
        if not isinstance(row, dict):
            continue
        filename = str(
            row.get("filename") or row.get("file_name") or row.get("name") or ""
        ).strip()
        mime = str(
            row.get("content_type") or row.get("mime_type") or row.get("mimeType") or ""
        ).strip()
        try:
            size = int(row.get("file_size") or row.get("size") or 0)
        except Exception:
            size = 0
        items.append(
            {
                "client_id": str(client_id or "").strip(),
                "file_name": filename,
                "mime_type": mime,
                "size_bytes": size,
            }
        )
    return items


def _remember_pending_batch_client_ids(
    req: Dict[str, Any],
    client_ids: List[str],
) -> None:
    clean = [str(x or "").strip() for x in client_ids if str(x or "").strip()]
    if not clean:
        return
    request_id = str(req.get("request_id") or "").strip()
    # Strip Inline "-up-*" / response suffix so req/resp can share a key when possible.
    base_rid = re.sub(r"-up-[0-9a-fA-F]+$", "", request_id)
    if base_rid:
        if len(_PENDING_BATCH_CLIENT_IDS) >= _PENDING_BATCH_LIMIT:
            oldest = next(iter(_PENDING_BATCH_CLIENT_IDS))
            _PENDING_BATCH_CLIENT_IDS.pop(oldest, None)
        _PENDING_BATCH_CLIENT_IDS[base_rid] = list(clean)
        if request_id and request_id != base_rid:
            _PENDING_BATCH_CLIENT_IDS[request_id] = list(clean)
    scope = _pplx_upload_scope(req)
    if scope:
        if len(_PENDING_BATCH_CLIENT_IDS_BY_SCOPE) >= _PENDING_BATCH_LIMIT:
            oldest = next(iter(_PENDING_BATCH_CLIENT_IDS_BY_SCOPE))
            _PENDING_BATCH_CLIENT_IDS_BY_SCOPE.pop(oldest, None)
        _PENDING_BATCH_CLIENT_IDS_BY_SCOPE[scope] = list(clean)
    for cid in clean:
        _PENDING_BATCH_FIFO.append(cid)
    while len(_PENDING_BATCH_FIFO) > _PENDING_BATCH_FIFO_LIMIT:
        _PENDING_BATCH_FIFO.popleft()


def _claim_pending_batch_client_ids(req: Dict[str, Any], count: int) -> List[str]:
    n = max(0, int(count or 0))
    if n <= 0:
        return []
    request_id = str(req.get("request_id") or "").strip()
    base_rid = re.sub(r"-up-[0-9a-fA-F]+$", "", request_id)
    for key in (request_id, base_rid):
        if key and key in _PENDING_BATCH_CLIENT_IDS:
            claimed = list(_PENDING_BATCH_CLIENT_IDS.pop(key) or [])
            if key == request_id and base_rid and base_rid != request_id:
                _PENDING_BATCH_CLIENT_IDS.pop(base_rid, None)
            elif key == base_rid and request_id and request_id != base_rid:
                _PENDING_BATCH_CLIENT_IDS.pop(request_id, None)
            scope = _pplx_upload_scope(req)
            if scope:
                _PENDING_BATCH_CLIENT_IDS_BY_SCOPE.pop(scope, None)
            for cid in claimed:
                try:
                    _PENDING_BATCH_FIFO.remove(cid)
                except ValueError:
                    pass
            return claimed[:n] if len(claimed) > n else claimed

    scope = _pplx_upload_scope(req)
    if scope and scope in _PENDING_BATCH_CLIENT_IDS_BY_SCOPE:
        claimed = list(_PENDING_BATCH_CLIENT_IDS_BY_SCOPE.pop(scope) or [])
        for cid in claimed:
            try:
                _PENDING_BATCH_FIFO.remove(cid)
            except ValueError:
                pass
        return claimed[:n] if len(claimed) > n else claimed

    out: List[str] = []
    while _PENDING_BATCH_FIFO and len(out) < n:
        out.append(_PENDING_BATCH_FIFO.popleft())
    return out


def _handle_batch_create_request(req: Dict[str, Any], obj: Dict[str, Any]) -> Dict[str, Any]:
    policy = _fum.collection_policy_from_request(req)
    items = _parse_batch_request_files(obj)
    client_ids: List[str] = []
    for item in items:
        client_id = str(item.get("client_id") or "").strip()
        if not client_id:
            continue
        client_ids.append(client_id)
        filename = str(item.get("file_name") or "")
        mime = str(item.get("mime_type") or "")
        try:
            size = int(item.get("size_bytes") or 0)
        except Exception:
            size = 0
        _store_file_meta(client_id, filename, size, mime)
        if filename:
            _register_filename(client_id, filename, policy)
    if client_ids:
        _remember_pending_batch_client_ids(req, client_ids)
        # Also stash ordered client ids on scope pending helper for multi-file promote.
        scope = _pplx_upload_scope(req)
        if scope:
            _fum.remember_pending_uploads_for_scope(scope, client_ids)
    if items:
        logger.info(
            "perplexity batch_create_upload_urls: remembered %d file(s) names=%s scope=%s",
            len(items),
            [str(x.get("file_name") or "")[:48] for x in items],
            _pplx_upload_scope(req) or "-",
        )
    return _fum.build_upload_result(handled=True)


def _handle_batch_create_response(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None

    file_uuids = obj.get("file_uuids") or obj.get("fileUuids")
    if not isinstance(file_uuids, list) or not file_uuids:
        # Legacy results map (older API) — keep minimal support.
        results = obj.get("results")
        if isinstance(results, dict) and results:
            return _handle_legacy_results_response(req, results)
        _log_upload_destinations("batch_create_response", obj)
        return None

    pending = _claim_pending_batch_client_ids(req, len(file_uuids))
    policy = _fum.collection_policy_from_request(req)
    mapped = 0
    for idx, raw_uid in enumerate(file_uuids):
        file_uuid = str(raw_uid or "").strip()
        if not file_uuid or not _ATTACHMENT_UUID_RE.match(file_uuid):
            continue
        client_id = pending[idx] if idx < len(pending) else ""
        client_meta = _lookup_file_meta(client_id) if client_id else {}
        file_name = _preferred_filename(
            _fum.get_registered_upload_filename(client_id) if client_id else "",
            str(client_meta.get("file_name") or ""),
        )
        mime = str(client_meta.get("mime_type") or "").strip()
        try:
            size = int(client_meta.get("size_bytes") or 0)
        except Exception:
            size = 0
        if client_id and client_id != file_uuid:
            _fum.register_upload_server_alias(client_id, file_uuid)
        if file_name:
            _register_filename(file_uuid, file_name, policy)
        _store_file_meta(file_uuid, file_name, size, mime)
        mapped += 1

    _log_upload_destinations("batch_create_response", obj)
    logger.info(
        "perplexity batch_create_upload_urls response: mapped %d file(s) pending=%d",
        mapped,
        len(pending),
    )
    return _fum.build_upload_result(handled=True)


def _handle_legacy_results_response(
    req: Dict[str, Any],
    results: Dict[str, Any],
) -> Dict[str, Any]:
    policy = _fum.collection_policy_from_request(req)
    mapped = 0
    for client_id, row in results.items():
        if not isinstance(row, dict):
            continue
        fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
        key_value = str(fields.get("key") or "")
        s3_url = str(row.get("s3_object_url") or row.get("s3ObjectUrl") or "")
        file_uuid = (
            str(row.get("file_uuid") or row.get("fileUuid") or "").strip()
            or _extract_file_uuid_from_path(key_value)
            or _extract_file_uuid_from_path(s3_url)
        )
        if not file_uuid:
            continue
        client_meta = _lookup_file_meta(client_id)
        file_name = _preferred_filename(
            _fum.get_registered_upload_filename(str(client_id or "")),
            str(client_meta.get("file_name") or ""),
            _filename_from_url_or_path(s3_url or key_value),
        )
        mime = str(
            client_meta.get("mime_type")
            or fields.get("Content-Type")
            or fields.get("content_type")
            or ""
        ).strip()
        try:
            size = int(client_meta.get("size_bytes") or 0)
        except Exception:
            size = 0
        if client_id and str(client_id) != file_uuid:
            _fum.register_upload_server_alias(str(client_id), file_uuid)
        if file_name:
            _register_filename(file_uuid, file_name, policy)
        _store_file_meta(file_uuid, file_name, size, mime, url=s3_url)
        mapped += 1
    _log_upload_destinations("legacy_results", results)
    logger.info("perplexity legacy results response: mapped %d file(s)", mapped)
    return _fum.build_upload_result(handled=True)


def _is_multipart_body(req: Dict[str, Any], payload: bytes) -> bool:
    content_type = str(req.get("content_type") or "")
    if "multipart/form-data" in content_type.lower():
        return True
    return bool(payload[:2] == b"--" and b"Content-Disposition" in payload[:512])


def _handle_s3_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    meta = _meta(req)
    path = str(meta.get("path") or "")
    host = _meta_host(req)
    file_uuid = _extract_file_uuid_from_path(path)
    filename_hint = _filename_from_url_or_path(path)
    file_body = b""

    if _is_multipart_body(req, payload):
        parts = _fum.parse_multipart(payload, str(req.get("content_type") or ""))
        key_value = ""
        tagging_value = ""
        for part in parts:
            pname = str(part.get("name") or "").strip().lower()
            body = part.get("body", b"")
            if pname == "key" and body:
                key_value = body.decode("utf-8", errors="replace").strip()
            elif pname == "tagging" and body:
                tagging_value = body.decode("utf-8", errors="replace").strip()
            elif pname == "file" or part.get("filename"):
                file_body = body
                if part.get("filename"):
                    filename_hint = _preferred_filename(
                        filename_hint, str(part.get("filename") or "")
                    )
        if not file_uuid:
            file_uuid = _extract_file_uuid_from_path(key_value)
        if not file_uuid and tagging_value:
            # S3 object tagging often embeds the Perplexity file UUID.
            file_uuid = _extract_file_uuid_from_path(tagging_value)
            if not file_uuid:
                match = _ATTACHMENT_UUID_FIND_RE.search(tagging_value)
                if match:
                    file_uuid = str(match.group(0) or "").strip()
        if not file_body:
            for part in reversed(parts):
                if len(part.get("body", b"")) > 256:
                    file_body = part["body"]
                    break
        if key_value and not filename_hint:
            filename_hint = _filename_from_url_or_path(key_value)
    else:
        # Modern path: raw PUT/POST body to /web/direct-files/.../<uuid>/<slug>
        file_body = payload

    if not file_uuid or not file_body:
        if _is_perplexity_s3_host(host):
            logger.warning(
                "perplexity s3 upload missing uuid/body path=%s bytes=%d",
                path[:160],
                len(payload),
            )
        return None

    cached = _lookup_file_meta(file_uuid)
    # server alias may point client→file; also try reverse via registered name
    filename = _preferred_filename(
        str(cached.get("file_name") or ""),
        _fum.get_registered_upload_filename(file_uuid),
        filename_hint,
    )
    mime = str(cached.get("mime_type") or "").strip()
    try:
        cached_size = int(cached.get("size_bytes") or 0)
    except Exception:
        cached_size = 0

    try:
        policy = _fum.collection_policy_from_request(req)
        result = _fum.save_inline_upload_result(
            file_uuid, filename or file_uuid, file_body, policy
        )
        if result.get("handled"):
            if filename:
                _register_filename(file_uuid, filename, policy)
            size = cached_size if cached_size > 0 else len(file_body)
            _store_file_meta(file_uuid, filename, size, mime)
            logger.info(
                "perplexity normalize_upload: saved id=%s name=%s (%d bytes)",
                file_uuid,
                filename or file_uuid,
                len(file_body),
            )
        return result
    except Exception:
        logger.exception("perplexity normalize_upload save failed: %s", file_uuid)
    return None


def normalize_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    direction = str(req.get("direction") or "").lower()
    host = _meta_host(req)
    path = _meta_path(req)

    if direction == "response":
        if _is_perplexity_web_host(host) and _is_batch_create_path(path):
            return _handle_batch_create_response(req, payload)
        return None

    if direction != "request" or not payload:
        return None

    # batch_create metadata (multi-file names) on perplexity.ai
    if _is_perplexity_web_host(host) and _is_batch_create_path(path):
        content_type = str(req.get("content_type") or "").lower()
        if "application/json" in content_type or payload[:1] in (b"{", b"["):
            try:
                obj = json.loads(_safe_utf8(payload))
            except Exception:
                obj = None
            if isinstance(obj, dict) and isinstance(obj.get("files"), dict):
                return _handle_batch_create_request(req, obj)
        return _fum.build_upload_result(handled=True)

    # S3 binary (Inline MITM or Mirror cleartext). Regional SNI:
    # ppl-ai-file-upload.s3.us-east-1.amazonaws.com — not only the global host.
    if _is_perplexity_s3_host(host):
        return _handle_s3_upload(req, payload)

    path_l = path.lower()
    # Mirror/Fast may label S3 as perplexity via host map; also accept path-style.
    if "/web/direct-files/" in path_l or "/direct-files/" in path_l:
        return _handle_s3_upload(req, payload)
    if "ppl-ai-file-upload" in path_l and "amazonaws.com" in host:
        return _handle_s3_upload(req, payload)

    return None


def strip_chat_attachments(obj: Dict[str, Any]) -> int:
    """Remove chat attachment refs before upstream MASK/BLOCK send."""
    if not isinstance(obj, dict):
        return 0
    removed = 0

    params = obj.get("params")
    if isinstance(params, dict):
        att = params.get("attachments")
        if isinstance(att, list) and att:
            removed += len(att)
            params["attachments"] = []

    for key in ("attachments", "file_attachments", "fileAttachments"):
        val = obj.get(key)
        if isinstance(val, list) and val:
            removed += len(val)
            obj[key] = []

    files = obj.get("files")
    if isinstance(files, list) and files:
        removed += len(files)
        obj["files"] = []

    return removed


def _collect_attachment_refs(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect attachment URL / object refs from ask JSON (pcap: params.attachments[])."""
    refs: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add_ref(
        file_uuid: str,
        name: str = "",
        mime: str = "",
        size: int = 0,
        url: str = "",
    ) -> None:
        uid = str(file_uuid or "").strip()
        if not uid or uid in seen or not _ATTACHMENT_UUID_RE.match(uid):
            return
        seen.add(uid)
        refs.append(
            {
                "file_uuid": uid,
                "file_name": name,
                "mime_type": mime,
                "size_bytes": size,
                "url": str(url or "").strip(),
            }
        )

    def process_items(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, str):
                uid = _extract_file_uuid_from_path(item)
                name = _filename_from_url_or_path(item)
                add_ref(uid, name=name, url=item)
                continue
            if not isinstance(item, dict):
                continue
            uid = str(
                item.get("file_uuid")
                or item.get("fileUuid")
                or item.get("uuid")
                or item.get("id")
                or item.get("upload_id")
                or item.get("uploadId")
                or ""
            ).strip()
            url = str(
                item.get("url")
                or item.get("s3_url")
                or item.get("s3_object_url")
                or item.get("s3ObjectUrl")
                or ""
            )
            if not uid:
                uid = _extract_file_uuid_from_path(url)
            name = str(
                item.get("name")
                or item.get("file_name")
                or item.get("filename")
                or item.get("fileName")
                or ""
            )
            if not name and url:
                name = _filename_from_url_or_path(url)
            mime = str(
                item.get("mime_type")
                or item.get("mimeType")
                or item.get("content_type")
                or ""
            )
            try:
                size = int(item.get("size") or item.get("file_size") or 0)
            except Exception:
                size = 0
            add_ref(uid, name, mime, size, url)

    params = obj.get("params")
    if isinstance(params, dict):
        process_items(params.get("attachments"))

    for key in ("attachments", "files", "file_attachments", "fileAttachments"):
        process_items(obj.get(key))

    return refs


def build_policy_attachments(
    req: Dict[str, Any],
    obj: Dict[str, Any],
    payload: bytes,
) -> List[Dict[str, Any]]:
    """Build file_block_filter attachments from params.attachments URL UUIDs.

    Prefer ask-time S3 GET when bytes were not teed on PUT (Fast S3). If
    hostname Deep already saved the object, fetch is a cheap no-op.
    """
    del payload  # ask body itself has no file bytes; S3 tee / ask-time GET fills disk
    refs = _collect_attachment_refs(obj)
    if not refs:
        return [{"id": "", "size": 0, "name": "", "mime_type": ""}]

    out: List[Dict[str, Any]] = []
    for ref in refs:
        file_uuid = str(ref.get("file_uuid") or "").strip()
        if not file_uuid:
            continue
        # Bytes are saved under server file_uuid; client_id→file_uuid alias is metadata only.
        upload_id = file_uuid
        meta = _lookup_file_meta(file_uuid)
        name = _preferred_filename(
            str(ref.get("file_name") or ""),
            str(meta.get("file_name") or ""),
            _fum.get_registered_upload_filename(file_uuid),
        )
        mime = str(ref.get("mime_type") or meta.get("mime_type") or "").strip()
        try:
            size = int(ref.get("size_bytes") or meta.get("size_bytes") or 0)
        except Exception:
            size = 0

        url = str(ref.get("url") or meta.get("url") or "").strip()
        if url:
            _store_file_meta(file_uuid, name, size, mime, url=url)
            _ensure_saved_from_s3_url(req, upload_id, name, mime, url)

        out.append(_finalize_policy_attachment(upload_id, name, mime, size))

    return out if out else [{"id": "", "size": 0, "name": "", "mime_type": ""}]


def _finalize_policy_attachment(
    upload_id: str,
    req_name: str,
    req_mime: str,
    req_size: int = 0,
) -> Dict[str, Any]:
    uid = str(upload_id or "").strip()
    meta = _lookup_file_meta(uid)
    name = _preferred_filename(
        str(req_name or "").strip(),
        str(meta.get("file_name") or ""),
        _fum.get_registered_upload_filename(uid),
    )
    mime = str(req_mime or "").strip() or str(meta.get("mime_type") or "").strip()

    saved = (
        _fum.get_saved_attachment(uid, fallback_name=name, fallback_mime=mime) if uid else None
    )
    if not saved and uid:
        saved = _fum.wait_for_saved_attachment(uid, fallback_name=name, fallback_mime=mime)
    if saved:
        size = int(req_size or 0) if req_size > 0 else int(saved.get("size") or 0)
        display_name = name or str(saved.get("name") or "")
        if display_name and (
            _ATTACHMENT_UUID_RE.match(display_name) or _is_s3_slug_filename(display_name)
        ):
            cached_name = _preferred_filename(
                str(meta.get("file_name") or ""),
                _fum.get_registered_upload_filename(uid),
            )
            if cached_name and not _is_s3_slug_filename(cached_name):
                display_name = cached_name
        return {
            "id": str(saved.get("id") or ""),
            "size": size,
            "name": display_name,
            "mime_type": mime or str(saved.get("mime_type") or ""),
        }

    if uid:
        expected = _fum.expected_saved_path(uid)
        logger.warning(
            "perplexity policy attachment not on disk yet: uid=%s expected=%s",
            uid[:64],
            expected[:120],
        )
        # Do not return expected path as id — that fakes on_disk and confuses
        # file_block_filter when S3 tee never landed (Deep divert miss).
        return {
            "id": "",
            "size": int(req_size or 0),
            "name": name,
            "mime_type": mime,
        }

    return {
        "id": "",
        "size": int(req_size or 0),
        "name": name,
        "mime_type": mime,
    }


# Public entry points share file_upload_manager.UPLOAD_STATE_LOCK so the
# policy loop can run normalize/persist on a worker without racing extract.
_fum.bind_upload_state_lock(globals())
