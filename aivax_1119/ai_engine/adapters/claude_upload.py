"""
Claude file upload and policy attachment assembly.

Flow (content.pcap):
  POST .../conversations/{id}/wiggle/upload-file  (multipart file bytes)
  response JSON: file_uuid, file_name, size_bytes, file_kind
  POST .../chat_conversations/{id}/completion     files: ["<file_uuid>"]

Adapter owns upload + attachment resolution; file_upload_manager is storage only.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

import file_upload_manager as _fum

logger = logging.getLogger(__name__)

POLICY_ATTACHMENTS_FINAL = True

_FILE_CONTENT_BLOCK_TYPES = frozenset({
    "document",
    "file",
    "image",
    "attachment",
    "pdf",
    "upload",
    "file_attachment",
})


def _is_file_content_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    btype = str(block.get("type") or "").lower()
    if btype in _FILE_CONTENT_BLOCK_TYPES:
        return True
    for key in ("file_uuid", "fileUuid", "document_uuid", "document_id", "upload_id"):
        if str(block.get(key) or "").strip():
            return True
    source = block.get("source")
    if isinstance(source, dict):
        if str(source.get("file_uuid") or source.get("upload_id") or "").strip():
            return True
        if str(source.get("type") or "").lower() in _FILE_CONTENT_BLOCK_TYPES:
            return True
    return False


def strip_completion_attachments(obj: Any) -> int:
    """Remove completion file refs before upstream MASK/BLOCK send."""
    if not isinstance(obj, dict):
        return 0
    removed = 0

    files = obj.get("files")
    if isinstance(files, list) and files:
        removed += len(files)
        obj["files"] = []

    for key in ("file_uuids", "attachments", "attachment_ids"):
        val = obj.get(key)
        if isinstance(val, list) and val:
            removed += len(val)
            obj[key] = []

    for msg_key in ("messages", "chat_messages"):
        messages = obj.get(msg_key)
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            new_content: List[Any] = []
            for block in content:
                if _is_file_content_block(block):
                    removed += 1
                    continue
                new_content.append(block)
            if len(new_content) != len(content):
                msg["content"] = new_content

    return removed

_CLAUDE_FILE_UUID_INDEX: Dict[str, Dict[str, Any]] = {}
_CLAUDE_FILE_UUID_ORDER: deque[str] = deque()
_CLAUDE_FILE_UUID_LIMIT = 512

_FILE_UUID_RE = re.compile(r'"file_uuid"\s*:\s*"([0-9a-fA-F-]{36})"', re.IGNORECASE)
_FILE_NAME_JSON_RE = re.compile(r'"file_name"\s*:\s*"((?:\\.|[^"\\])*)"', re.IGNORECASE)
_SIZE_BYTES_RE = re.compile(r'"size_bytes"\s*:\s*(\d+)', re.IGNORECASE)
_MIME_TYPE_RE = re.compile(r'"(?:mime_type|content_type)"\s*:\s*"([^"]+)"', re.IGNORECASE)
_CONV_SCOPE_RE = re.compile(
    r"/(?:chat_)?conversations/([0-9a-fA-F-]{36})/",
    re.IGNORECASE,
)
_ORG_SCOPE_RE = re.compile(
    r"/api/organizations/([0-9a-fA-F-]{36})/",
    re.IGNORECASE,
)
_DIRECT_UPLOAD_RE = re.compile(
    r"^/api/[0-9a-fA-F-]{36}/upload/?$",
    re.IGNORECASE,
)
_CLAIMED_RPC_UPLOADS: set[str] = set()


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


def _is_claude_upload_path(path: str) -> bool:
    low = str(path or "").split("?", 1)[0].lower()
    return (
        "/wiggle/upload-file" in low
        or low.endswith("/upload-file")
        or bool(_DIRECT_UPLOAD_RE.fullmatch(low))
    )


def _claude_client_scope(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    client = str(meta.get("client_ip") or meta.get("client_host") or "").strip()
    return f"claude:client:{client}" if client else ""


def _claude_upload_scope_from_path(path: str) -> str:
    if not path:
        return ""
    low = path.split("?", 1)[0]
    m = _CONV_SCOPE_RE.search(low)
    if m:
        return f"conv:{m.group(1).lower()}"
    m = _ORG_SCOPE_RE.search(low)
    if m:
        return f"org:{m.group(1).lower()}"
    return ""


def _is_multipart_upload(req: Dict[str, Any], payload: bytes) -> bool:
    content_type = str(req.get("content_type") or "")
    if "multipart/form-data" in content_type.lower():
        return True
    return bool(payload[:2] == b"--" and b"Content-Disposition" in payload[:512])


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
        uid = str(row.get("file_uuid") or row.get("uuid") or "").strip()
        if not uid:
            return
        name = str(row.get("file_name") or row.get("name") or "").strip()
        try:
            size = int(row.get("size_bytes") or row.get("size") or 0)
        except Exception:
            size = 0
        mime = str(row.get("mime_type") or row.get("content_type") or "").strip()
        items.append(
            {
                "file_uuid": uid,
                "file_name": name,
                "size_bytes": size,
                "mime_type": mime,
            }
        )

    if isinstance(obj, dict):
        add_item(obj)
        for key in ("file", "files", "uploaded_file", "uploaded_files"):
            nested = obj.get(key)
            if isinstance(nested, dict):
                add_item(nested)
            elif isinstance(nested, list):
                for row in nested:
                    if isinstance(row, dict):
                        add_item(row)

    if not items:
        uuids = _FILE_UUID_RE.findall(text)
        names = [_decode_json_string(n) for n in _FILE_NAME_JSON_RE.findall(text)]
        sizes = [int(x) for x in _SIZE_BYTES_RE.findall(text)]
        mimes = _MIME_TYPE_RE.findall(text)
        for idx, uid in enumerate(uuids):
            name = names[idx] if idx < len(names) else ""
            size = sizes[idx] if idx < len(sizes) else 0
            mime = mimes[idx] if idx < len(mimes) else ""
            items.append(
                {
                    "file_uuid": uid,
                    "file_name": name,
                    "size_bytes": size,
                    "mime_type": mime,
                }
            )
    return items


def _remember_claude_file_binding(
    file_uuid: str,
    file_name: str = "",
    size_bytes: int = 0,
    mime_type: str = "",
) -> None:
    uid = str(file_uuid or "").strip().lower()
    if not uid:
        return
    if uid not in _CLAUDE_FILE_UUID_INDEX:
        _CLAUDE_FILE_UUID_ORDER.append(uid)
        while len(_CLAUDE_FILE_UUID_ORDER) > _CLAUDE_FILE_UUID_LIMIT:
            oldest = _CLAUDE_FILE_UUID_ORDER.popleft()
            _CLAUDE_FILE_UUID_INDEX.pop(oldest, None)
    _CLAUDE_FILE_UUID_INDEX[uid] = {
        "upload_id": uid,
        "file_name": str(file_name or "").strip(),
        "size_bytes": int(size_bytes or 0),
        "mime_type": str(mime_type or "").strip(),
    }


def _resolve_claude_file_ref(server_uuid: str) -> Optional[Dict[str, Any]]:
    key = str(server_uuid or "").strip().lower()
    if not key:
        return None
    cached = _CLAUDE_FILE_UUID_INDEX.get(key)
    if cached:
        return dict(cached)
    if _fum.get_saved_attachment(server_uuid) or _fum.get_registered_upload_filename(server_uuid):
        return {
            "upload_id": server_uuid,
            "file_name": _fum.get_registered_upload_filename(server_uuid),
            "size_bytes": 0,
            "mime_type": "",
        }
    legacy = _fum.lookup_upload_id_by_registered_server_uuid(server_uuid)
    if legacy:
        return {
            "upload_id": legacy,
            "file_name": _fum.get_registered_upload_filename(legacy),
            "size_bytes": 0,
            "mime_type": "",
        }
    return None


def _promote_uploads_to_server_uuid(
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
        file_uuid = str(item.get("file_uuid") or "").strip()
        file_name = str(item.get("file_name") or _fum.get_registered_upload_filename(temp_upload_id) or "")
        if not temp_upload_id or not file_uuid:
            promoted.append(row)
            continue

        upload_id = file_uuid
        if temp_upload_id != file_uuid:
            if not _fum.rekey_upload_id(temp_upload_id, file_uuid):
                upload_id = temp_upload_id
            else:
                logger.info(
                    "claude upload promoted: %s -> %s name=%s",
                    temp_upload_id,
                    file_uuid,
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
        _remember_claude_file_binding(
            file_uuid,
            file_name,
            int(item.get("size_bytes") or 0),
            str(item.get("mime_type") or ""),
        )
    return promoted


def _finalize_claude_upload_response(
    req: Dict[str, Any],
    payload: bytes,
    request_id: str,
) -> Dict[str, Any]:
    policy = _fum.collection_policy_from_request(req)
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")
    scope = _claude_upload_scope_from_path(path)
    items = _parse_upload_response_items(payload)
    if not items:
        logger.debug("claude upload response: no file metadata request_id=%s", request_id)
        return _fum.build_upload_result(handled=True)

    filenames = [str(it.get("file_name") or it.get("file_uuid") or "").strip() for it in items]
    saved_files: List[Dict[str, Any]] = []
    result: Dict[str, Any] = _fum.build_upload_result(handled=True)

    # Prefer request_id first: upload body tee uses "{rid}-up-{hex}", response keeps base rid.
    # Scope-first + pop-all discarded sibling files when several uploads raced (UI showed one).
    if request_id:
        result = _fum.finalize_pending_uploads(request_id, filenames, policy)
        saved_files = result.get("saved_files") if isinstance(result.get("saved_files"), list) else []
        if saved_files and scope:
            _fum.forget_pending_uploads_from_scope(
                scope,
                [str(x.get("upload_id") or "").strip() for x in saved_files if isinstance(x, dict)],
            )
    if not saved_files and scope:
        result = _fum.finalize_scope_pending_uploads(scope, filenames, policy)
        saved_files = result.get("saved_files") if isinstance(result.get("saved_files"), list) else []

    promoted = _promote_uploads_to_server_uuid(items, saved_files, policy)
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
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    path = str(meta.get("path") or "")

    if direction == "response":
        if not _is_claude_upload_path(path):
            return None
        return _finalize_claude_upload_response(req, payload, request_id)

    if direction != "request" or not payload:
        return None
    if not _is_claude_upload_path(path):
        return None
    if not _is_multipart_upload(req, payload):
        return None

    content_type = str(req.get("content_type") or "")
    parts = _fum.parse_multipart(payload, content_type)
    if not parts:
        return None

    policy = _fum.collection_policy_from_request(req)
    file_payloads = [
        (str(part.get("filename") or ""), part.get("body", b""))
        for part in parts
        if part.get("filename") and part.get("body", b"")
    ]
    if not file_payloads:
        return None

    try:
        result = _fum.create_pending_upload_batch_from_files(
            request_id,
            file_payloads,
            "claude",
            policy,
        )
    except Exception:
        logger.exception("claude normalize_upload batch save failed")
        return None

    if not result.get("handled"):
        return result

    logger.info("claude normalize_upload: saved %d file(s)", len(file_payloads))
    saved_files = result.get("saved_files") or []
    upload_ids = [
        str(x.get("upload_id") or "").strip()
        for x in saved_files
        if isinstance(x, dict)
    ]
    scope = _claude_upload_scope_from_path(path)
    if upload_ids:
        _fum.remember_pending_uploads_for_scope(scope or "claude:orphan", upload_ids)
        client_scope = _claude_client_scope(req)
        if client_scope:
            # The current app's /api/{uuid}/upload response is compact protobuf
            # and does not expose the old file_uuid JSON. PerformAction follows
            # on the same client, so retain a scoped one-shot binding as well.
            _fum.remember_pending_uploads_for_scope(client_scope, upload_ids)
    return result


def _collect_completion_file_refs(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Use completion JSON `files` only (pcap: string UUID list)."""
    refs: List[Dict[str, Any]] = []
    seen: set[str] = set()
    files = obj.get("files")
    if not isinstance(files, list):
        return refs
    for item in files:
        if isinstance(item, str):
            uid = item.strip()
            if uid and uid not in seen:
                seen.add(uid)
                refs.append({"file_uuid": uid})
            continue
        if not isinstance(item, dict):
            continue
        uid = str(item.get("file_uuid") or item.get("uuid") or item.get("id") or "").strip()
        if uid and uid not in seen:
            seen.add(uid)
            refs.append(item)
    return refs


def build_policy_attachments(
    req: Dict[str, Any],
    obj: Dict[str, Any],
    payload: bytes,
) -> List[Dict[str, Any]]:
    """Build file_block_filter attachments strictly from completion `files[]`."""
    refs = _collect_completion_file_refs(obj)
    if not refs:
        return [{"id": "", "size": 0, "name": "", "mime_type": ""}]

    out: List[Dict[str, Any]] = []
    for ref in refs:
        server_uuid = str(
            ref.get("file_uuid")
            or ref.get("fileUuid")
            or ref.get("uuid")
            or ref.get("id")
            or ""
        ).strip()
        if not server_uuid:
            continue

        resolved = _resolve_claude_file_ref(server_uuid)
        upload_id = str(server_uuid).strip()
        if resolved and str(resolved.get("upload_id") or "").strip():
            upload_id = str(resolved.get("upload_id") or "").strip()

        name = str(
            ref.get("file_name")
            or ref.get("name")
            or ref.get("filename")
            or (resolved or {}).get("file_name")
            or ""
        )
        mime = str(
            ref.get("mime_type")
            or ref.get("mimeType")
            or ref.get("content_type")
            or (resolved or {}).get("mime_type")
            or ""
        )
        try:
            size = int(
                ref.get("size_bytes")
                or ref.get("size")
                or (resolved or {}).get("size_bytes")
                or 0
            )
        except Exception:
            size = 0

        out.append(_finalize_policy_attachment(upload_id or server_uuid, name, mime, size))

    return out if out else [{"id": "", "size": 0, "name": "", "mime_type": ""}]


def save_perform_action_inline_files(
    files: Sequence[Tuple[str, str, bytes]],
) -> List[Dict[str, Any]]:
    """Store file bytes that arrived inside PerformAction."""
    out: List[Dict[str, Any]] = []
    for name, mime, content in files:
        filename = str(name or "").strip()
        body = bytes(content or b"")
        if not filename or not body:
            continue
        upload_id = "claude-inline-" + uuid.uuid4().hex[:16]
        saved = _fum.save_inline_upload(upload_id, filename, body)
        if not saved:
            continue
        logger.info(
            "claude inline file saved name=%s bytes=%d id=%s",
            filename[:80],
            len(body),
            upload_id,
        )
        out.append(
            _finalize_policy_attachment(upload_id, filename, str(mime or ""), len(body))
        )
    return out


def build_rpc_policy_attachments(
    req: Dict[str, Any],
    file_refs: List[str],
    has_file_extras: bool,
) -> List[Dict[str, Any]]:
    """Resolve new PerformAction file refs, with a same-client upload fallback."""
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in file_refs:
        uid = str(raw or "").strip()
        if not uid or uid.lower() in seen:
            continue
        seen.add(uid.lower())
        resolved = _resolve_claude_file_ref(uid)
        if resolved:
            out.append(
                _finalize_policy_attachment(
                    str(resolved.get("upload_id") or uid),
                    str(resolved.get("file_name") or ""),
                    str(resolved.get("mime_type") or ""),
                    int(resolved.get("size_bytes") or 0),
                )
            )

    # /api/{uuid}/upload no longer returns the classic JSON file_uuid. Only
    # claim uploads when the action actually has file/multimodal extras.
    if has_file_extras and not out:
        scope = _claude_client_scope(req)
        pending = _fum.claim_pending_uploads_for_scope(scope, 8) if scope else []
        for uid in pending:
            if uid in _CLAIMED_RPC_UPLOADS:
                continue
            saved = _fum.get_saved_attachment(
                uid, fallback_name=_fum.get_registered_upload_filename(uid)
            )
            if not saved:
                continue
            out.append(
                {
                    "id": str(saved.get("id") or ""),
                    "size": int(saved.get("size") or 0),
                    "name": str(
                        saved.get("name")
                        or _fum.get_registered_upload_filename(uid)
                        or ""
                    ),
                    "mime_type": str(saved.get("mime_type") or ""),
                }
            )
            _CLAIMED_RPC_UPLOADS.add(uid)
        if len(_CLAIMED_RPC_UPLOADS) > 512:
            _CLAIMED_RPC_UPLOADS.clear()

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
        return {
            "id": str(saved.get("id") or ""),
            "size": size,
            "name": name or _fum.get_registered_upload_filename(uid),
            "mime_type": mime,
        }

    if uid:
        expected = _fum.expected_saved_path(uid)
        logger.warning(
            "claude policy attachment not on disk yet: uid=%s expected=%s",
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


def claude_upload_scope_from_path(path: str) -> str:
    """Public helper for tests and claude_engine path id extraction."""
    return _claude_upload_scope_from_path(path)


# Public entry points share file_upload_manager.UPLOAD_STATE_LOCK so the
# policy loop can run normalize/persist on a worker without racing extract.
_fum.bind_upload_state_lock(globals())
