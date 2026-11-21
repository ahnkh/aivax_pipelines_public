"""
Gemini file upload and policy attachment assembly.

Adapter owns init → binary upload → StreamGenerate attachment resolution.
file_upload_manager is storage/registry only; ai_policy_engine must not re-resolve attachments.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, unquote_plus

import file_upload_manager as _fum

logger = logging.getLogger(__name__)

POLICY_ATTACHMENTS_FINAL = True

_MAX_DEPTH = 20
_PENDING_UPLOAD_IDS: List[str] = []
_PENDING_UPLOAD_FILENAMES: List[str] = []
_PENDING_UPLOAD_LIMIT = 64
_CLAIMED_POLICY_UPLOADS: set[str] = set()
_INIT_NAME_BY_REQUEST: Dict[str, str] = {}
# upload_id -> client address that actually sent the bytes.
# A later text-only prompt may adopt a file only from this same client.
_UPLOAD_CLIENT: Dict[str, str] = {}


def _bare_client_ip(raw: str) -> str:
    """Envoy puts the user address in XFF or client_ip, sometimes with a port."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("["):
        end = text.find("]")
        text = text[1:end] if end > 1 else ""
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
        if port.isdigit():
            text = host
    try:
        import ipaddress

        return str(ipaddress.ip_address(text))
    except ValueError:
        return ""


def _request_client_ip(req: Optional[Dict[str, Any]]) -> str:
    if not isinstance(req, dict):
        return ""
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    headers = meta.get("request_headers") if isinstance(meta.get("request_headers"), dict) else {}
    for raw in (
        meta.get("x_forwarded_for"),
        headers.get("x-forwarded-for"),
        headers.get("X-Forwarded-For"),
        meta.get("client_ip"),
        meta.get("clientIp"),
        meta.get("true_client_ip"),
        meta.get("x_real_ip"),
    ):
        ip = _bare_client_ip(str(raw or "").split(",")[0])
        if ip:
            return ip
    return ""


def _remember_upload_client(upload_id: str, req: Optional[Dict[str, Any]]) -> None:
    """First client to send this upload keeps it. A later request cannot retag it."""
    uid = os.path.basename(str(upload_id or "").replace("\\", "/")).strip()
    ip = _request_client_ip(req)
    if not uid or not ip:
        return
    _UPLOAD_CLIENT.setdefault(uid, ip)
    if len(_UPLOAD_CLIENT) > 4096:
        for key in list(_UPLOAD_CLIENT)[:2048]:
            _UPLOAD_CLIENT.pop(key, None)


def _upload_id_of(value: str) -> str:
    return os.path.basename(str(value or "").replace("\\", "/")).strip()


def _mark_upload_claimed(upload_id: str) -> None:
    uid = _upload_id_of(upload_id)
    if _is_gemini_local_upload_id(uid):
        _CLAIMED_POLICY_UPLOADS.add(uid)


def _base_request_id(req: Dict[str, Any]) -> str:
    """Envoy tags request-body tees as `<id>-up-<hex>`; the response keeps `<id>`."""
    rid = str(req.get("request_id") or "").strip()
    return rid.split("-up-", 1)[0] if "-up-" in rid else rid

_UPLOAD_ID_IN_TEXT_RE = re.compile(r"upload_id=([^&\s\"'<>]+)", re.IGNORECASE)
_GEMINI_F_REQ_MIME_RE = re.compile(
    r'\["(/contrib_service/ttl_1d/[^"]+)"\s*,\s*(\d+)\s*,\s*null\s*,\s*"([^"]+)"\]',
    flags=re.IGNORECASE,
)
_GEMINI_F_REQ_NAME_RE = re.compile(
    r'\["(/contrib_service/ttl_1d/[^"]+)"\s*,\s*\d+\s*,\s*null\s*,\s*"[^"]+"\]\s*,\s*"((?:\\.|[^"\\])+)"',
    flags=re.IGNORECASE,
)
# Gemini f.req nests JSON-as-string; quotes often remain as \".
_MIN_REAL_SIZE_HINT = 4096
# StreamGenerate often references images uploaded earlier in the chat; 180s is too short.
_MIME_FALLBACK_MAX_AGE_SEC = 7200.0
_MIME_FALLBACK_LIMIT = 32


def _safe_utf8(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _unescape_gemini_f_req_blob(text: str) -> str:
    """Turn JSON-string escapes into plain text so TTL mime/name regexes can match."""
    raw = str(text or "")
    if not raw:
        return ""
    if '\\"' not in raw and "\\\\" not in raw:
        return raw
    return raw.replace("\\\\", "\\").replace('\\"', '"')


def _gemini_f_req_parse_blobs(f_req: str) -> List[str]:
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(f_req or ""))
    blobs: List[str] = [clean, unquote_plus(clean), _unescape_gemini_f_req_blob(clean)]
    outer = _loads_maybe_json(clean)
    if isinstance(outer, list):
        for item in outer:
            if isinstance(item, str) and item.strip():
                blobs.append(item)
                blobs.append(_unescape_gemini_f_req_blob(item))
    # Preserve order, drop empties/dupes.
    out: List[str] = []
    seen: set[str] = set()
    for blob in blobs:
        if not blob or blob in seen:
            continue
        seen.add(blob)
        out.append(blob)
    return out


def _file_header_matches_mime(path: str, mime: str) -> bool:
    mime_l = str(mime or "").lower()
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    if not head:
        return False
    if "pdf" in mime_l:
        return head.startswith(b"%PDF")
    if "png" in mime_l:
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if "jpeg" in mime_l or "jpg" in mime_l:
        return head.startswith(b"\xff\xd8\xff")
    if "gif" in mime_l:
        return head.startswith(b"GIF8")
    if "webp" in mime_l:
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if any(
        tok in mime_l
        for tok in (
            "officedocument",
            "presentation",
            "spreadsheet",
            "wordprocessing",
            "zip",
            "pptx",
            "docx",
            "xlsx",
        )
    ):
        return head.startswith(b"PK\x03\x04")
    return False

def _loads_maybe_json(raw: str) -> Optional[Any]:
    text = (raw or "").strip()
    if not text:
        return None
    for candidate in (text, re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)):
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


def _remember_pending_upload_id(upload_id: str) -> None:
    if not upload_id:
        return
    _PENDING_UPLOAD_IDS.append(upload_id)
    del _PENDING_UPLOAD_IDS[:-_PENDING_UPLOAD_LIMIT]


def _claim_pending_upload_id() -> str:
    """Newest init session that still has no filename and no bytes.

    Logged-in Gemini leaves earlier /upload/ ids pending. FIFO then binds the
    new File-name onto that stale id, and the file that actually arrived stays
    nameless.
    """
    while _PENDING_UPLOAD_IDS:
        uid = _PENDING_UPLOAD_IDS.pop()
        if _fum.get_registered_upload_filename(uid):
            continue
        if _fum.get_saved_attachment(uid):
            continue
        return uid
    return ""


def _remember_pending_upload_filename(filename: str) -> None:
    if not filename:
        return
    _PENDING_UPLOAD_FILENAMES.append(filename)
    del _PENDING_UPLOAD_FILENAMES[:-_PENDING_UPLOAD_LIMIT]


def _claim_pending_upload_filename() -> str:
    if not _PENDING_UPLOAD_FILENAMES:
        return ""
    return _PENDING_UPLOAD_FILENAMES.pop(0)


def _bind_pending_filename(upload_id: str, req: Dict[str, Any], preferred_name: str = "") -> str:
    """Ensure upload_id ↔ filename is registered as early as init/response allows."""
    uid = str(upload_id or "").strip()
    if not uid:
        return ""
    existing = _fum.get_registered_upload_filename(uid)
    if existing:
        return existing
    name = str(preferred_name or "").strip() or _claim_pending_upload_filename()
    if not name:
        return ""
    policy = _fum.collection_policy_from_request(req)
    if not _fum.register_upload_mapping(uid, name, policy):
        return ""
    return name


def _extract_final_upload_name(payload: bytes) -> str:
    if not payload:
        return ""
    byte_match = re.search(rb'/contrib_service/ttl_1d/([^"\'\s\\<>]+)', payload)
    if byte_match and byte_match.group(1):
        return byte_match.group(1).decode("utf-8", errors="replace").strip()
    text = payload.decode("utf-8", errors="replace")
    match = re.search(r'/contrib_service/ttl_1d/([^"\'\s\\<>]+)', text)
    if not match:
        return ""
    return str(match.group(1) or "").strip()


def _extract_upload_init_name(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, (bytes, bytearray)) else str(payload or "")
    match = re.search(r"File name:\s*(.+?)\s*$", text.strip(), re.IGNORECASE | re.DOTALL)
    if match:
        name = str(match.group(1) or "").strip()
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", name).strip()
    return ""


def _extract_upload_id_from_headers(response_headers: Dict[str, Any]) -> str:
    if not isinstance(response_headers, dict):
        return ""
    direct = str(response_headers.get("x-guploader-uploadid") or "").strip()
    if direct:
        return direct
    for key in ("x-goog-upload-url", "x-goog-upload-control-url"):
        val = str(response_headers.get(key) or "")
        match = _UPLOAD_ID_IN_TEXT_RE.search(val)
        if match:
            return str(match.group(1) or "").strip()
    return ""


def _extract_upload_id_from_text(text: str) -> str:
    match = _UPLOAD_ID_IN_TEXT_RE.search(str(text or ""))
    if not match:
        return ""
    return str(match.group(1) or "").strip()


def _normalize_upload_meta(meta: Dict[str, Any], payload: bytes = b"") -> Tuple[str, str]:
    response_headers = meta.get("response_headers") if isinstance(meta.get("response_headers"), dict) else {}
    upload_id = str(meta.get("upload_id") or "").strip()
    if not upload_id:
        upload_id = _extract_upload_id_from_headers(response_headers)
    if not upload_id and payload:
        upload_id = _extract_upload_id_from_text(_safe_utf8(payload))
    upload_filename = str(meta.get("upload_filename") or "").strip()
    if upload_id:
        meta["upload_id"] = upload_id
    if upload_filename:
        meta["upload_filename"] = upload_filename
    return upload_id, upload_filename


def _is_upload_path(path: str) -> bool:
    # Inline Envoy meta.path often includes ?upload_id=…; Mirror eve may be path-only.
    path_only = str(path or "").split("?", 1)[0]
    return "/upload/" in path_only or path_only.endswith("/upload") or path_only == "/upload"


def _save_upload_payload(
    req: Dict[str, Any],
    upload_id: str,
    payload: bytes,
    upload_filename: str = "",
) -> Dict[str, Any]:
    policy = _fum.collection_policy_from_request(req)
    registered_name = _fum.get_registered_upload_filename(upload_id)
    pending_name = upload_filename or registered_name
    if not _human_upload_filename(pending_name):
        inherited = _inherit_filename_from_probe(upload_id)
        if inherited:
            pending_name = inherited
            logger.info(
                "gemini upload inherit filename from probe: uid=%s name=%s",
                upload_id,
                inherited,
            )

    if pending_name:
        human = _human_upload_filename(pending_name)
        existing_human = _human_upload_filename(registered_name)
        if human and human != existing_human:
            if not _fum.register_upload_mapping(upload_id, pending_name, policy):
                return _fum.build_skipped_upload_result(upload_id, pending_name, "collection_policy")
            registered_name = pending_name

    saved_path = ""
    unit_type = str(req.get("unit_type") or "").lower()
    if payload and unit_type in ("raw_body", "form_body", "json_body"):
        try:
            saved_path = _fum.append_upload_payload(upload_id, payload, policy)
            if not saved_path:
                return _fum.build_skipped_upload_result(
                    upload_id,
                    pending_name or registered_name,
                    "collection_policy",
                )
        except Exception:
            logger.exception("Gemini upload save failed: upload_id=%s", upload_id)
            return _fum.build_upload_result(
                upload_id=upload_id,
                upload_filename=pending_name or registered_name or upload_id[:48],
                handled=True,
            )

    effective_name = pending_name or registered_name or upload_id[:48]
    return _fum.build_upload_result(
        upload_id=upload_id,
        upload_filename=effective_name,
        saved_path=saved_path,
        handled=True,
    )


def normalize_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    upload_id, upload_filename = _normalize_upload_meta(meta, payload)
    path = str(meta.get("path") or "")
    direction = str(req.get("direction") or "").lower()

    logger.info(
        "gemini normalize_upload: dir=%s path=%.80s uid=%.40s ufname=%s reqid=%s payload=%d",
        direction,
        path,
        upload_id,
        upload_filename,
        str(req.get("request_id") or "").strip(),
        len(payload) if payload else 0,
    )

    if not upload_id:
        m = re.search(r"upload_id=([^&\s]+)", path)
        if m:
            upload_id = m.group(1)
    if upload_id:
        _remember_upload_client(upload_id, req)

    if not _is_upload_path(path):
        if upload_id or upload_filename:
            return _fum.build_upload_result(
                upload_id=upload_id,
                upload_filename=upload_filename,
                handled=True,
            )
        return None

    if upload_id and direction == "request":
        pending_name = _fum.get_registered_upload_filename(upload_id)
        if not pending_name:
            pending_name = _claim_pending_upload_filename()
        logger.info("gemini upload data request: uid=%s name=%s", upload_id, pending_name)
        return _save_upload_payload(req, upload_id, payload, pending_name)

    if upload_id and direction == "response":
        ttl_name = _extract_final_upload_name(payload)
        # The init POST that carried "File name:" and its response share a request id.
        # Use that pairing first. Parallel uploads break newest-name guessing.
        paired_name = _INIT_NAME_BY_REQUEST.pop(_base_request_id(req), "")
        # Bind pending filename as soon as we learn upload_id (before binary arrives).
        bound_name = _bind_pending_filename(upload_id, req, paired_name or upload_filename)
        if not _human_upload_filename(bound_name):
            inherited = _inherit_filename_from_probe(upload_id)
            if inherited:
                bound_name = _bind_pending_filename(upload_id, req, inherited) or inherited
        if ttl_name:
            _fum.register_upload_ttl_segment(ttl_name, upload_id)
            logger.info(
                "gemini upload response ttl mapped: uid=%s segment=%s bound_name=%s",
                upload_id,
                ttl_name,
                bound_name or "",
            )
            return _fum.build_upload_result(
                upload_id=upload_id,
                upload_filename=bound_name or upload_filename,
                handled=True,
            )

        final_name = bound_name or upload_filename or _fum.get_registered_upload_filename(upload_id)
        if final_name:
            logger.info("gemini upload init response mapped: uid=%s final_name=%s", upload_id, final_name)
            return _fum.build_upload_result(
                upload_id=upload_id,
                upload_filename=final_name,
                handled=True,
            )

        _remember_pending_upload_id(upload_id)
        logger.info("gemini upload init response pending filename: uid=%s", upload_id)
        return _fum.build_upload_result(upload_id=upload_id, handled=True)

    if direction == "request":
        init_name = _extract_upload_init_name(payload) or upload_filename
        if init_name:
            _INIT_NAME_BY_REQUEST[_base_request_id(req)] = init_name
            while len(_INIT_NAME_BY_REQUEST) > _PENDING_UPLOAD_LIMIT:
                _INIT_NAME_BY_REQUEST.pop(next(iter(_INIT_NAME_BY_REQUEST)))
        pending_id = _claim_pending_upload_id()
        policy = _fum.collection_policy_from_request(req)
        if pending_id and init_name:
            _remember_upload_client(pending_id, req)
            if not _fum.register_upload_mapping(pending_id, init_name, policy):
                return _fum.build_skipped_upload_result(pending_id, init_name, "collection_policy")
            logger.info("gemini upload init mapped: uid=%s filename=%s", pending_id, init_name)
            return _fum.build_upload_result(
                upload_id=pending_id,
                upload_filename=init_name,
                handled=True,
            )
        if init_name:
            _remember_pending_upload_filename(init_name)
            logger.info("gemini upload init filename pending: filename=%s", init_name)
            return _fum.build_upload_result(upload_filename=init_name, handled=True)

    if upload_id or upload_filename:
        return _fum.build_upload_result(
            upload_id=upload_id,
            upload_filename=upload_filename,
            handled=True,
        )
    return None


def _extract_gemini_ttl_segments(payload: bytes) -> List[str]:
    if not payload:
        return []
    text = payload.decode("utf-8", errors="replace")
    blobs = [text]
    try:
        blobs.append(unquote(text))
    except Exception:
        pass
    seen: set[str] = set()
    out: List[str] = []
    for blob in blobs:
        for m in re.finditer(r"/contrib_service/ttl_1d/([^\"'\s\\<>]+)", blob, flags=re.IGNORECASE):
            seg = unquote((m.group(1) or "").strip())
            if not seg or any(ch in seg for ch in "\"'[]{}, "):
                continue
            if seg and seg not in seen:
                seen.add(seg)
                out.append(seg)
        for m in re.finditer(r"(?i)contrib_service%2Fttl_1d%2F([^&\s\"'<>%]+)", blob):
            seg = unquote((m.group(1) or "").strip())
            if not seg or any(ch in seg for ch in "\"'[]{}, "):
                continue
            if seg and seg not in seen:
                seen.add(seg)
                out.append(seg)
    return out


def _extract_gemini_f_req_raw(payload: bytes) -> str:
    form_text = _safe_utf8(payload)
    params = parse_qs(form_text, keep_blank_values=True, strict_parsing=False)
    values = params.get("f.req")
    if not values:
        return ""
    return str(values[0] or "")


def _decode_gemini_json_string(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        decoded = json.loads(f'"{text}"')
        if isinstance(decoded, str):
            return decoded
    except Exception:
        pass
    return text


def _iter_gemini_f_req_json_roots(raw: str) -> List[Any]:
    roots: List[Any] = []
    seen: set[str] = set()
    queue: List[Any] = []

    def push(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if not text or text in seen:
                return
            seen.add(text)
            parsed = _loads_maybe_json(text)
            if parsed is None:
                return
            queue.append(parsed)
            return
        queue.append(value)

    push(raw.strip())
    while queue:
        current = queue.pop(0)
        roots.append(current)
        if isinstance(current, list):
            for item in current:
                push(item)
        elif isinstance(current, dict):
            for item in current.values():
                push(item)
    return roots


def _collect_gemini_mime_by_segment(value: Any, out: Dict[str, str], depth: int = 0) -> None:
    if depth > _MAX_DEPTH or not isinstance(value, list):
        return
    for item in value:
        if (
            isinstance(item, list)
            and len(item) >= 4
            and isinstance(item[0], str)
            and "/contrib_service/ttl_1d/" in item[0]
            and isinstance(item[3], str)
        ):
            segment = os.path.basename(unquote(item[0].replace("\\", "/").strip("/")))
            mime = str(item[3] or "").strip()
            if segment and mime and segment.lower() not in out:
                out[segment.lower()] = mime
        _collect_gemini_mime_by_segment(item, out, depth + 1)


def _parse_gemini_mime_by_segment(payload: bytes) -> Dict[str, str]:
    f_req = _extract_gemini_f_req_raw(payload)
    if not f_req:
        return {}
    out: Dict[str, str] = {}
    blobs = _gemini_f_req_parse_blobs(f_req)
    for blob in blobs:
        for match in _GEMINI_F_REQ_MIME_RE.finditer(blob):
            segment = os.path.basename(unquote(str(match.group(1) or "").replace("\\", "/").strip("/")))
            mime = str(match.group(3) or "").strip()
            if segment and mime:
                out.setdefault(segment.lower(), mime)
    if not out:
        clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", f_req)
        for root in _iter_gemini_f_req_json_roots(clean):
            _collect_gemini_mime_by_segment(root, out)
        for root in _iter_gemini_f_req_json_roots(_unescape_gemini_f_req_blob(clean)):
            _collect_gemini_mime_by_segment(root, out)
    return out


def _parse_gemini_sizes_by_segment(payload: bytes) -> Dict[str, int]:
    """Parse byte-size hints only. Gemini often puts a small type code (1/12) here — ignore those."""
    f_req = _extract_gemini_f_req_raw(payload)
    if not f_req:
        return {}
    out: Dict[str, int] = {}
    for blob in _gemini_f_req_parse_blobs(f_req):
        for match in _GEMINI_F_REQ_MIME_RE.finditer(blob):
            segment = os.path.basename(unquote(str(match.group(1) or "").replace("\\", "/").strip("/")))
            try:
                size = int(match.group(2) or 0)
            except Exception:
                size = 0
            # Real attach sizes are large; tiny values are enum/type codes, not bytes.
            if segment and size >= _MIN_REAL_SIZE_HINT and segment.lower() not in out:
                out[segment.lower()] = size
    return out


def _parse_gemini_names_by_segment(payload: bytes) -> Dict[str, str]:
    f_req = _extract_gemini_f_req_raw(payload)
    if not f_req:
        return {}
    out: Dict[str, str] = {}
    for blob in _gemini_f_req_parse_blobs(f_req):
        for match in _GEMINI_F_REQ_NAME_RE.finditer(blob):
            segment = os.path.basename(unquote(str(match.group(1) or "").replace("\\", "/").strip("/")))
            name = os.path.basename(_decode_gemini_json_string(match.group(2)).replace("\\", "/"))
            if segment and name and segment.lower() not in out:
                out[segment.lower()] = name
    return out


def _resolve_gemini_upload_id(segment: str, embedded_ids: List[str]) -> str:
    uid = _fum.lookup_upload_id_by_ttl_segment(segment)
    if uid and _is_gemini_local_upload_id(uid):
        return uid
    gemini_embedded = [x for x in embedded_ids if _is_gemini_local_upload_id(x)]
    if len(gemini_embedded) == 1:
        return gemini_embedded[0]
    return ""


# Google resumable guploader ids: AKh…, AJjja9…, and logged-in AP6rU8….
_GEMINI_UPLOAD_ID_RE = re.compile(r"^A[A-Za-z0-9_-]{24,}$")


def _is_gemini_local_upload_id(upload_id: str) -> bool:
    """Gemini resumable upload_ids are AJ… / AK… — never claim ChatGPT file_* / Claude UUIDs."""
    uid = str(upload_id or "").strip()
    if not uid:
        return False
    base = os.path.basename(uid.replace("\\", "/"))
    return bool(_GEMINI_UPLOAD_ID_RE.match(base))


def _human_upload_filename(name: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    if not base:
        return ""
    if not re.search(r"\.[A-Za-z0-9]{2,8}$", base):
        return ""
    if _GEMINI_UPLOAD_ID_RE.match(base):
        return ""
    return base


def _inherit_filename_from_probe(upload_id: str) -> str:
    """Copy File-name from the short Gemini init session onto the uid that got the bytes.

    Gemini POSTs ``File name: img.jpg`` against the first /upload/ session, then
    sends the JPEG on a *second* upload_id. FIFO mapping leaves the real uid nameless.
    """
    uid = str(upload_id or "").strip()
    if not uid:
        return ""
    iter_maps = getattr(_fum, "iter_registered_upload_mappings", None)
    if not callable(iter_maps):
        return ""
    for other, name, _ts in iter_maps(limit=64):
        if other == uid or not _is_gemini_local_upload_id(other):
            continue
        human = _human_upload_filename(name)
        if not human:
            continue
        if _fum.get_saved_attachment(other, fallback_name=human):
            continue
        return human
    return ""


def _finalize_gemini_attachment_item(
    upload_id: str,
    mime_type: str,
    f_req_name: str = "",
    size_hint: int = 0,
    exclude_ids: Optional[set] = None,
    ttl_segment: str = "",
    client_ip: str = "",
) -> Dict[str, Any]:
    uid = str(upload_id or "").strip()
    mime = str(mime_type or "").strip()
    name = str(f_req_name or "").strip()
    excluded = exclude_ids if exclude_ids is not None else set()
    saved: Optional[Dict[str, Any]] = None
    if not name:
        name = _fum.get_registered_upload_filename(uid)

    def _usable(cand: str) -> bool:
        c = str(cand or "").strip()
        if not c or c in excluded or os.path.basename(c) in excluded:
            return False
        # Filename/size fallback must not steal ChatGPT/Claude uploads,
        # or a file another client uploaded under a common name.
        if not _is_gemini_local_upload_id(c):
            return False
        if client_ip and _UPLOAD_CLIENT.get(_upload_id_of(c)) != client_ip:
            return False
        return True

    # TTL→upload_id miss (pcap race / response lost): recover via registered filename.
    resolve_via = "upload_id" if uid else "none"
    if uid and not _usable(uid):
        # Explicit TTL-mapped uid may already be AKh; if not gemini-shaped, drop.
        if not _is_gemini_local_upload_id(uid):
            uid = ""
            resolve_via = "none"
        elif uid in excluded or os.path.basename(uid) in excluded:
            uid = ""
            resolve_via = "none"
    if not uid and name:
        by_name = _fum.lookup_upload_ids_by_registered_filename(name)
        inflight = ""
        for cand in by_name:
            if not _usable(cand):
                continue
            probe = _fum.get_saved_attachment(cand, fallback_name=name, fallback_mime=mime)
            if probe:
                uid = cand
                saved = probe
                resolve_via = "filename"
                break
            if not inflight:
                inflight = cand
        if not uid and inflight:
            uid = inflight
            resolve_via = "filename_inflight"
    if not saved and not uid and size_hint >= _MIN_REAL_SIZE_HINT:
        by_size = _fum.lookup_upload_ids_by_saved_size(size_hint)
        for cand in by_size:
            if _usable(cand):
                uid = cand
                resolve_via = "size"
                break
    if not uid and (name or mime):
        recent = _fum.lookup_recent_saved_upload_ids(
            prefix="",
            limit=_MIME_FALLBACK_LIMIT,
            max_age_sec=_MIME_FALLBACK_MAX_AGE_SEC,
        )
        recent = [c for c in recent if _usable(c)]
        if len(recent) == 1:
            uid = recent[0]
            resolve_via = "recent_single"
        elif mime:
            for cand in recent:
                path = _fum.get_saved_attachment(cand, fallback_name=name, fallback_mime=mime)
                saved_path = str((path or {}).get("id") or "")
                if saved_path and _file_header_matches_mime(saved_path, mime):
                    uid = cand
                    resolve_via = "mime_magic"
                    break

    if uid:
        loaded = _fum.get_saved_attachment(uid, fallback_name=name, fallback_mime=mime)
        if loaded:
            saved = loaded
    if not saved and name:
        by_name = _fum.lookup_upload_ids_by_registered_filename(name)
        for cand in by_name:
            if not _usable(cand):
                continue
            saved = _fum.get_saved_attachment(cand, fallback_name=name, fallback_mime=mime)
            if saved:
                uid = cand
                resolve_via = "filename_rescan"
                break
            # Registered name but binary not on disk yet (second file still uploading).
            if not uid:
                uid = cand
                resolve_via = "filename_inflight"
    if not saved and size_hint >= _MIN_REAL_SIZE_HINT:
        by_size = _fum.lookup_upload_ids_by_saved_size(size_hint)
        for cand in by_size:
            if not _usable(cand):
                continue
            saved = _fum.get_saved_attachment(cand, fallback_name=name, fallback_mime=mime)
            if saved:
                uid = cand
                resolve_via = "size_rescan"
                break
    if not saved and (name or mime):
        recent = _fum.lookup_recent_saved_upload_ids(
            prefix="",
            limit=_MIME_FALLBACK_LIMIT,
            max_age_sec=_MIME_FALLBACK_MAX_AGE_SEC,
        )
        recent = [c for c in recent if _usable(c)]
        if len(recent) == 1:
            cand = recent[0]
            saved = _fum.get_saved_attachment(cand, fallback_name=name, fallback_mime=mime)
            if saved:
                uid = cand
                resolve_via = "recent_single_rescan"
        elif mime:
            for cand in recent:
                cand_saved = _fum.get_saved_attachment(cand, fallback_name=name, fallback_mime=mime)
                saved_path = str((cand_saved or {}).get("id") or "")
                if cand_saved and saved_path and _file_header_matches_mime(saved_path, mime):
                    saved = cand_saved
                    uid = cand
                    resolve_via = "mime_magic_rescan"
                    break

    if not saved and uid:
        waited = _fum.wait_for_saved_attachment(uid, fallback_name=name, fallback_mime=mime)
        if waited:
            saved = waited
            resolve_via = f"{resolve_via}+wait" if resolve_via != "none" else "wait"

    logger.info(
        "gemini finalize attachment via=%s name=%s size_hint=%s saved=%s id_len=%s size=%s",
        resolve_via,
        (name or "")[:80],
        int(size_hint or 0),
        bool(saved),
        len(str((saved or {}).get("id") or uid or "")),
        int((saved or {}).get("size") or size_hint or 0),
    )
    if saved:
        return {
            "id": str(saved.get("id") or ""),
            "size": int(saved.get("size") or 0),
            "name": name or _fum.get_registered_upload_filename(uid),
            "mime_type": mime,
        }
    # In-flight upload: keep stable tmpfs path so multi-file policy sees both attachments.
    if uid and _is_gemini_local_upload_id(uid):
        expected = _fum.expected_saved_path(uid)
        logger.warning(
            "gemini policy attachment not on disk yet: uid=%s expected=%s via=%s",
            uid[:64],
            expected[:120],
            resolve_via,
        )
        return {
            "id": expected,
            "size": int(size_hint or 0),
            "name": name or _fum.get_registered_upload_filename(uid),
            "mime_type": mime,
        }
    # No binary on disk (e.g. QUIC bypass) — still emit a stable non-empty id so
    # management UI can list multi-file rows (React key / selection). file_block
    # skips non-path ids (os.path.isfile fails).
    placeholder = ""
    seg = os.path.basename(str(ttl_segment or "").strip().strip("/"))
    if seg:
        placeholder = f"gemini:ttl:{seg}"
    elif name:
        placeholder = f"gemini:name:{name}"
    return {
        "id": placeholder,
        "size": int(size_hint or 0),
        "name": name,
        "mime_type": mime,
    }


def claim_recent_gemini_attachments(
    max_age_sec: float = 180.0,
    req: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Saved Gemini uploads this same client has not yet put on a policy row.

    Logged-in processSession does not repeat the file inside f.req, so the
    prompt request picks up that client's own /upload/ from the last few
    minutes. Another client's file is never adopted, and a file already
    written onto a prompt is not written again.
    """
    owner = _request_client_ip(req)
    if not owner:
        return []
    ids = _fum.lookup_recent_saved_upload_ids(
        prefix="",
        limit=8,
        max_age_sec=max_age_sec,
    )
    items: List[Dict[str, Any]] = []
    skipped_foreign = 0
    for uid in ids:
        if uid in _CLAIMED_POLICY_UPLOADS or not _is_gemini_local_upload_id(uid):
            continue
        if _UPLOAD_CLIENT.get(uid) != owner:
            skipped_foreign += 1
            continue
        name = _fum.get_registered_upload_filename(uid)
        if not _human_upload_filename(name):
            inherited = _inherit_filename_from_probe(uid)
            if inherited:
                name = inherited
                _fum.register_upload_mapping(uid, inherited, {})
        saved = _fum.get_saved_attachment(uid, fallback_name=name or "")
        if not saved:
            continue
        if _human_upload_filename(name):
            saved = dict(saved)
            saved["name"] = name
        items.append(saved)
        _mark_upload_claimed(uid)
    if skipped_foreign:
        logger.info(
            "gemini processSession left foreign uploads unattached count=%d client=%s",
            skipped_foreign,
            owner,
        )
    return items


def build_policy_attachments(
    payload: bytes,
    req: Optional[Dict[str, Any]] = None,
    extra_segments: Optional[List[str]] = None,
    skip_claimed: bool = False,
) -> List[Dict[str, Any]]:
    """Build final file_block_filter attachments from StreamGenerate f.req.

    extra_segments carries TTL ids that exist only inside a processSession
    protobuf. skip_claimed keeps a file already written on an earlier row
    from being copied onto a later text turn.
    """
    segments = _extract_gemini_ttl_segments(payload)
    seen_segs = {seg.lower() for seg in segments}
    for seg in extra_segments or []:
        text = str(seg or "").strip()
        if text and text.lower() not in seen_segs:
            seen_segs.add(text.lower())
            segments.append(text)
    mime_by_segment = _parse_gemini_mime_by_segment(payload)
    name_by_segment = _parse_gemini_names_by_segment(payload)
    size_by_segment = _parse_gemini_sizes_by_segment(payload)
    text_blob = _safe_utf8(payload)
    embedded = _fum.lookup_upload_ids_embedded_in_text(text_blob)
    client_ip = _request_client_ip(req)
    items: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    claimed_ids: set[str] = set()

    for seg in segments:
        uid = _resolve_gemini_upload_id(seg, embedded)
        key = uid if uid else seg.lower()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        if (
            skip_claimed
            and uid
            and _upload_id_of(uid) in _CLAIMED_POLICY_UPLOADS
        ):
            continue
        mime = mime_by_segment.get(seg.lower(), "")
        f_req_name = name_by_segment.get(seg.lower(), "")
        size_hint = int(size_by_segment.get(seg.lower(), 0) or 0)
        item = _finalize_gemini_attachment_item(
            uid, mime, f_req_name, size_hint, claimed_ids, ttl_segment=seg, client_ip=client_ip
        )
        items.append(item)
        bound = str(item.get("id") or "").strip()
        if bound:
            claimed_ids.add(bound)
            claimed_ids.add(os.path.basename(bound))
            _mark_upload_claimed(bound)

    for uid in embedded:
        if uid in seen_keys or uid in claimed_ids:
            continue
        if any(str(it.get("id") or "").endswith(uid) for it in items):
            continue
        seen_keys.add(uid)
        item = _finalize_gemini_attachment_item(
            uid, "", exclude_ids=claimed_ids, client_ip=client_ip
        )
        items.append(item)
        bound = str(item.get("id") or "").strip()
        if bound:
            claimed_ids.add(bound)
            claimed_ids.add(os.path.basename(bound))
            _mark_upload_claimed(bound)

    if not items:
        return [{"id": "", "size": 0, "name": "", "mime_type": ""}]
    return items


# One attachment group only — greedy match across consecutive groups breaks multi-file f.req.
_GEMINI_F_REQ_ATTACHMENT_GROUP_RE = re.compile(
    r'\[\[(?:\\")?/contrib_service/ttl_1d/[^\]\\"]+(?:\\")?'
    r'\s*,\s*\d+\s*,\s*null\s*,\s*(?:\\")?[^\]\\"]+(?:\\")?\]'
    r'\s*,\s*(?:\\")?(?:\\.|[^"\\])*?(?:\\")?'
    r'(?:\s*,\s*null\s*){6}\s*,\s*\[0\]\]',
    re.IGNORECASE,
)


def _is_gemini_ttl_file_entry(item: Any) -> bool:
    return (
        isinstance(item, list)
        and len(item) >= 4
        and isinstance(item[0], str)
        and "/contrib_service/ttl_1d/" in item[0]
    )


def _is_gemini_file_follower_string(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s or s.startswith("c_"):
        return False
    if len(s) > 256 or "\n" in s:
        return False
    if re.search(r"\.(pdf|docx?|xlsx?|pptx?|hwp|hwpx|png|jpe?g|gif|txt|csv|zip)$", s, re.I):
        return True
    return bool(re.fullmatch(r"[\w.\- ()가-힣]+", s))


def _is_gemini_attachment_group(item: Any) -> bool:
    return isinstance(item, list) and bool(item) and _is_gemini_ttl_file_entry(item[0])


def _is_gemini_attachment_orphan_shell(item: Any) -> bool:
    """Leftover [null, ..., [0]] after a partial strip — invalid for upstream."""
    if not isinstance(item, list) or len(item) < 2:
        return False
    if _is_gemini_attachment_group(item) or _is_gemini_ttl_file_entry(item):
        return False
    if not (isinstance(item[-1], list) and item[-1] == [0]):
        return False
    for entry in item:
        if isinstance(entry, str) and ("/contrib_service/" in entry or "ttl_1d" in entry):
            return False
        if isinstance(entry, list) and _is_gemini_ttl_file_entry(entry):
            return False
    return True


def _is_gemini_user_turn_tuple(item: Any) -> bool:
    """User message shape: [\"<prompt>\", 0, null, <attachments>]."""
    return (
        isinstance(item, list)
        and len(item) >= 4
        and isinstance(item[0], str)
        and item[1] == 0
        and item[2] is None
    )


def _attachment_slot_is_empty(att: Any) -> bool:
    if att is None:
        return True
    if not isinstance(att, list):
        return False
    if not att:
        return True
    return all(x is None or _attachment_slot_is_empty(x) for x in att)


def _attachment_slot_has_ttl(att: Any, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH or att is None:
        return False
    if isinstance(att, str):
        return "ttl_1d" in att or "/contrib_service/" in att
    if isinstance(att, list):
        return any(_attachment_slot_has_ttl(child, depth + 1) for child in att)
    return False


def _canonical_gemini_user_turn_no_attachments(prompt: str) -> List[Any]:
    """Gemini StreamGenerate user turn without files: [prompt, 0, null, null, null, null, 0]."""
    return [prompt, 0, None, None, None, None, 0]


def _json_tree_has_ttl(value: Any, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if isinstance(value, str):
        return "ttl_1d" in value or "/contrib_service/" in value
    if isinstance(value, list):
        return any(_json_tree_has_ttl(child, depth + 1) for child in value)
    if isinstance(value, dict):
        return any(_json_tree_has_ttl(child, depth + 1) for child in value.values())
    return False


def _collapse_gemini_user_turn_no_attachments(item: Any) -> bool:
    if not _is_gemini_user_turn_tuple(item):
        return False
    if not _attachment_slot_is_empty(item[3]):
        return False
    prompt = item[0]
    target = _canonical_gemini_user_turn_no_attachments(prompt)
    if item == target:
        return False
    item[:] = target
    return True


def _force_clear_user_turn_attachment_slots(value: Any, depth: int = 0) -> int:
    """Last-resort: null user-turn attachment slot(s) and collapse to canonical shape."""
    if depth > _MAX_DEPTH:
        return 0
    cleared = 0
    if isinstance(value, list):
        if _is_gemini_user_turn_tuple(value) and (
            _attachment_slot_has_ttl(value[3]) or not _attachment_slot_is_empty(value[3])
        ):
            value[3] = None
            if _collapse_gemini_user_turn_no_attachments(value):
                cleared += 1
            else:
                cleared += 1
        for child in value:
            cleared += _force_clear_user_turn_attachment_slots(child, depth + 1)
    elif isinstance(value, dict):
        for child in value.values():
            cleared += _force_clear_user_turn_attachment_slots(child, depth + 1)
    return cleared


def _strip_attachments_from_json(value: Any, depth: int = 0) -> int:
    if depth > _MAX_DEPTH:
        return 0
    removed = 0
    if isinstance(value, list):
        # Remove attachment groups at this level before recursing — otherwise the
        # inner file tuple is stripped first and orphan shells break f.req JSON.
        i = 0
        while i < len(value):
            if (
                _is_gemini_user_turn_tuple(value)
                and i == 3
                and _attachment_slot_has_ttl(value[i])
            ):
                value[i] = None
                removed += 1
                i += 1
                continue
            if _is_gemini_attachment_group(value[i]) or _is_gemini_attachment_orphan_shell(value[i]):
                value.pop(i)
                removed += 1
                continue
            if _is_gemini_ttl_file_entry(value[i]):
                if i + 1 < len(value) and _is_gemini_file_follower_string(value[i + 1]):
                    value.pop(i + 1)
                    removed += 1
                value.pop(i)
                removed += 1
                continue
            i += 1
        for child in value:
            removed += _strip_attachments_from_json(child, depth + 1)
        if _collapse_gemini_user_turn_no_attachments(value):
            removed += 1
    elif isinstance(value, dict):
        for child in value.values():
            removed += _strip_attachments_from_json(child, depth + 1)
    return removed


def _normalize_gemini_attachment_slots(value: Any, depth: int = 0) -> None:
    """Collapse stripped user turns to [prompt,0,null,null,null,null,0]."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, list):
        if _is_gemini_user_turn_tuple(value) and _attachment_slot_has_ttl(value[3]):
            value[3] = None
        _collapse_gemini_user_turn_no_attachments(value)
        for child in value:
            _normalize_gemini_attachment_slots(child, depth + 1)
    elif isinstance(value, dict):
        for child in value.values():
            _normalize_gemini_attachment_slots(child, depth + 1)


def _strip_f_req_attachments_regex(raw: str) -> Tuple[str, int]:
    removed = 0
    out = str(raw or "")
    while True:
        new_out, n = _GEMINI_F_REQ_ATTACHMENT_GROUP_RE.subn("null", out)
        if n <= 0:
            break
        removed += n
        out = new_out
    return out, removed


def _strip_parsed_f_req_inner(parsed: Any) -> int:
    had_ttl = _json_tree_has_ttl(parsed)
    removed = _strip_attachments_from_json(parsed)
    _normalize_gemini_attachment_slots(parsed)
    if _json_tree_has_ttl(parsed):
        removed += _force_clear_user_turn_attachment_slots(parsed)
        _normalize_gemini_attachment_slots(parsed)
    if had_ttl and not _json_tree_has_ttl(parsed) and removed <= 0:
        removed = 1
    return removed


def strip_f_req_attachments(f_req_raw: str) -> Tuple[str, int]:
    """Remove contrib_service/ttl_1d file refs from StreamGenerate f.req JSON."""
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(f_req_raw or ""))
    if not raw.strip():
        return f_req_raw, 0

    result = raw
    json_removed = 0
    outer = _loads_maybe_json(raw)
    if outer is not None:
        if isinstance(outer, list):
            for idx, item in enumerate(outer):
                if isinstance(item, str):
                    parsed = _loads_maybe_json(item)
                    if isinstance(parsed, (dict, list)):
                        removed = _strip_parsed_f_req_inner(parsed)
                        if removed > 0:
                            outer[idx] = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
                            json_removed += removed
                elif isinstance(item, (dict, list)):
                    removed = _strip_parsed_f_req_inner(item)
                    if removed > 0:
                        json_removed += removed
        else:
            removed = _strip_parsed_f_req_inner(outer)
            if removed > 0:
                json_removed += removed
        if json_removed > 0:
            result = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))

    total = json_removed
    if total <= 0:
        return f_req_raw, 0
    return result, total


# Public entry points share file_upload_manager.UPLOAD_STATE_LOCK so the
# policy loop can run normalize/persist on a worker without racing extract.
_fum.bind_upload_state_lock(globals())
