"""
Wrks file upload and policy attachment assembly.

Legacy flow:
  POST /file/toOpenai              multipart file bytes
  response                         {"data":{"file":{"name":"...","gptFileId":"file-..."}}}
  POST /file/forConversation       multipart image
  response                         {"data":{"file":{"imageUrl":"https://images.wrks.ai/dalle/TOKEN.png"}}}
  chat                             {"message":"...","gptFileIds":["file-..."]}

Agent UI v2 (2026-08):
  POST /ai/file/chat               multipart: file, vectorstoreId, chatId
  POST /ai/file                    multipart: file
  POST /ai/file/processing-status  {"fileIds":[...]}  (status only — not an upload)
  chat                             fileIds / imageFileIds (numeric or string)

Adapter owns upload + attachment resolution; file_upload_manager is storage only.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import file_upload_manager as _fum

logger = logging.getLogger(__name__)

POLICY_ATTACHMENTS_FINAL = True

_WRKS_FILE_INDEX: Dict[str, Dict[str, Any]] = {}
_WRKS_FILE_ORDER: deque[str] = deque()
_WRKS_FILE_LIMIT = 512

_IMAGE_UPLOAD_PATH = "/file/forConversation"
_OPENAI_UPLOAD_PATH = "/file/toOpenai"
_AI_FILE_CHAT_PATH = "/ai/file/chat"
_AI_FILE_PATH = "/ai/file"
_UPLOAD_PATHS = frozenset(
    {
        _IMAGE_UPLOAD_PATH,
        _OPENAI_UPLOAD_PATH,
        _AI_FILE_CHAT_PATH,
        _AI_FILE_PATH,
    }
)

_GPT_FILE_ID_RE = re.compile(r"^file-[A-Za-z0-9_-]+$")
_GPT_FILE_ID_JSON_RE = re.compile(r'"gptFileId"\s*:\s*"(file-[A-Za-z0-9_-]+)"', re.IGNORECASE)
_FILE_ID_JSON_RE = re.compile(r'"fileId"\s*:\s*"?([A-Za-z0-9_-]+)"?', re.IGNORECASE)
_FILE_NAME_JSON_RE = re.compile(r'"name"\s*:\s*"((?:\\.|[^"\\])*)"', re.IGNORECASE)
_DALLE_URL_TOKEN_RE = re.compile(
    r"https://images\.wrks\.ai/dalle/([A-Za-z0-9_-]+)\.[A-Za-z0-9]+"
)
_DALLE_IMAGE_URL_JSON_RE = re.compile(
    r'"imageUrl"\s*:\s*"https://images\.wrks\.ai/dalle/([A-Za-z0-9_-]+)\.[A-Za-z0-9]+"'
)


def _safe_utf8(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


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


def _wrks_upload_scope(req: Dict[str, Any]) -> str:
    token = _extract_bearer_token(req)
    return f"wrks:{token}" if token else "wrks:orphan"


def _wrks_path_base(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("path") or "").split("?", 1)[0]


def _is_wrks_host(req: Dict[str, Any]) -> bool:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return "gateway-api.wrks.ai" in str(meta.get("host") or "").lower()


def is_wrks_file_upload_request(req: Dict[str, Any]) -> bool:
    direction = str(req.get("direction") or "").lower()
    if direction != "request" or not _is_wrks_host(req):
        return False
    base = _wrks_path_base(req).rstrip("/") or "/"
    return base in {p.rstrip("/") for p in _UPLOAD_PATHS}


def is_wrks_file_upload_response(req: Dict[str, Any]) -> bool:
    direction = str(req.get("direction") or "").lower()
    if direction != "response" or not _is_wrks_host(req):
        return False
    base = _wrks_path_base(req).rstrip("/") or "/"
    return base in {p.rstrip("/") for p in _UPLOAD_PATHS}


def _is_openai_upload_path(req: Dict[str, Any]) -> bool:
    return (_wrks_path_base(req).rstrip("/") or "/") == _OPENAI_UPLOAD_PATH.rstrip("/")


def _remember_wrks_file_binding(
    upload_id: str,
    file_name: str = "",
    size_bytes: int = 0,
    mime_type: str = "",
) -> None:
    uid = str(upload_id or "").strip()
    if not uid:
        return
    key = uid.lower()
    if key not in _WRKS_FILE_INDEX:
        _WRKS_FILE_ORDER.append(key)
        while len(_WRKS_FILE_ORDER) > _WRKS_FILE_LIMIT:
            oldest = _WRKS_FILE_ORDER.popleft()
            _WRKS_FILE_INDEX.pop(oldest, None)
    existing = _WRKS_FILE_INDEX.get(key, {})
    name = str(file_name or existing.get("file_name") or "").strip()
    mime = str(mime_type or existing.get("mime_type") or "").strip()
    try:
        size = int(size_bytes or existing.get("size_bytes") or 0)
    except Exception:
        size = int(existing.get("size_bytes") or 0)
    _WRKS_FILE_INDEX[key] = {
        "upload_id": uid,
        "file_name": name,
        "size_bytes": size,
        "mime_type": mime,
    }


def _resolve_wrks_file_ref(upload_id: str) -> Optional[Dict[str, Any]]:
    uid = str(upload_id or "").strip()
    if not uid:
        return None
    cached = _WRKS_FILE_INDEX.get(uid.lower())
    if cached:
        return dict(cached)
    if _fum.get_saved_attachment(uid) or _fum.get_registered_upload_filename(uid):
        return {
            "upload_id": uid,
            "file_name": _fum.get_registered_upload_filename(uid),
            "size_bytes": 0,
            "mime_type": "",
        }
    legacy = _fum.lookup_upload_id_by_registered_server_uuid(uid)
    if legacy:
        return {
            "upload_id": legacy,
            "file_name": _fum.get_registered_upload_filename(legacy),
            "size_bytes": 0,
            "mime_type": "",
        }
    return None


def _parse_upload_response_items(payload: bytes) -> List[Dict[str, Any]]:
    if not payload:
        return []
    text = _safe_utf8(payload)
    items: List[Dict[str, Any]] = []

    def add_item(
        server_id: str,
        file_name: str = "",
        mime_type: str = "",
        size_bytes: int = 0,
    ) -> None:
        sid = str(server_id or "").strip()
        if not sid:
            return
        items.append(
            {
                "server_id": sid,
                "file_name": str(file_name or "").strip(),
                "mime_type": str(mime_type or "").strip(),
                "size_bytes": int(size_bytes or 0),
            }
        )

    try:
        obj = json.loads(text)
    except Exception:
        obj = None

    if isinstance(obj, dict):
        data = obj.get("data")
        rows: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            file_row = data.get("file")
            if isinstance(file_row, dict):
                rows.append(file_row)
            if data.get("gptFileId") or data.get("imageUrl") or data.get("fileId"):
                rows.append(data)
            files = data.get("files")
            if isinstance(files, list):
                for row in files:
                    if isinstance(row, dict):
                        rows.append(row)
        elif isinstance(data, list):
            for row in data:
                if isinstance(row, dict):
                    rows.append(row)
        # Top-level file / files (Agent API /ai/file*)
        if isinstance(obj.get("file"), dict):
            rows.append(obj["file"])
        if obj.get("gptFileId") or obj.get("imageUrl") or obj.get("fileId"):
            rows.append(obj)
        files_top = obj.get("files")
        if isinstance(files_top, list):
            for row in files_top:
                if isinstance(row, dict):
                    rows.append(row)
        for row in rows:
            gpt_id = str(
                row.get("gptFileId")
                or row.get("gpt_file_id")
                or row.get("fileId")
                or row.get("file_id")
                or ""
            ).strip()
            image_url = str(row.get("imageUrl") or row.get("image_url") or "").strip()
            name = str(row.get("name") or row.get("file_name") or row.get("filename") or "").strip()
            mime = str(row.get("mime_type") or row.get("mimeType") or row.get("content_type") or "").strip()
            if gpt_id:
                add_item(gpt_id, name, mime)
                continue
            m = _DALLE_URL_TOKEN_RE.search(image_url)
            if m:
                add_item(m.group(1), name or f"{m.group(1)}.png", mime or "image/png")

    if not items:
        gpt_ids = _GPT_FILE_ID_JSON_RE.findall(text)
        names = [_decode_json_string(n) for n in _FILE_NAME_JSON_RE.findall(text)]
        dalle_tokens = [m.group(1) for m in _DALLE_IMAGE_URL_JSON_RE.finditer(text)]
        for idx, gpt_id in enumerate(gpt_ids):
            name = names[idx] if idx < len(names) else ""
            add_item(gpt_id, name)
        for idx, token in enumerate(dalle_tokens):
            if any(it.get("server_id") == token for it in items):
                continue
            add_item(token, f"{token}.png", "image/png")
        if not items:
            for fid in _FILE_ID_JSON_RE.findall(text):
                add_item(fid)
    return items


def _is_synthesized_image_name(name: str, server_id: str) -> bool:
    """Image upload responses carry no filename, so we synthesize `<token>.ext`."""
    if not name or not server_id:
        return False
    stem = name.rsplit(".", 1)[0]
    return name == server_id or stem == server_id


def _preferred_file_name(item_name: str, server_id: str, temp_upload_id: str) -> str:
    registered = _fum.get_registered_upload_filename(temp_upload_id)
    if registered and (
        not item_name or _is_synthesized_image_name(item_name, server_id)
    ):
        return registered
    return item_name or registered or ""


def _promote_uploads_to_server_id(
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
        server_id = str(item.get("server_id") or "").strip()
        file_name = _preferred_file_name(
            str(item.get("file_name") or ""),
            server_id,
            temp_upload_id,
        )
        if not temp_upload_id or not server_id:
            promoted.append(row)
            continue

        upload_id = server_id
        if temp_upload_id != server_id:
            if not _fum.rekey_upload_id(temp_upload_id, server_id):
                upload_id = temp_upload_id
            else:
                logger.info(
                    "wrks upload promoted: %s -> %s name=%s",
                    temp_upload_id,
                    server_id,
                    file_name,
                )

        if file_name:
            _fum.register_upload_mapping(upload_id, file_name, policy, allow_unknown_extension=True)

        saved = _fum.get_saved_attachment(upload_id, fallback_name=file_name)
        saved_path = str((saved or {}).get("id") or row.get("saved_path") or "")

        promoted.append(
            {
                "upload_id": upload_id,
                "filename": file_name or upload_id,
                "saved_path": saved_path,
            }
        )
        _remember_wrks_file_binding(
            server_id,
            file_name,
            int(item.get("size_bytes") or 0),
            str(item.get("mime_type") or ""),
        )
    return promoted


def _restore_original_upload_names(items: List[Dict[str, Any]], scope: str) -> None:
    """Image upload responses omit the filename, so recover it from the pending upload.

    Without this the attachment surfaces in the UI as `<token>.png` instead of the
    name the user picked. Pending ids are FIFO, matching how the scope queue claims.
    """
    pending = _fum.peek_pending_uploads_for_scope(scope) if scope else []
    if not pending:
        return
    for idx, item in enumerate(items):
        if idx >= len(pending):
            break
        server_id = str(item.get("server_id") or "").strip()
        name = str(item.get("file_name") or "").strip()
        if name and not _is_synthesized_image_name(name, server_id):
            continue
        original = _fum.get_registered_upload_filename(pending[idx])
        if not original:
            continue
        item["file_name"] = original
        if not original.lower().endswith(".png"):
            # Synthesized image/png no longer matches; let the saved file decide.
            item["mime_type"] = ""


def _finalize_wrks_upload_response(
    req: Dict[str, Any],
    payload: bytes,
    request_id: str,
) -> Optional[Dict[str, Any]]:
    if not is_wrks_file_upload_response(req):
        return None

    items = _parse_upload_response_items(payload)
    if not items:
        logger.debug("wrks upload response: no file metadata request_id=%s", request_id)
        return _fum.build_upload_result(handled=True)

    policy = _fum.collection_policy_from_request(req)
    scope = _wrks_upload_scope(req)
    _restore_original_upload_names(items, scope)
    filenames = [str(it.get("file_name") or it.get("server_id") or "").strip() for it in items]
    saved_files: List[Dict[str, Any]] = []
    result: Dict[str, Any] = _fum.build_upload_result(handled=True)

    if scope:
        result = _fum.finalize_scope_pending_uploads(scope, filenames, policy)
        saved_files = result.get("saved_files") if isinstance(result.get("saved_files"), list) else []
    if not saved_files and request_id:
        result = _fum.finalize_pending_uploads(request_id, filenames, policy)
        saved_files = result.get("saved_files") if isinstance(result.get("saved_files"), list) else []

    promoted = _promote_uploads_to_server_id(items, saved_files, policy)
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
    logger.info(
        "wrks upload response: mapped %d file(s) ids=%s",
        len(promoted or saved_files),
        [str(x.get("upload_id") or "") for x in (promoted or saved_files) if isinstance(x, dict)],
    )
    return result


def normalize_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    direction = str(req.get("direction") or "").lower()
    request_id = str(req.get("request_id") or "").strip()

    if direction == "response":
        return _finalize_wrks_upload_response(req, payload, request_id)

    if not is_wrks_file_upload_request(req) or not payload:
        return None

    content_type = str(req.get("content_type") or "")
    is_multipart = "multipart/form-data" in content_type.lower()
    if not is_multipart:
        if payload[:2] == b"--" and b"Content-Disposition" in payload[:512]:
            is_multipart = True
    if not is_multipart:
        return None

    parts = _fum.parse_multipart(payload, content_type)
    if not parts:
        return None

    file_payloads: List[Tuple[str, bytes]] = []
    for part in parts:
        body = part.get("body", b"")
        if not body:
            continue
        filename = str(part.get("filename") or "").strip()
        if filename or part.get("name") == "file":
            file_payloads.append((filename, body if isinstance(body, bytes) else bytes(body)))

    if not file_payloads:
        return None

    try:
        policy = _fum.collection_policy_from_request(req)
        result = _fum.create_pending_upload_batch_from_files(
            request_id,
            file_payloads,
            "wrks",
            policy,
        )
        if not result.get("handled"):
            return result
        upload_ids = [
            str(x.get("upload_id") or "").strip()
            for x in (result.get("saved_files") or [])
            if isinstance(x, dict)
        ]
        scope_key = _wrks_upload_scope(req)
        if upload_ids:
            _fum.remember_pending_uploads_for_scope(scope_key, upload_ids)
        logger.info(
            "wrks normalize_upload: saved %d file(s) path=%s scope=%s ids=%s",
            len(file_payloads),
            _wrks_path_base(req),
            scope_key,
            upload_ids,
        )
        return result
    except Exception:
        logger.exception("wrks normalize_upload batch save failed")
    return None


def strip_chat_attachments(obj: Dict[str, Any]) -> int:
    """Remove gptFileIds / fileIds / vision image refs before upstream MASK/BLOCK send."""
    if not isinstance(obj, dict):
        return 0
    removed = 0

    for key in (
        "gptFileIds",
        "visionImageGptFileIds",
        "fileIds",
        "imageFileIds",
        "visionImageFileIds",
    ):
        val = obj.get(key)
        if isinstance(val, list) and val:
            removed += len(val)
            obj[key] = []

    for key in ("visionImageGptFileId", "fileId", "imageFileId"):
        vid = obj.get(key)
        if isinstance(vid, str) and vid.strip():
            obj[key] = ""
            removed += 1
        elif isinstance(vid, int):
            obj[key] = None
            removed += 1

    message = obj.get("message")
    if isinstance(message, dict):
        parts = message.get("parts")
        if isinstance(parts, list):
            kept: List[Any] = []
            for part in parts:
                if isinstance(part, dict) and str(part.get("type") or "").lower() == "file":
                    removed += 1
                    continue
                kept.append(part)
            if len(kept) != len(parts):
                message["parts"] = kept

    return removed


def _iter_message_file_parts(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    parts = message.get("parts")
    if not isinstance(parts, list):
        return []
    out: List[Dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if str(part.get("type") or "").lower() != "file":
            continue
        out.append(part)
    return out


def _collect_attachment_refs(obj: Dict[str, Any]) -> List[str]:
    """Chat JSON refs: gptFileIds / fileIds / message.parts[].fileId plus dalle tokens.

    Agent UI v2 attaches files as:
      message.parts: [{type:\"file\", fileId:1999068, filename:\"...pdf\", url:\"vectorstore://...\"}]
    Legacy chat used top-level gptFileIds / [analyzeImageUrl] dalle tokens.
    """
    refs: List[str] = []
    seen: set[str] = set()

    def add_ref(uid: str) -> None:
        uid = str(uid or "").strip()
        if not uid or uid in seen:
            return
        if _GPT_FILE_ID_RE.match(uid) or uid.isdigit() or len(uid) >= 6:
            seen.add(uid)
            refs.append(uid)

    for key in (
        "gptFileIds",
        "visionImageGptFileIds",
        "visionImageGptFileId",
        "fileIds",
        "imageFileIds",
        "fileId",
        "imageFileId",
    ):
        value = obj.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            value = [value]
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, (str, int)):
                add_ref(str(item))

    for part in _iter_message_file_parts(obj):
        fid = part.get("fileId") if part.get("fileId") is not None else part.get("file_id")
        if isinstance(fid, (str, int)):
            add_ref(str(fid))
            name = str(part.get("filename") or part.get("name") or "").strip()
            if name:
                _remember_wrks_file_binding(str(fid), name, 0, str(part.get("mediaType") or ""))

    for token in _collect_image_url_tokens(obj):
        add_ref(token)
    return refs


def _claim_pending_upload_for_server_id(req: Dict[str, Any], server_id: str, file_name: str = "") -> Optional[str]:
    """When upload response tee is missing, bind the next scope-pending upload to chat fileId."""
    sid = str(server_id or "").strip()
    if not sid:
        return None
    if _resolve_wrks_file_ref(sid):
        return sid
    scope = _wrks_upload_scope(req)
    pending = _fum.peek_pending_uploads_for_scope(scope) if scope else []
    if not pending:
        return None
    temp_id = str(pending[0] or "").strip()
    if not temp_id:
        return None
    # Consume one pending id from the FIFO queue.
    claimed = _fum.claim_pending_uploads_for_scope(scope, 1) if hasattr(_fum, "claim_pending_uploads_for_scope") else [temp_id]
    if claimed:
        temp_id = str(claimed[0] or temp_id).strip()
    upload_id = sid
    if temp_id != sid:
        if not _fum.rekey_upload_id(temp_id, sid):
            upload_id = temp_id
        else:
            logger.info("wrks chat claim promoted: %s -> %s name=%s", temp_id, sid, file_name)
    if file_name:
        _fum.register_upload_mapping(upload_id, file_name, None, allow_unknown_extension=True)
    _remember_wrks_file_binding(sid, file_name)
    return upload_id


def _collect_image_url_tokens(obj: Dict[str, Any]) -> List[str]:
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except Exception:
        return []
    tokens: List[str] = []
    seen: set[str] = set()
    for match in _DALLE_URL_TOKEN_RE.finditer(text.replace("\\/", "/")):
        token = match.group(1).strip()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def build_policy_attachments(
    req: Dict[str, Any],
    obj: Dict[str, Any],
    payload: bytes,
) -> List[Dict[str, Any]]:
    """Build file_block_filter attachments from chat-turn file/image references."""
    del payload
    refs = _collect_attachment_refs(obj)
    if not refs:
        return [{"id": "", "size": 0, "name": "", "mime_type": ""}]

    # Prefer filenames carried on message.parts for Agent UI v2.
    part_names: Dict[str, str] = {}
    for part in _iter_message_file_parts(obj):
        fid = part.get("fileId") if part.get("fileId") is not None else part.get("file_id")
        if fid is None:
            continue
        name = str(part.get("filename") or part.get("name") or "").strip()
        if name:
            part_names[str(fid)] = name

    out: List[Dict[str, Any]] = []
    for server_id in refs:
        name_hint = part_names.get(server_id, "")
        resolved = _resolve_wrks_file_ref(server_id)
        upload_id = server_id
        if not resolved:
            claimed = _claim_pending_upload_for_server_id(req, server_id, name_hint)
            if claimed:
                upload_id = claimed
                resolved = _resolve_wrks_file_ref(server_id) or _resolve_wrks_file_ref(claimed)
        if resolved and str(resolved.get("upload_id") or "").strip():
            upload_id = str(resolved.get("upload_id") or "").strip()

        name = name_hint or str((resolved or {}).get("file_name") or "")
        mime = str((resolved or {}).get("mime_type") or "")
        try:
            size = int((resolved or {}).get("size_bytes") or 0)
        except Exception:
            size = 0

        out.append(_finalize_policy_attachment(upload_id or server_id, name, mime, size))

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
        if req_size > 0:
            size = req_size
        else:
            size = int(saved.get("size") or 0)
        display_name = name or str(saved.get("name") or "")
        if display_name == uid or (_GPT_FILE_ID_RE.match(display_name) and name):
            display_name = name or _fum.get_registered_upload_filename(uid) or display_name
        return {
            "id": str(saved.get("id") or ""),
            "size": size,
            "name": display_name,
            "mime_type": mime or str(saved.get("mime_type") or ""),
        }

    if uid:
        expected = _fum.expected_saved_path(uid)
        logger.warning(
            "wrks policy attachment not on disk yet: uid=%s expected=%s",
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
