"""
Grok file upload and policy attachment assembly.

Flow (upload v2):
  Small file:
    POST /http/upload-file-v2/direct  (multipart file bytes)
    response (legacy): {uploadId, fileMetadata:{fileMetadataId, fileName, ...}}
    response (2026+):  {"status": 1, "error": null}  — asset id arrives later via
      GET /rest/assets/{assetId}  or chat fileAttachments / WS file_mention
  Large file: init → GCS PUT → complete → status (unchanged)
  chat (legacy REST): {message, fileAttachments: ["<fileMetadataId>"]}
  chat (2026+): WebSocket /ws/mgw/ conversation.item.create with input_chunks /
    file_attachment_ids; HTTP fallback load-responses embeds human message +
    fileAttachmentsMetadata

Adapter owns upload + attachment resolution; file_upload_manager is storage only.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple, Deque

import file_upload_manager as _fum

logger = logging.getLogger(__name__)

POLICY_ATTACHMENTS_FINAL = True

_GROK_FILE_METADATA_INDEX: Dict[str, Dict[str, Any]] = {}
_GROK_FILE_METADATA_ORDER: deque[str] = deque()
_GROK_FILE_METADATA_LIMIT = 512

# Grok often uploads (direct v2) then reconnects WS; the next response.create
# may omit file_mention. Keep recently saved/promoted ids for the same cookie
# so chat policy still sees the file — but only briefly, and consume-once so
# later text-only turns do not keep re-attaching stale files into the UI.
_RECENT_CHAT_FILES_BY_SCOPE: Dict[str, Deque[Tuple[str, float]]] = {}
_RECENT_CHAT_FILES_LIMIT = 8
_RECENT_CHAT_FILES_TTL_SEC = 90.0

_V2_INIT_FILENAME_BY_REQUEST: Dict[str, str] = {}
_V2_INIT_SIZE_BY_REQUEST: Dict[str, int] = {}
_V2_INIT_FILENAME_BY_SCOPE: Dict[str, str] = {}
_V2_INIT_SIZE_BY_SCOPE: Dict[str, int] = {}
_V2_INIT_SIZE_BY_ASSET: Dict[str, int] = {}
_V2_INIT_MAP_LIMIT = 2048

_ASSET_ID_PATH_RE = re.compile(
    r"/xai-enterprise-api-mc-assets-prod/users/[^/]+/([0-9a-fA-F-]{36})/content",
    re.IGNORECASE,
)
_REST_ASSET_PATH_RE = re.compile(
    r"^/rest/assets/([0-9a-fA-F-]{36})$",
    re.IGNORECASE,
)
_ASSETS_CDN_CONTENT_RE = re.compile(
    r"/users/[^/]+/([0-9a-fA-F-]{36})/content",
    re.IGNORECASE,
)
_ASSETS_CDN_USER_RE = re.compile(
    r"/users/([0-9a-fA-F-]{36})/",
    re.IGNORECASE,
)
_WS_UID_QUERY_RE = re.compile(
    r"[?&]uid=([0-9a-fA-F-]{36})",
    re.IGNORECASE,
)

_FILE_METADATA_ID_RE = re.compile(
    r'"fileMetadataId"\s*:\s*"([0-9a-fA-F-]{36})"',
    re.IGNORECASE,
)
_FILE_NAME_JSON_RE = re.compile(
    r'"fileName"\s*:\s*"((?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)
_FILE_MIME_RE = re.compile(
    r'"fileMimeType"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
_SIZE_BYTES_RE = re.compile(
    r'"sizeBytes"\s*:\s*(\d+)',
    re.IGNORECASE,
)
_ATTACHMENT_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _safe_utf8(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _grok_meta(req: Dict[str, Any]) -> Dict[str, Any]:
    meta = req.get("meta")
    return meta if isinstance(meta, dict) else {}


def _grok_meta_host(req: Dict[str, Any]) -> str:
    return str(_grok_meta(req).get("host") or "").lower()


def _grok_meta_path(req: Dict[str, Any]) -> str:
    return str(_grok_meta(req).get("path") or "")


def _grok_path_base(req: Dict[str, Any]) -> str:
    return _grok_meta_path(req).split("?", 1)[0].rstrip("/") or "/"


def _is_grok_direct_v2_path(path: str) -> bool:
    return "/http/upload-file-v2/direct" in str(path or "")


def _is_grok_v2_init_path(path: str) -> bool:
    return str(path or "").endswith("/rest/app-chat/upload-file-v2/init")


def _is_grok_v2_complete_path(path: str) -> bool:
    return str(path or "").endswith("/rest/app-chat/upload-file-v2/complete")


def _is_grok_v2_status_path(path: str) -> bool:
    return "/rest/app-chat/upload-file-v2/status" in str(path or "")


def _is_grok_rest_asset_path(path: str) -> bool:
    return bool(_REST_ASSET_PATH_RE.match(str(path or "").split("?", 1)[0].rstrip("/") or "/"))


def _is_grok_assets_cdn_content(host: str, path: str) -> bool:
    h = str(host or "").lower()
    p = str(path or "")
    if "assets.grok.com" not in h:
        return False
    return bool(_ASSETS_CDN_CONTENT_RE.search(p))


def _extract_rest_asset_id(path: str) -> str:
    m = _REST_ASSET_PATH_RE.match(str(path or "").split("?", 1)[0].rstrip("/") or "/")
    return m.group(1) if m else ""


def _extract_cdn_asset_id(path: str) -> str:
    m = _ASSETS_CDN_CONTENT_RE.search(str(path or ""))
    return m.group(1) if m else ""


def _is_grok_gcs_upload(host: str, path: str) -> bool:
    h = str(host or "").lower()
    p = str(path or "").lower()
    return "storage.googleapis.com" in h and "xai-enterprise-api-mc-assets-prod" in p


def _is_multipart_upload(req: Dict[str, Any], payload: bytes) -> bool:
    content_type = str(req.get("content_type") or "")
    if "multipart/form-data" in content_type.lower():
        return True
    return bool(payload[:2] == b"--" and b"Content-Disposition" in payload[:512])


def _remember_v2_init_filename(request_id: str, filename: str, scope: str = "") -> None:
    if not filename:
        return
    if request_id:
        _V2_INIT_FILENAME_BY_REQUEST[request_id] = filename
        while len(_V2_INIT_FILENAME_BY_REQUEST) > _V2_INIT_MAP_LIMIT:
            oldest = next(iter(_V2_INIT_FILENAME_BY_REQUEST))
            _V2_INIT_FILENAME_BY_REQUEST.pop(oldest, None)
    # Inline file_upload_policy_ tees request with request_id-up-… but response
    # keeps the stream request_id — also key by cookie scope for promote.
    if scope:
        _V2_INIT_FILENAME_BY_SCOPE[scope] = filename
        while len(_V2_INIT_FILENAME_BY_SCOPE) > _V2_INIT_MAP_LIMIT:
            oldest = next(iter(_V2_INIT_FILENAME_BY_SCOPE))
            _V2_INIT_FILENAME_BY_SCOPE.pop(oldest, None)


def _remember_v2_init_size(request_id: str, size_bytes: int, scope: str = "") -> None:
    if size_bytes <= 0:
        return
    if request_id:
        _V2_INIT_SIZE_BY_REQUEST[request_id] = int(size_bytes)
        while len(_V2_INIT_SIZE_BY_REQUEST) > _V2_INIT_MAP_LIMIT:
            oldest = next(iter(_V2_INIT_SIZE_BY_REQUEST))
            _V2_INIT_SIZE_BY_REQUEST.pop(oldest, None)
    if scope:
        _V2_INIT_SIZE_BY_SCOPE[scope] = int(size_bytes)
        while len(_V2_INIT_SIZE_BY_SCOPE) > _V2_INIT_MAP_LIMIT:
            oldest = next(iter(_V2_INIT_SIZE_BY_SCOPE))
            _V2_INIT_SIZE_BY_SCOPE.pop(oldest, None)


def _pop_v2_init_filename(request_id: str, scope: str = "") -> str:
    filename = ""
    if request_id:
        filename = _V2_INIT_FILENAME_BY_REQUEST.pop(request_id, "") or ""
    if not filename and scope:
        filename = _V2_INIT_FILENAME_BY_SCOPE.pop(scope, "") or ""
    elif scope:
        _V2_INIT_FILENAME_BY_SCOPE.pop(scope, None)
    return filename


def _pop_v2_init_size(request_id: str, scope: str = "") -> int:
    size_bytes = 0
    if request_id:
        size_bytes = int(_V2_INIT_SIZE_BY_REQUEST.pop(request_id, 0) or 0)
    if size_bytes <= 0 and scope:
        size_bytes = int(_V2_INIT_SIZE_BY_SCOPE.pop(scope, 0) or 0)
    elif scope:
        _V2_INIT_SIZE_BY_SCOPE.pop(scope, None)
    return size_bytes


def _remember_v2_asset_size(asset_id: str, size_bytes: int) -> None:
    if not asset_id or size_bytes <= 0:
        return
    _V2_INIT_SIZE_BY_ASSET[str(asset_id).strip()] = int(size_bytes)
    while len(_V2_INIT_SIZE_BY_ASSET) > _V2_INIT_MAP_LIMIT:
        oldest = next(iter(_V2_INIT_SIZE_BY_ASSET))
        _V2_INIT_SIZE_BY_ASSET.pop(oldest, None)


def _parse_size_bytes(raw: Any) -> int:
    try:
        return max(0, int(str(raw or "").strip()))
    except Exception:
        return 0


def _request_content_length(req: Dict[str, Any]) -> int:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    headers = meta.get("request_headers")
    if not isinstance(headers, dict):
        return 0
    for key in ("content-length", "Content-Length"):
        if key in headers:
            return _parse_size_bytes(headers.get(key))
    return 0


def _policy_for_gcs_put(
    req: Dict[str, Any],
    asset_id: str,
    payload_len: int = 0,
) -> Dict[str, Any]:
    policy = _fum.collection_policy_from_request(req)
    configured = int(policy.get("maxsize") or 0)
    declared = int(_V2_INIT_SIZE_BY_ASSET.get(str(asset_id or "").strip(), 0) or 0)
    if declared <= 0:
        declared = _request_content_length(req)
    if declared <= 0 and payload_len > 0:
        declared = int(payload_len)
    if declared <= 0:
        return policy
    effective = min(max(configured, declared), _fum.MAX_FILE_CONTROL_MAXSIZE)
    if effective <= configured:
        return policy
    merged = dict(policy)
    merged["maxsize"] = effective
    return merged


def _json_obj(payload: bytes) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _extract_v2_init_filename(payload: bytes) -> str:
    obj = _json_obj(payload)
    if not obj:
        return ""
    return str(obj.get("fileName") or obj.get("file_name") or "").strip()


def _extract_v2_init_response_ids(payload: bytes) -> Tuple[str, str]:
    obj = _json_obj(payload)
    if not obj:
        return "", ""
    upload_id = str(obj.get("uploadId") or obj.get("upload_id") or "").strip()
    asset_id = str(obj.get("assetId") or obj.get("asset_id") or "").strip()
    return upload_id, asset_id


def _extract_gcs_asset_id(path: str) -> str:
    m = _ASSET_ID_PATH_RE.search(str(path or ""))
    return m.group(1) if m else ""


def _handle_grok_direct_multipart(
    req: Dict[str, Any],
    payload: bytes,
    request_id: str,
) -> Dict[str, Any]:
    path = _grok_path_base(req)
    if not _is_multipart_upload(req, payload):
        return _fum.build_upload_result(handled=True)

    content_type = str(req.get("content_type") or "")
    parts = _fum.parse_multipart(payload, content_type)
    files: List[Tuple[str, bytes]] = []
    for part in parts:
        body = part.get("body")
        if not isinstance(body, (bytes, bytearray)) or not body:
            continue
        # filename-less parts are form fields (e.g. source_conversation_id), not files.
        fname = str(part.get("filename") or "").strip()
        if not fname:
            continue
        files.append((fname, bytes(body)))

    if not files:
        return _fum.build_upload_result(handled=True)

    policy = _fum.collection_policy_from_request(req)
    try:
        result = _fum.create_pending_upload_batch_from_files(
            request_id,
            files,
            "grok",
            policy,
        )
    except Exception:
        logger.exception("grok direct v2 multipart save failed")
        return _fum.build_upload_result(handled=True)

    upload_ids = [
        str(x.get("upload_id") or "").strip()
        for x in (result.get("saved_files") or [])
        if isinstance(x, dict)
    ]
    if upload_ids:
        _fum.remember_pending_uploads_for_scope(_grok_upload_scope(req), upload_ids)
        for uid in upload_ids:
            remember_recent_chat_file(req, uid)

    if not result.get("handled"):
        return _fum.build_upload_result(
            handled=True,
            skipped=bool(result.get("skipped")),
            skip_reason=str(result.get("skip_reason") or ""),
        )
    return result


def _handle_grok_v2_init_request(
    req: Dict[str, Any],
    payload: bytes,
    request_id: str,
) -> Dict[str, Any]:
    obj = _json_obj(payload)
    filename = _extract_v2_init_filename(payload)
    size_bytes = _parse_size_bytes(obj.get("sizeBytes") if obj else 0)
    scope = _grok_upload_scope(req)
    _remember_v2_init_filename(request_id, filename, scope=scope)
    _remember_v2_init_size(request_id, size_bytes, scope=scope)
    return _fum.build_upload_result(upload_filename=filename, handled=True)


def _handle_grok_v2_init_response(
    req: Dict[str, Any],
    payload: bytes,
    request_id: str,
) -> Dict[str, Any]:
    upload_id, asset_id = _extract_v2_init_response_ids(payload)
    scope = _grok_upload_scope(req)
    filename = _pop_v2_init_filename(request_id, scope=scope)
    declared_size = _pop_v2_init_size(request_id, scope=scope)
    policy = _fum.collection_policy_from_request(req)
    if asset_id and filename:
        if not _fum.register_upload_mapping(asset_id, filename, policy):
            return _fum.build_skipped_upload_result(asset_id, filename, "collection_policy")
    if upload_id and asset_id:
        _fum.register_upload_server_alias(upload_id, asset_id)
    if asset_id and declared_size:
        _remember_v2_asset_size(asset_id, declared_size)
    return _fum.build_upload_result(
        upload_id=asset_id or upload_id,
        upload_filename=filename,
        handled=True,
    )


def _handle_grok_gcs_put(req: Dict[str, Any], payload: bytes) -> Dict[str, Any]:
    path = _grok_meta_path(req)
    asset_id = _extract_gcs_asset_id(path)
    if not asset_id:
        return _fum.build_upload_result(handled=True)

    payload_len = len(payload) if payload else 0
    policy = _policy_for_gcs_put(req, asset_id, payload_len=payload_len)
    upload_filename = _fum.get_registered_upload_filename(asset_id) or asset_id[:48]
    saved_path = ""
    if payload:
        try:
            saved_path = _fum.append_upload_payload(asset_id, payload, policy)
            if not saved_path:
                logger.info(
                    "grok GCS upload skipped: asset_id=%s payload=%d maxsize=%d declared=%d",
                    asset_id,
                    len(payload),
                    int(policy.get("maxsize") or 0),
                    int(_V2_INIT_SIZE_BY_ASSET.get(asset_id, 0) or 0),
                )
                return _fum.build_skipped_upload_result(
                    asset_id,
                    upload_filename,
                    "collection_policy",
                )
        except Exception:
            logger.exception("grok GCS upload save failed: asset_id=%s", asset_id)
            return _fum.build_upload_result(
                upload_id=asset_id,
                upload_filename=upload_filename,
                handled=True,
            )

    return _fum.build_upload_result(
        upload_id=asset_id,
        upload_filename=upload_filename,
        saved_path=saved_path,
        handled=True,
    )


def _decode_json_string(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        decoded = json.loads(f'"{text}"')
        if isinstance(decoded, str):
            return decoded
    except Exception:
        pass
    return text.replace("\\/", "/")


def _extract_x_user_from_cookie(cookie_str: str) -> Optional[str]:
    if not cookie_str or not isinstance(cookie_str, str):
        return None
    cookie_str = cookie_str.strip()
    for name in ("x-user", "x-userid"):
        m = re.search(r"%s=([^;\s]+)" % re.escape(name), cookie_str, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _grok_userid_from_req(req: Dict[str, Any]) -> str:
    """Resolve Grok account id across HTTP upload, CDN, and /ws/mgw scopes.

    Mirror often strips cookies on assets.grok.com while chat WS carries
    ``?uid=<uuid>``. Without unifying these, recent-file peek misses the
    CDN-saved asset and policy attachments stay empty.
    """
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    x_userid = str(meta.get("x_userid") or "").strip()
    if x_userid:
        return x_userid
    cookie_str = str(meta.get("cookie") or "").strip()
    x_userid = _extract_x_user_from_cookie(cookie_str) or ""
    if x_userid:
        return x_userid
    path = str(meta.get("path") or "")
    m = _ASSETS_CDN_USER_RE.search(path)
    if m:
        return m.group(1)
    m = _WS_UID_QUERY_RE.search(path)
    if m:
        return m.group(1)
    return ""


def _grok_upload_scope(req: Dict[str, Any]) -> str:
    x_userid = _grok_userid_from_req(req)
    return f"grok:{x_userid}" if x_userid else "grok:orphan"


def _remember_recent_chat_file_for_scope(scope: str, upload_id: str) -> None:
    uid = str(upload_id or "").strip()
    if not uid or not scope:
        return
    now = time.time()
    q = _RECENT_CHAT_FILES_BY_SCOPE.setdefault(scope, deque())
    # Refresh timestamp if already present; do not duplicate.
    kept: Deque[Tuple[str, float]] = deque()
    for existing_uid, _ts in q:
        if existing_uid == uid:
            continue
        kept.append((existing_uid, _ts))
    kept.append((uid, now))
    while len(kept) > _RECENT_CHAT_FILES_LIMIT:
        kept.popleft()
    _RECENT_CHAT_FILES_BY_SCOPE[scope] = kept
    logger.info("grok remember recent chat file scope=%s id=%s n=%d", scope, uid, len(kept))


def remember_recent_chat_file(req: Dict[str, Any], upload_id: str) -> None:
    uid = str(upload_id or "").strip()
    if not uid:
        return
    # Only the resolved user scope. Never mirror into orphan — that made every
    # later text-only turn inherit the previous file into UI/policy.
    _remember_recent_chat_file_for_scope(_grok_upload_scope(req), uid)


def _prune_recent_chat_files(scope: str) -> Deque[Tuple[str, float]]:
    q = _RECENT_CHAT_FILES_BY_SCOPE.get(scope)
    if not q:
        empty: Deque[Tuple[str, float]] = deque()
        return empty
    now = time.time()
    kept: Deque[Tuple[str, float]] = deque()
    for uid, ts in q:
        if now - float(ts or 0.0) <= _RECENT_CHAT_FILES_TTL_SEC and str(uid or "").strip():
            kept.append((str(uid).strip(), float(ts)))
    if kept:
        _RECENT_CHAT_FILES_BY_SCOPE[scope] = kept
    else:
        _RECENT_CHAT_FILES_BY_SCOPE.pop(scope, None)
    return kept


def peek_recent_chat_files(req: Dict[str, Any]) -> List[str]:
    """Return recent upload ids for this user scope only (TTL-bounded).

    Does not fall back to ``grok:orphan`` — cross-scope peek polluted UI rows
    with stale attachments on unrelated prompts.
    """
    scope = _grok_upload_scope(req)
    q = _prune_recent_chat_files(scope)
    return [uid for uid, _ts in q]


def consume_recent_chat_files(req: Dict[str, Any], upload_ids: List[str]) -> None:
    """Drop ids after a real chat turn binds them so they are not re-attached."""
    wanted = {str(x or "").strip() for x in (upload_ids or []) if str(x or "").strip()}
    if not wanted:
        return
    scope = _grok_upload_scope(req)
    q = _prune_recent_chat_files(scope)
    if not q:
        return
    kept: Deque[Tuple[str, float]] = deque(
        (uid, ts) for uid, ts in q if uid not in wanted
    )
    if kept:
        _RECENT_CHAT_FILES_BY_SCOPE[scope] = kept
    else:
        _RECENT_CHAT_FILES_BY_SCOPE.pop(scope, None)
    logger.info(
        "grok consume recent chat files scope=%s removed=%s left=%d",
        scope,
        sorted(wanted),
        len(kept),
    )

def _direct_upload_payload(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    du = obj.get("directUpload")
    if isinstance(du, dict):
        return du
    return None


def _is_grok_direct_upload_request(obj: Dict[str, Any]) -> bool:
    du = _direct_upload_payload(obj)
    if not du:
        return False
    return bool(str(du.get("fileName") or "").strip() and du.get("fileData"))


def _is_grok_upload_request(obj: Dict[str, Any]) -> bool:
    if _is_grok_direct_upload_request(obj):
        return True
    return bool(str(obj.get("fileName") or "").strip() and obj.get("content"))


def _decode_direct_upload_file_data(raw: Any) -> bytes:
    if isinstance(raw, bytes):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return b""
    try:
        return base64.b64decode(raw)
    except Exception:
        return raw.encode("utf-8", errors="replace")


def _extract_upload_file(obj: Dict[str, Any]) -> Optional[Tuple[str, bytes, str]]:
    du = _direct_upload_payload(obj)
    if du:
        fname = str(du.get("fileName") or "").strip()
        file_data = _decode_direct_upload_file_data(du.get("fileData"))
        mime = str(du.get("fileMimeType") or du.get("mimeType") or "").strip()
        if fname and file_data:
            return fname, file_data, mime
        return None

    fname = str(obj.get("fileName") or "").strip()
    content = obj.get("content")
    if not fname or not content:
        return None
    if isinstance(content, str):
        try:
            file_data = base64.b64decode(content)
        except Exception:
            file_data = content.encode("utf-8", errors="replace")
    elif isinstance(content, bytes):
        file_data = content
    else:
        return None
    if not file_data:
        return None
    mime = str(obj.get("fileMimeType") or obj.get("mimeType") or "").strip()
    return fname, file_data, mime


def parse_direct_upload_message(text: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Split chat message text from an embedded directUpload JSON blob."""
    raw = str(text or "").strip()
    if not raw:
        return "", None
    if not raw.startswith("{"):
        return raw, None
    try:
        parsed = json.loads(raw)
    except Exception:
        return raw, None
    if not isinstance(parsed, dict):
        return raw, None
    du = _direct_upload_payload(parsed)
    if du:
        return "", du
    return raw, None


def unwrap_masked_direct_upload_prompt(replacement_text: str, original_message: str = "") -> str:
    """Policy may return masked directUpload JSON; show plain text in the chat message only."""
    rep = str(replacement_text or "").strip()
    if not rep:
        return rep
    try:
        obj = json.loads(rep)
    except Exception:
        return rep
    if not isinstance(obj, dict):
        return rep
    du = _direct_upload_payload(obj)
    if not du:
        return rep
    user_text, _ = parse_direct_upload_message(original_message)
    if user_text.strip():
        return user_text.strip()
    masked = str(du.get("fileData") or "").strip()
    if masked.startswith("{") and masked.endswith("}"):
        return ""
    return masked


def _is_grok_upload_response(payload: bytes) -> bool:
    if not payload:
        return False
    text = _safe_utf8(payload) if isinstance(payload, (bytes, bytearray)) else str(payload)
    if _FILE_METADATA_ID_RE.search(text):
        return True
    return '"uploadId"' in text and '"fileMetadata"' in text


def _parse_upload_response_items(payload: bytes) -> List[Dict[str, Any]]:
    if not payload:
        return []
    text = _safe_utf8(payload)
    items: List[Dict[str, Any]] = []

    try:
        obj = json.loads(text)
    except Exception:
        obj = None

    def add_item(row: Dict[str, Any]) -> None:
        uid = str(
            row.get("fileMetadataId")
            or row.get("file_metadata_id")
            or row.get("assetId")
            or row.get("asset_id")
            or row.get("id")
            or ""
        ).strip()
        if not uid or not _ATTACHMENT_UUID_RE.match(uid):
            return
        name = str(
            row.get("fileName")
            or row.get("file_name")
            or row.get("name")
            or ""
        ).strip()
        mime = str(
            row.get("fileMimeType")
            or row.get("mime_type")
            or row.get("mimeType")
            or ""
        ).strip()
        try:
            size = int(
                row.get("sizeBytes")
                or row.get("size_bytes")
                or row.get("size")
                or 0
            )
        except Exception:
            size = 0
        items.append(
            {
                "file_metadata_id": uid,
                "file_name": name,
                "mime_type": mime,
                "size_bytes": size,
            }
        )

    if isinstance(obj, dict):
        add_item(obj)
        nested_meta = obj.get("fileMetadata")
        if isinstance(nested_meta, dict):
            add_item(nested_meta)
        for key in ("file", "files", "uploaded_file", "uploaded_files", "assets"):
            nested = obj.get(key)
            if isinstance(nested, dict):
                add_item(nested)
            elif isinstance(nested, list):
                for row in nested:
                    if isinstance(row, dict):
                        add_item(row)
        # load-responses / chat history
        responses = obj.get("responses")
        if isinstance(responses, list):
            for row in responses:
                if not isinstance(row, dict):
                    continue
                meta_rows = row.get("fileAttachmentsMetadata")
                if isinstance(meta_rows, list):
                    for mrow in meta_rows:
                        if isinstance(mrow, dict):
                            add_item(mrow)
        meta_top = obj.get("fileAttachmentsMetadata")
        if isinstance(meta_top, list):
            for mrow in meta_top:
                if isinstance(mrow, dict):
                    add_item(mrow)

    if not items:
        ids = _FILE_METADATA_ID_RE.findall(text)
        names = [_decode_json_string(n) for n in _FILE_NAME_JSON_RE.findall(text)]
        mimes = _FILE_MIME_RE.findall(text)
        sizes = [int(x) for x in _SIZE_BYTES_RE.findall(text)]
        for idx, uid in enumerate(ids):
            name = names[idx] if idx < len(names) else ""
            mime = mimes[idx] if idx < len(mimes) else ""
            size = sizes[idx] if idx < len(sizes) else 0
            items.append(
                {
                    "file_metadata_id": uid,
                    "file_name": name,
                    "mime_type": mime,
                    "size_bytes": size,
                }
            )
    return items


def _remember_grok_file_binding(
    file_metadata_id: str,
    file_name: str = "",
    size_bytes: int = 0,
    mime_type: str = "",
) -> None:
    uid = str(file_metadata_id or "").strip().lower()
    if not uid:
        return
    if uid not in _GROK_FILE_METADATA_INDEX:
        _GROK_FILE_METADATA_ORDER.append(uid)
        while len(_GROK_FILE_METADATA_ORDER) > _GROK_FILE_METADATA_LIMIT:
            oldest = _GROK_FILE_METADATA_ORDER.popleft()
            _GROK_FILE_METADATA_INDEX.pop(oldest, None)
    _GROK_FILE_METADATA_INDEX[uid] = {
        "upload_id": file_metadata_id,
        "file_name": str(file_name or "").strip(),
        "size_bytes": int(size_bytes or 0),
        "mime_type": str(mime_type or "").strip(),
    }


def _resolve_grok_file_ref(file_metadata_id: str) -> Optional[Dict[str, Any]]:
    key = str(file_metadata_id or "").strip().lower()
    if not key:
        return None
    cached = _GROK_FILE_METADATA_INDEX.get(key)
    if cached:
        return dict(cached)
    if _fum.get_saved_attachment(file_metadata_id) or _fum.get_registered_upload_filename(file_metadata_id):
        return {
            "upload_id": file_metadata_id,
            "file_name": _fum.get_registered_upload_filename(file_metadata_id),
            "size_bytes": 0,
            "mime_type": "",
        }
    legacy = _fum.lookup_upload_id_by_registered_server_uuid(file_metadata_id)
    if legacy:
        return {
            "upload_id": legacy,
            "file_name": _fum.get_registered_upload_filename(legacy),
            "size_bytes": 0,
            "mime_type": "",
        }
    return None


def _promote_uploads_to_file_metadata_id(
    items: List[Dict[str, Any]],
    saved_files: List[Dict[str, Any]],
    policy: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    promoted: List[Dict[str, Any]] = []
    for idx, row in enumerate(saved_files):
        if not isinstance(row, dict) or idx >= len(items):
            if isinstance(row, dict):
                promoted.append(row)
            continue
        temp_upload_id = str(row.get("upload_id") or "").strip()
        item = items[idx]
        file_metadata_id = str(item.get("file_metadata_id") or "").strip()
        file_name = str(
            item.get("file_name")
            or _fum.get_registered_upload_filename(temp_upload_id)
            or ""
        )
        if not temp_upload_id or not file_metadata_id:
            promoted.append(row)
            continue

        upload_id = file_metadata_id
        if temp_upload_id != file_metadata_id:
            if not _fum.rekey_upload_id(temp_upload_id, file_metadata_id):
                upload_id = temp_upload_id
            else:
                logger.info(
                    "grok upload promoted: %s -> %s name=%s",
                    temp_upload_id,
                    file_metadata_id,
                    file_name,
                )

        if file_name:
            _fum.register_upload_mapping(upload_id, file_name, policy)

        saved = _fum.get_saved_attachment(upload_id, fallback_name=file_name)
        saved_path = str((saved or {}).get("id") or row.get("saved_path") or "")

        promoted.append(
            {
                "upload_id": upload_id,
                "filename": file_name or upload_id,
                "saved_path": saved_path,
            }
        )
        _remember_grok_file_binding(
            file_metadata_id,
            file_name,
            int(item.get("size_bytes") or 0),
            str(item.get("mime_type") or ""),
        )
    return promoted


def _pending_grok_ids_on_disk() -> List[str]:
    """Process-local pending queue is lost on engine restart; tmpfs grok-* files remain."""
    root = os.path.abspath(
        os.environ.get("AIVAX_ATTACH_TMPFS") or "/run/aivax/attachments"
    )
    try:
        entries: List[Tuple[float, str]] = []
        for name in os.listdir(root):
            if not name.startswith("grok-"):
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            entries.append((os.path.getmtime(path), name))
        entries.sort()
        return [name for _, name in entries]
    except OSError:
        return []


def _grok_terminal_error(payload: bytes) -> str:
    obj = _json_obj(payload)
    if not obj:
        return ""
    return str(obj.get("terminalError") or "").strip()


def _saved_attachment_path(upload_id: str) -> str:
    saved = _fum.get_saved_attachment(upload_id)
    if not saved:
        return ""
    return str(saved.get("id") or "").strip()


def _binding_fields(
    asset_id: str,
    file_name: str = "",
    mime_type: str = "",
    size_bytes: int = 0,
) -> Tuple[str, str, int]:
    """Keep a name already stored for this asset when the caller has none.

    An empty promote (chat turn, no fileName) must not replace 계좌번호.png
    with whatever leftover temp file is still on disk.
    """
    cached = _GROK_FILE_METADATA_INDEX.get(str(asset_id or "").strip().lower()) or {}
    # Registered filename wins over the in-memory binding. A refused upload used
    # to overwrite that binding with its own name while the registry still held
    # the original.
    name = (
        str(file_name or "").strip()
        or _fum.get_registered_upload_filename(asset_id)
        or str(cached.get("file_name") or "").strip()
        or asset_id
    )
    mime = str(mime_type or "").strip() or str(cached.get("mime_type") or "").strip()
    try:
        size = int(size_bytes or 0) or int(cached.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size = 0
    return name, mime, size


def _finish_asset_binding(
    req: Dict[str, Any],
    asset_id: str,
    *,
    policy: Optional[Dict[str, Any]],
    file_name: str = "",
    mime_type: str = "",
    size_bytes: int = 0,
    publish_name: bool = False,
) -> Dict[str, Any]:
    name, mime, size = _binding_fields(asset_id, file_name, mime_type, size_bytes)
    if publish_name and str(file_name or "").strip():
        _fum.register_upload_mapping(
            asset_id, name, policy, allow_unknown_extension=True
        )
    _remember_grok_file_binding(asset_id, name, size, mime)
    remember_recent_chat_file(req, asset_id)
    saved = _fum.get_saved_attachment(asset_id, fallback_name=name, fallback_mime=mime)
    return _fum.build_upload_result(
        upload_id=asset_id,
        upload_filename=name,
        saved_path=str((saved or {}).get("id") or ""),
        handled=True,
    )


def _next_pending_temp(scope: str) -> str:
    """Next grok-* that still has bytes. Stale ids are dropped.

    Scope-queue leftovers stay queued. Disk ids are not copied into the queue;
    the files remain and the next empty-queue call scans again.
    """
    queued = _fum.pop_pending_uploads_for_scope(scope)
    from_scope = bool(queued)
    pending = queued or _pending_grok_ids_on_disk()
    while pending:
        candidate = str(pending[0] or "").strip()
        tail = [str(item).strip() for item in pending[1:] if str(item).strip()]
        if candidate and _saved_attachment_path(candidate):
            if from_scope and tail:
                _fum.remember_pending_uploads_for_scope(scope, tail)
            return candidate
        pending = tail
    return ""


def _discard_rejected_direct_upload(
    req: Dict[str, Any],
    request_id: str,
    reason: str,
) -> None:
    """Drop a direct upload Grok refused, so its temp file cannot be reused."""
    ids = [str(item).strip() for item in _fum._claim_pending_upload_ids(request_id)]
    ids = [item for item in ids if item]
    scope = _grok_upload_scope(req)
    if ids:
        _fum.forget_pending_uploads_from_scope(scope, ids)
    for upload_id in ids:
        consume_recent_chat_files(req, [upload_id])
        _GROK_FILE_METADATA_INDEX.pop(upload_id.lower(), None)
        _fum.discard_upload(upload_id)
    logger.info(
        "grok upload rejected request_id=%s discarded=%s reason=%s",
        request_id,
        ids,
        reason[:180],
    )


def _promote_one_pending_to_asset(
    req: Dict[str, Any],
    asset_id: str,
    file_name: str = "",
    mime_type: str = "",
    size_bytes: int = 0,
) -> Optional[Dict[str, Any]]:
    """When direct upload only returns status:1, bind next pending temp id to assetId.

    An asset that already has its own bytes is left alone. A refused upload
    stays as grok-* on disk; rebinding that temp id used to copy its filename
    onto every other attachment in the chat turn.
    """
    uid = str(asset_id or "").strip()
    if not uid or not _ATTACHMENT_UUID_RE.match(uid):
        return None
    policy = _fum.collection_policy_from_request(req)
    if _saved_attachment_path(uid):
        return _finish_asset_binding(
            req,
            uid,
            policy=policy,
            file_name=file_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            publish_name=True,
        )

    scope = _grok_upload_scope(req)
    temp_id = _next_pending_temp(scope)
    if not temp_id:
        # Already saved under asset id, or metadata-only bind.
        # Mirror CDN often lands the bytes under the asset UUID with no pending
        # grok-* temp id (service_gate blocked the multipart request). Still
        # publish the id so the next WS turn can attach it via peek_recent.
        return _finish_asset_binding(
            req,
            uid,
            policy=policy,
            file_name=file_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            publish_name=True,
        )

    upload_id = uid
    if temp_id != uid:
        if not _fum.rekey_upload_id(temp_id, uid):
            # Target is already registered. Do not alias it to this temp file
            # and do not copy the temp filename onto it.
            _fum.remember_pending_uploads_for_scope(scope, [temp_id])
            logger.info(
                "grok asset promote skipped: target=%s already registered temp=%s",
                uid,
                temp_id,
            )
            return _finish_asset_binding(
                req,
                uid,
                policy=policy,
                file_name=file_name,
                mime_type=mime_type,
                size_bytes=size_bytes,
                publish_name=bool(str(file_name or "").strip()),
            )
        logger.info("grok asset promote: %s -> %s name=%s", temp_id, uid, file_name)

    name = file_name or _fum.get_registered_upload_filename(upload_id) or uid
    if name:
        _fum.register_upload_mapping(upload_id, name, policy, allow_unknown_extension=True)
    _remember_grok_file_binding(uid, name, size_bytes, mime_type)
    remember_recent_chat_file(req, uid)
    if upload_id != uid:
        remember_recent_chat_file(req, upload_id)
    saved = _fum.get_saved_attachment(upload_id, fallback_name=name, fallback_mime=mime_type)
    return _fum.build_upload_result(
        upload_id=upload_id,
        upload_filename=name,
        saved_path=str((saved or {}).get("id") or ""),
        handled=True,
    )


def _handle_grok_rest_asset_response(
    req: Dict[str, Any],
    payload: bytes,
) -> Optional[Dict[str, Any]]:
    path_base = _grok_path_base(req)
    asset_from_path = _extract_rest_asset_id(path_base)
    items = _parse_upload_response_items(payload)
    if not items and asset_from_path:
        items = [
            {
                "file_metadata_id": asset_from_path,
                "file_name": "",
                "mime_type": "",
                "size_bytes": 0,
            }
        ]
    if not items:
        return None
    # Prefer explicit asset metadata rows; promote FIFO one-by-one.
    last: Optional[Dict[str, Any]] = None
    for item in items:
        last = _promote_one_pending_to_asset(
            req,
            str(item.get("file_metadata_id") or ""),
            file_name=str(item.get("file_name") or ""),
            mime_type=str(item.get("mime_type") or ""),
            size_bytes=int(item.get("size_bytes") or 0),
        )
    return last or _fum.build_upload_result(handled=True)


def _handle_grok_cdn_asset_response(
    req: Dict[str, Any],
    payload: bytes,
) -> Optional[Dict[str, Any]]:
    """assets.grok.com/.../content response may carry file bytes after direct upload."""
    asset_id = _extract_cdn_asset_id(_grok_meta_path(req))
    if not asset_id:
        return None
    policy = _fum.collection_policy_from_request(req)
    name = _fum.get_registered_upload_filename(asset_id) or asset_id
    if payload and len(payload) > 16:
        try:
            # Bind any pending multipart temp id before writing the CDN object so
            # rekey cannot clobber the just-saved complete bytes.
            _promote_one_pending_to_asset(req, asset_id, file_name=name)
            # This endpoint returns the complete object. The direct-upload body
            # may already be stored under the same asset id, so appending here
            # duplicates/corrupts the file (often producing exactly 2x size).
            saved_path = _fum.replace_upload_payload(asset_id, payload, name, policy)
            if saved_path:
                _remember_grok_file_binding(asset_id, name, len(payload), "")
                remember_recent_chat_file(req, asset_id)
                return _fum.build_upload_result(
                    upload_id=asset_id,
                    upload_filename=name,
                    saved_path=saved_path,
                    saved_files=[
                        {
                            "upload_id": asset_id,
                            "filename": name,
                            "saved_path": saved_path,
                            "size": len(payload),
                        }
                    ],
                    handled=True,
                )
        except Exception:
            logger.exception("grok cdn asset save failed: %s", asset_id)
    return _promote_one_pending_to_asset(req, asset_id, file_name=name)


def _finalize_grok_upload_response(
    req: Dict[str, Any],
    payload: bytes,
    request_id: str,
) -> Optional[Dict[str, Any]]:
    path_base = _grok_path_base(req)
    rejected = _grok_terminal_error(payload)
    if rejected:
        snippet = (payload or b"")[:240].decode("utf-8", errors="replace")
        logger.info(
            "grok upload ack path=%s bytes=%d body=%s",
            path_base,
            len(payload or b""),
            snippet,
        )
        _discard_rejected_direct_upload(req, request_id, rejected)
        return _fum.build_upload_result(handled=True)
    has_meta = _is_grok_upload_response(payload) or b"fileAttachmentsMetadata" in (payload or b"")
    # Direct upload may only ack with {"status":1,"error":null}; keep pending for /rest/assets.
    if not has_meta:
        if _is_grok_direct_v2_path(path_base) or _is_grok_v2_status_path(path_base):
            snippet = (payload or b"")[:240].decode("utf-8", errors="replace")
            logger.info(
                "grok upload ack path=%s bytes=%d body=%s",
                path_base,
                len(payload or b""),
                snippet,
            )
            return _fum.build_upload_result(handled=True)
        if b'"assetId"' in (payload or b"") or b'"asset_id"' in (payload or b""):
            pass  # continue into asset-shaped parse below
        else:
            return None

    policy = _fum.collection_policy_from_request(req)
    scope = _grok_upload_scope(req)
    items = _parse_upload_response_items(payload)
    if not items:
        logger.debug("grok upload response: no file metadata request_id=%s", request_id)
        return _fum.build_upload_result(handled=True)

    filenames = [
        str(it.get("file_name") or it.get("file_metadata_id") or "").strip()
        for it in items
    ]
    saved_files: List[Dict[str, Any]] = []
    result: Dict[str, Any] = _fum.build_upload_result(handled=True)

    if scope:
        result = _fum.finalize_scope_pending_uploads(scope, filenames, policy)
        saved_files = result.get("saved_files") if isinstance(result.get("saved_files"), list) else []
    if not saved_files and request_id:
        result = _fum.finalize_pending_uploads(request_id, filenames, policy)
        saved_files = result.get("saved_files") if isinstance(result.get("saved_files"), list) else []

    if not saved_files and items:
        for item in items:
            uid = str(item.get("file_metadata_id") or "").strip()
            name = str(item.get("file_name") or "").strip()
            if not uid:
                continue
            saved = _fum.get_saved_attachment(uid, fallback_name=name)
            if saved:
                saved_files.append(
                    {
                        "upload_id": uid,
                        "filename": name or str(saved.get("name") or uid),
                        "saved_path": str(saved.get("id") or ""),
                    }
                )

    promoted = _promote_uploads_to_file_metadata_id(items, saved_files, policy)
    if promoted:
        result["saved_files"] = promoted
        if promoted[0].get("upload_id"):
            result["upload_id"] = promoted[0]["upload_id"]
        if promoted[0].get("filename"):
            result["upload_filename"] = promoted[0]["filename"]
        if promoted[0].get("saved_path"):
            result["saved_path"] = promoted[0]["saved_path"]

    if not result.get("handled"):
        result["handled"] = True
    return result


def normalize_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    direction = str(req.get("direction") or "").lower()
    request_id = str(req.get("request_id") or "").strip()
    host = _grok_meta_host(req)
    path = _grok_meta_path(req)
    path_base = _grok_path_base(req)

    if _is_grok_gcs_upload(host, path):
        if direction == "request":
            return _handle_grok_gcs_put(req, payload)
        return _fum.build_upload_result(handled=True)

    if direction == "response":
        if _is_grok_v2_init_path(path_base):
            return _handle_grok_v2_init_response(req, payload, request_id)
        if _is_grok_rest_asset_path(path_base):
            return _handle_grok_rest_asset_response(req, payload)
        if _is_grok_assets_cdn_content(host, path):
            return _handle_grok_cdn_asset_response(req, payload)
        # Chat history (load-responses / .../responses) embeds prior-turn
        # fileAttachmentsMetadata. Treating that as upload-finalize returns
        # handled=True and skips prompt/output extract entirely.
        if path_base.endswith("/load-responses") or re.search(
            r"/rest/app-chat/conversations/[0-9a-fA-F-]+/responses$", path_base
        ):
            return None
        if (
            _is_grok_v2_status_path(path_base)
            or _is_grok_direct_v2_path(path_base)
            or _is_grok_upload_response(payload)
            or b"fileAttachmentsMetadata" in (payload or b"")
            or b'"assetId"' in (payload or b"")
        ):
            return _finalize_grok_upload_response(req, payload, request_id)
        return None

    if direction != "request":
        return None

    if _is_grok_direct_v2_path(path_base):
        return _handle_grok_direct_multipart(req, payload, request_id)

    if "/ws/mgw" in path_base and payload:
        # Promote is done in build_policy_attachments from the already-parsed
        # chat object. Raw ws_frame often is not bare JSON (and may be
        # permessage-deflate); json.loads here used to no-op and leave
        # fileMetadataId unbound. Keep this branch as a no-op side path.
        return None

    if _is_grok_v2_init_path(path_base):
        return _handle_grok_v2_init_request(req, payload, request_id)

    if _is_grok_v2_complete_path(path_base) or _is_grok_v2_status_path(path_base):
        return _fum.build_upload_result(handled=True)

    if not payload:
        return None

    try:
        obj = json.loads(payload)
    except Exception:
        return None
    if not isinstance(obj, dict) or not _is_grok_upload_request(obj):
        return None

    extracted = _extract_upload_file(obj)
    if not extracted:
        return None
    fname, file_data, _mime = extracted

    try:
        policy = _fum.collection_policy_from_request(req)
        result = _fum.create_pending_upload_batch_from_files(
            request_id,
            [(fname, file_data)],
            "grok",
            policy,
        )
    except Exception:
        logger.exception("grok normalize_upload batch save failed")
        return _fum.build_upload_result(handled=True)

    if not result.get("handled"):
        return _fum.build_upload_result(
            handled=True,
            skipped=bool(result.get("skipped")),
            skip_reason=str(result.get("skip_reason") or ""),
        )

    logger.info("grok normalize_upload: saved %s (%d bytes)", fname, len(file_data))
    saved_files = result.get("saved_files") or []
    upload_ids = [
        str(x.get("upload_id") or "").strip()
        for x in saved_files
        if isinstance(x, dict)
    ]
    if upload_ids:
        _fum.remember_pending_uploads_for_scope(_grok_upload_scope(req), upload_ids)
        for uid in upload_ids:
            remember_recent_chat_file(req, uid)
    return result


def _collect_attachment_refs(obj: Dict[str, Any]) -> List[str]:
    """Use chat JSON fileAttachments/imageAttachments and fileAttachmentsMetadata."""
    refs: List[str] = []
    seen: set[str] = set()

    def add_uid(uid: str) -> None:
        text = str(uid or "").strip()
        if text and text not in seen and _ATTACHMENT_UUID_RE.match(text):
            seen.add(text)
            refs.append(text)

    for key in ("fileAttachments", "imageAttachments", "file_attachment_ids", "fileUris"):
        items = obj.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str):
                add_uid(item)
            elif isinstance(item, dict):
                add_uid(
                    str(
                        item.get("fileMetadataId")
                        or item.get("file_metadata_id")
                        or item.get("file_id")
                        or item.get("id")
                        or item.get("fileId")
                        or ""
                    )
                )

    metadata = obj.get("fileAttachmentsMetadata")
    if isinstance(metadata, list):
        for row in metadata:
            if not isinstance(row, dict):
                continue
            add_uid(str(row.get("fileMetadataId") or row.get("file_metadata_id") or "").strip())

    # WS / mgw: event.item or nested x_grok.input_chunks file_mention
    event = obj.get("event") if isinstance(obj.get("event"), dict) else None
    item = None
    if isinstance(event, dict) and isinstance(event.get("item"), dict):
        item = event.get("item")
    elif isinstance(obj.get("item"), dict):
        item = obj.get("item")
    if isinstance(item, dict):
        for key in ("file_attachment_ids", "fileAttachments", "fileUris"):
            vals = item.get(key)
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, str):
                        add_uid(v)
        xg = item.get("x_grok") if isinstance(item.get("x_grok"), dict) else {}
        for key in ("file_attachment_ids", "fileAttachments", "fileUris"):
            vals = xg.get(key)
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, str):
                        add_uid(v)
        chunks = xg.get("input_chunks") if isinstance(xg.get("input_chunks"), list) else []
        for ch in chunks:
            if not isinstance(ch, dict):
                continue
            mention = ch.get("mention") if isinstance(ch.get("mention"), dict) else {}
            fm = mention.get("file_mention") if isinstance(mention.get("file_mention"), dict) else {}
            add_uid(
                str(
                    fm.get("file_id")
                    or fm.get("fileId")
                    or fm.get("file_metadata_id")
                    or fm.get("fileMetadataId")
                    or ""
                ).strip()
            )
            for nested_key in ("file_mention", "file", "asset", "image", "media"):
                nested = ch.get(nested_key)
                if not isinstance(nested, dict):
                    continue
                add_uid(
                    str(
                        nested.get("file_id")
                        or nested.get("fileId")
                        or nested.get("fileMetadataId")
                        or nested.get("file_metadata_id")
                        or nested.get("asset_id")
                        or nested.get("assetId")
                        or ""
                    ).strip()
                )
            add_uid(
                str(
                    ch.get("file_id")
                    or ch.get("fileId")
                    or ch.get("fileMetadataId")
                    or ch.get("file_metadata_id")
                    or ch.get("asset_id")
                    or ch.get("assetId")
                    or ""
                ).strip()
            )

    # load-responses: responses[].fileAttachments
    responses = obj.get("responses")
    if isinstance(responses, list):
        for row in responses:
            if not isinstance(row, dict):
                continue
            if str(row.get("sender") or "").lower() not in ("human", "user"):
                continue
            for key in ("fileAttachments", "fileUris"):
                vals = row.get(key)
                if isinstance(vals, list):
                    for v in vals:
                        if isinstance(v, str):
                            add_uid(v)
            meta = row.get("fileAttachmentsMetadata")
            if isinstance(meta, list):
                for mrow in meta:
                    if isinstance(mrow, dict):
                        add_uid(
                            str(
                                mrow.get("fileMetadataId")
                                or mrow.get("file_metadata_id")
                                or ""
                            ).strip()
                        )

    return refs


def strip_chat_attachments(obj: Dict[str, Any]) -> int:
    """Remove chat file refs before upstream MASK/BLOCK send."""
    if not isinstance(obj, dict):
        return 0
    removed = 0

    for key in (
        "fileAttachments",
        "imageAttachments",
        "attachments",
        "file_attachment_ids",
        "fileUris",
    ):
        val = obj.get(key)
        if isinstance(val, list) and val:
            removed += len(val)
            obj[key] = []

    metadata = obj.get("fileAttachmentsMetadata")
    if isinstance(metadata, list) and metadata:
        removed += len(metadata)
        obj["fileAttachmentsMetadata"] = []

    if isinstance(obj.get("directUpload"), dict):
        del obj["directUpload"]
        removed += 1

    message = obj.get("message")
    if isinstance(message, str):
        user_text, du = parse_direct_upload_message(message)
        if du is not None and message != user_text:
            obj["message"] = user_text
            removed += 1

    # WS /ws/mgw: nested event.item / item attachment ids + file_mention chunks.
    for container_key in ("event", None):
        container = obj.get("event") if container_key == "event" else obj
        if not isinstance(container, dict):
            continue
        item = container.get("item") if isinstance(container.get("item"), dict) else None
        if not isinstance(item, dict):
            continue
        for key in ("file_attachment_ids", "fileAttachments", "fileUris"):
            val = item.get(key)
            if isinstance(val, list) and val:
                removed += len(val)
                item[key] = []
        xg = item.get("x_grok") if isinstance(item.get("x_grok"), dict) else None
        if not isinstance(xg, dict):
            continue
        chunks = xg.get("input_chunks")
        if not isinstance(chunks, list) or not chunks:
            continue
        kept: List[Any] = []
        for ch in chunks:
            if not isinstance(ch, dict):
                kept.append(ch)
                continue
            mention = ch.get("mention") if isinstance(ch.get("mention"), dict) else {}
            fm = mention.get("file_mention") if isinstance(mention.get("file_mention"), dict) else {}
            if fm.get("file_id") or fm.get("fileId"):
                removed += 1
                continue
            kept.append(ch)
        if len(kept) != len(chunks):
            xg["input_chunks"] = kept

    return removed


def _collect_direct_upload_attachment_rows(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    message = obj.get("message")
    if isinstance(message, str):
        _, du = parse_direct_upload_message(message)
        if isinstance(du, dict):
            rows.append(
                {
                    "file_name": str(du.get("fileName") or "").strip(),
                    "mime_type": str(du.get("fileMimeType") or du.get("mimeType") or "").strip(),
                    "size_bytes": len(_decode_direct_upload_file_data(du.get("fileData"))),
                }
            )
    du = _direct_upload_payload(obj)
    if isinstance(du, dict) and not rows:
        rows.append(
            {
                "file_name": str(du.get("fileName") or "").strip(),
                "mime_type": str(du.get("fileMimeType") or du.get("mimeType") or "").strip(),
                "size_bytes": len(_decode_direct_upload_file_data(du.get("fileData"))),
            }
        )
    return rows


def build_policy_attachments(
    req: Dict[str, Any],
    obj: Dict[str, Any],
    payload: bytes,
) -> List[Dict[str, Any]]:
    """Build file_block_filter attachments from fileAttachments[] and embedded directUpload."""
    del payload
    refs = _collect_attachment_refs(obj)
    used_recent = False
    if not refs:
        recent = peek_recent_chat_files(req)
        if recent:
            logger.info(
                "grok attach recent chat files=%s path=%s",
                recent,
                str((req.get("meta") or {}).get("path") if isinstance(req.get("meta"), dict) else ""),
            )
            refs = list(recent)
            used_recent = True
    direct_rows = _collect_direct_upload_attachment_rows(obj)
    if not refs and not direct_rows:
        return [{"id": "", "size": 0, "name": "", "mime_type": ""}]

    out: List[Dict[str, Any]] = []
    for file_metadata_id in refs:
        # Direct upload returns status:1 only — bind pending grok-* temp id to
        # the server fileMetadataId before path lookup (FIFO per cookie scope).
        _promote_one_pending_to_asset(req, str(file_metadata_id or "").strip())
        resolved = _resolve_grok_file_ref(file_metadata_id)
        upload_id = str(file_metadata_id).strip()
        if resolved and str(resolved.get("upload_id") or "").strip():
            upload_id = str(resolved.get("upload_id") or "").strip()

        name = str((resolved or {}).get("file_name") or "")
        mime = str((resolved or {}).get("mime_type") or "")
        try:
            size = int((resolved or {}).get("size_bytes") or 0)
        except Exception:
            size = 0

        out.append(_finalize_policy_attachment(upload_id or file_metadata_id, name, mime, size))

    if not out and direct_rows:
        for row in direct_rows:
            name = str(row.get("file_name") or "")
            mime = str(row.get("mime_type") or "")
            size = int(row.get("size_bytes") or 0)
            saved = _fum.get_saved_attachment("", fallback_name=name, fallback_mime=mime)
            upload_id = str((saved or {}).get("id") or "").strip()
            out.append(_finalize_policy_attachment(upload_id, name, mime, size))

    # Consume peek-sourced ids after a successful bind so the next text-only
    # turn cannot reattach the same file into a new UI row.
    if used_recent and out and any(str((row or {}).get("id") or "").strip() for row in out):
        consume_recent_chat_files(req, refs)

    return out if out else [{"id": "", "size": 0, "name": "", "mime_type": ""}]


def _finalize_policy_attachment(
    upload_id: str,
    req_name: str,
    req_mime: str,
    req_size: int = 0,
) -> Dict[str, Any]:
    uid = str(upload_id or "").strip()
    name = str(req_name or "").strip()
    mime = str(req_mime or "").strip()
    if not name and uid:
        name = _fum.get_registered_upload_filename(uid)

    saved = _fum.get_saved_attachment(uid, fallback_name=name, fallback_mime=mime) if uid else None
    if not saved and uid:
        saved = _fum.wait_for_saved_attachment(uid, fallback_name=name, fallback_mime=mime)
    if saved:
        size = int(saved.get("size") or 0)
        if size <= 0 and req_size > 0:
            size = req_size
        display_name = name or str(saved.get("name") or "")
        if display_name and _ATTACHMENT_UUID_RE.match(display_name):
            reg = _fum.get_registered_upload_filename(uid)
            if reg:
                display_name = reg
        return {
            "id": str(saved.get("id") or ""),
            "size": size,
            "name": display_name,
            "mime_type": mime or str(saved.get("mime_type") or ""),
        }

    if uid:
        expected = _fum.expected_saved_path(uid)
        logger.warning(
            "grok policy attachment not on disk yet: uid=%s expected=%s",
            uid[:64],
            expected[:120],
        )
        return {
            "id": expected,
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
