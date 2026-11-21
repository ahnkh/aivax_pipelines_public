import hashlib
import json
import os
import re
import threading
import time
import logging
import heapq
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

_UPLOAD_SAVE_DIR = os.environ.get(
    "AIVAX_ATTACH_TMPFS",
    "/run/aivax/attachments",
)
# Loaded from policy_signal/aivax_policy.json (Inline + Mirror).
_FILE_COLLECTION_META: Dict[str, str] = {}
# upload_id → deny reason (extension_denied / size_exceeded) for chat-turn BLOCK.
_COLLECTION_DENIED: Dict[str, Tuple[str, float]] = {}
_COLLECTION_DENIED_TTL = float(os.environ.get("AIVAX_UPLOAD_TTL_SECONDS", "600") or "600")
_upload_id_map: Dict[str, Tuple[str, float]] = {}
# Grok fileMetadataId 등 서버 측 UUID → 내부 upload_id (디스크 파일명은 upload_id 기준 유지)
_upload_server_uuid_alias: Dict[str, str] = {}
# Gemini contrib_service/ttl_1d segment → upload_id
_upload_ttl_segment_alias: Dict[str, str] = {}
# upload_id → saved_path: race condition 대응 (actual upload이 init response보다 먼저 도착 시 rename용)
_saved_file_paths: Dict[str, str] = {}
_saved_file_sizes: Dict[str, int] = {}
_pending_upload_ids_by_request: Dict[str, Tuple[List[str], float]] = {}
_pending_upload_ids_by_scope: Dict[str, Tuple[List[str], float]] = {}
_upload_expiry_heap: List[Tuple[float, str, float]] = []
_pending_request_expiry_heap: List[Tuple[float, str, float]] = []
_scope_pending_expiry_heap: List[Tuple[float, str, float]] = []
_PENDING_TTL = float(os.environ.get("AIVAX_UPLOAD_TTL_SECONDS", "600") or "600")
_SCOPE_PENDING_LIMIT = 2000
_MAX_IDS_PER_SCOPE = 64
_MAP_LIMIT = 5000
_CLEANUP_INTERVAL = 1.0
_next_cleanup_at = 0.0
_DEFAULT_FILE_CONTROL_MAXSIZE = 10 * 1024 * 1024
_MAX_FILE_CONTROL_MAXSIZE = 50 * 1024 * 1024
MAX_FILE_CONTROL_MAXSIZE = _MAX_FILE_CONTROL_MAXSIZE

logger = logging.getLogger(__name__)

# Claude 등: 등록 파일명이 "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.png" 처럼 UUID+확장자일 수 있음
_STD_UUID_TOKEN_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _shared_upload_metadata_path(upload_id: str) -> str:
    """Return a process-independent metadata path in the attachment tmpfs."""
    uid = str(upload_id or "").strip()
    if not uid:
        return ""
    digest = hashlib.sha256(uid.encode("utf-8", errors="ignore")).hexdigest()
    return os.path.join(os.path.abspath(_UPLOAD_SAVE_DIR), ".aivax-meta", f"{digest}.json")


def _write_shared_upload_metadata(upload_id: str, filename: str) -> None:
    """Persist upload-id → filename so SHM workers resolve identical metadata."""
    uid = str(upload_id or "").strip()
    name = str(filename or "").strip()
    path = _shared_upload_metadata_path(uid)
    if not path or not name:
        return
    meta_dir = os.path.dirname(path)
    tmp_path = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        os.makedirs(meta_dir, mode=0o700, exist_ok=True)
        payload = json.dumps(
            {"upload_id": uid, "filename": name, "updated_at": time.time()},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
    except OSError:
        logger.exception("upload shared metadata write failed: id=%s", uid[:64])
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _read_shared_upload_filename(upload_id: str) -> str:
    uid = str(upload_id or "").strip()
    path = _shared_upload_metadata_path(uid)
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            row = json.load(fh)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(row, dict) or str(row.get("upload_id") or "") != uid:
        return ""
    return str(row.get("filename") or "").strip()


def _remove_shared_upload_metadata(upload_id: str) -> None:
    path = _shared_upload_metadata_path(upload_id)
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("upload shared metadata unlink failed: id=%s", str(upload_id)[:64])


def _canonical_uuid_from_upload_filename(name: str) -> str:
    """basename 또는 확장자를 뗀 stem이 표준 UUID 형태면 소문자 UUID 토큰을 반환."""
    if not name:
        return ""
    base_name = os.path.basename(str(name).replace("\\", "/")).strip()
    if not base_name:
        return ""
    stem, _ext = os.path.splitext(base_name)
    for candidate in (stem, base_name):
        if _STD_UUID_TOKEN_RE.match(candidate):
            return candidate.lower()
    return ""


def _schedule_upload_expiry(upload_id: str, ts: float) -> None:
    heapq.heappush(_upload_expiry_heap, (ts + _PENDING_TTL, upload_id, ts))


def _schedule_pending_request_expiry(request_id: str, ts: float) -> None:
    heapq.heappush(_pending_request_expiry_heap, (ts + _PENDING_TTL, request_id, ts))


def _unlink_saved_upload(upload_id: str, reason: str) -> None:
    saved_path = str(_saved_file_paths.pop(upload_id, "") or "")
    _saved_file_sizes.pop(upload_id, None)
    _remove_shared_upload_metadata(upload_id)
    if not saved_path:
        return
    try:
        os.remove(saved_path)
        logger.info("upload tmpfs unlinked: id=%s reason=%s path=%s", upload_id, reason, saved_path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("upload tmpfs unlink failed: id=%s reason=%s path=%s", upload_id, reason, saved_path)


def _cleanup_expired_uploads(now: float) -> None:
    while _upload_expiry_heap and _upload_expiry_heap[0][0] <= now:
        _, upload_id, ts = heapq.heappop(_upload_expiry_heap)
        entry = _upload_id_map.get(upload_id)
        if not entry or entry[1] != ts:
            continue
        _upload_id_map.pop(upload_id, None)
        _unlink_saved_upload(upload_id, "ttl")
        for seg, uid in list(_upload_ttl_segment_alias.items()):
            if uid == upload_id:
                _upload_ttl_segment_alias.pop(seg, None)


def _cleanup_expired_pending_requests(now: float) -> None:
    while _pending_request_expiry_heap and _pending_request_expiry_heap[0][0] <= now:
        _, request_id, ts = heapq.heappop(_pending_request_expiry_heap)
        entry = _pending_upload_ids_by_request.get(request_id)
        if not entry or entry[1] != ts:
            continue
        _pending_upload_ids_by_request.pop(request_id, None)


def _schedule_scope_pending_expiry(scope_key: str, ts: float) -> None:
    heapq.heappush(_scope_pending_expiry_heap, (ts + _PENDING_TTL, scope_key, ts))


def _cleanup_expired_scope_pending(now: float) -> None:
    while _scope_pending_expiry_heap and _scope_pending_expiry_heap[0][0] <= now:
        _, scope_key, ts = heapq.heappop(_scope_pending_expiry_heap)
        entry = _pending_upload_ids_by_scope.get(scope_key)
        if not entry or entry[1] != ts:
            continue
        _pending_upload_ids_by_scope.pop(scope_key, None)


def _evict_over_limit_scope_pending() -> None:
    while len(_pending_upload_ids_by_scope) > _SCOPE_PENDING_LIMIT and _scope_pending_expiry_heap:
        _, scope_key, ts = heapq.heappop(_scope_pending_expiry_heap)
        entry = _pending_upload_ids_by_scope.get(scope_key)
        if not entry or entry[1] != ts:
            continue
        _pending_upload_ids_by_scope.pop(scope_key, None)


def _evict_over_limit_uploads() -> None:
    while len(_upload_id_map) > _MAP_LIMIT and _upload_expiry_heap:
        _, upload_id, ts = heapq.heappop(_upload_expiry_heap)
        entry = _upload_id_map.get(upload_id)
        if not entry or entry[1] != ts:
            continue
        _upload_id_map.pop(upload_id, None)
        _unlink_saved_upload(upload_id, "registry_limit")


def _evict_over_limit_pending_requests() -> None:
    while len(_pending_upload_ids_by_request) > _MAP_LIMIT and _pending_request_expiry_heap:
        _, request_id, ts = heapq.heappop(_pending_request_expiry_heap)
        entry = _pending_upload_ids_by_request.get(request_id)
        if not entry or entry[1] != ts:
            continue
        _pending_upload_ids_by_request.pop(request_id, None)


def _build_saved_file_entry(upload_id: str, filename: str, saved_path: str) -> Dict[str, Any]:
    return {
        "upload_id": upload_id,
        "filename": filename,
        "saved_path": saved_path,
        "size": _saved_file_sizes.get(upload_id, 0),
    }


def build_upload_result(
    upload_id: str = "",
    upload_filename: str = "",
    saved_path: str = "",
    saved_files: Optional[List[Dict[str, str]]] = None,
    handled: bool = False,
    skipped: bool = False,
    skip_reason: str = "",
) -> Dict[str, Any]:
    normalized_saved_files = saved_files if isinstance(saved_files, list) else []
    effective_filename = upload_filename
    if not effective_filename and normalized_saved_files:
        effective_filename = str(normalized_saved_files[0].get("filename") or "")
    if not saved_path and normalized_saved_files:
        saved_path = str(normalized_saved_files[0].get("saved_path") or "")
    return {
        "handled": handled,
        "upload_id": upload_id,
        "upload_filename": effective_filename,
        "filename": effective_filename,
        "saved_path": saved_path,
        "saved_files": normalized_saved_files,
        "skipped": skipped,
        "skip_reason": skip_reason,
    }


def build_pending_upload_result(upload_id: str = "", upload_filename: str = "") -> Dict[str, Any]:
    return build_upload_result(upload_id=upload_id, upload_filename=upload_filename, handled=False)


def build_skipped_upload_result(upload_id: str = "", upload_filename: str = "", reason: str = "") -> Dict[str, Any]:
    return build_upload_result(
        upload_id=upload_id,
        upload_filename=upload_filename,
        handled=True,
        skipped=True,
        skip_reason=reason,
    )


def _build_saved_upload_result(saved_files: List[Dict[str, str]]) -> Dict[str, Any]:
    first_entry = saved_files[0] if saved_files else {}
    return build_upload_result(
        upload_id=str(first_entry.get("upload_id") or ""),
        upload_filename=str(first_entry.get("filename") or ""),
        saved_path=str(first_entry.get("saved_path") or ""),
        saved_files=saved_files,
        handled=bool(saved_files),
    )


def set_file_collection_policy_meta(meta: Optional[Dict[str, str]]) -> None:
    """Install snapshot from aivax_policy.json (file_allow_extension / file_control_maxsize)."""
    global _FILE_COLLECTION_META
    _FILE_COLLECTION_META = {
        str(k): str(v)
        for k, v in dict(meta or {}).items()
        if str(k).strip() and str(v).strip()
    }


def get_file_collection_policy_meta() -> Dict[str, str]:
    return dict(_FILE_COLLECTION_META)


def mark_collection_denied(upload_id: str, reason: str) -> None:
    uid = str(upload_id or "").strip()
    why = str(reason or "").strip() or "collection_policy"
    if not uid:
        return
    _COLLECTION_DENIED[uid] = (why, time.monotonic())


def collection_denied_reason(upload_id: str) -> str:
    uid = str(upload_id or "").strip()
    if not uid:
        return ""
    entry = _COLLECTION_DENIED.get(uid)
    if not entry:
        return ""
    why, ts = entry
    if time.monotonic() - float(ts) > _COLLECTION_DENIED_TTL:
        _COLLECTION_DENIED.pop(uid, None)
        return ""
    return str(why or "")


def _parse_allow_extensions(raw: Any) -> Optional[Set[str]]:
    text = str(raw or "").strip()
    if not text:
        return None
    exts = {
        item.strip().lower().lstrip(".")
        for item in text.split(",")
        if item.strip().lstrip(".")
    }
    return exts or None


def _parse_file_control_maxsize(raw: Any) -> int:
    try:
        value = int(str(raw or "").strip())
    except Exception:
        return _DEFAULT_FILE_CONTROL_MAXSIZE
    if value <= 0:
        return _DEFAULT_FILE_CONTROL_MAXSIZE
    if value > _MAX_FILE_CONTROL_MAXSIZE:
        logger.warning("file_control_maxsize clamped: %d -> %d", value, _MAX_FILE_CONTROL_MAXSIZE)
        return _MAX_FILE_CONTROL_MAXSIZE
    return value


def collection_policy_from_request(req: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve allow-ext / maxsize from request meta, else global policy_signal snapshot."""
    meta = req.get("meta") if isinstance(req, dict) and isinstance(req.get("meta"), dict) else {}
    allow_raw = meta.get("file_allow_extension")
    max_raw = meta.get("file_control_maxsize")
    if allow_raw is None or not str(allow_raw).strip():
        allow_raw = _FILE_COLLECTION_META.get("file_allow_extension")
    if max_raw is None or not str(max_raw).strip():
        max_raw = _FILE_COLLECTION_META.get("file_control_maxsize")
    return {
        "allow_extensions": _parse_allow_extensions(allow_raw),
        "maxsize": _parse_file_control_maxsize(max_raw),
    }


def _filename_extension(filename: str) -> str:
    name = os.path.basename(str(filename or "")).strip()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[1].strip().lower()


def _upload_saved_basename(upload_id: str, filename: str = "") -> str:
    """디스크 저장명 = upload_id만 (확장자 없음). 정책 API attachments.name과 동일."""
    return os.path.basename(str(upload_id or "").strip()) or str(upload_id or "")[:48] or uuid.uuid4().hex


def _retarget_saved_path_for_filename(upload_id: str, filename: str) -> None:
    """등록 파일명 변경 시에도 디스크 basename은 upload_id(확장자 없음)로 통일."""
    saved_path = str(_saved_file_paths.get(upload_id) or "")
    if not upload_id or not saved_path or not os.path.exists(saved_path):
        return
    save_dir = os.path.dirname(saved_path) or os.path.abspath(_UPLOAD_SAVE_DIR)
    new_path = os.path.join(save_dir, _upload_saved_basename(upload_id))
    if os.path.abspath(saved_path) == os.path.abspath(new_path):
        return
    try:
        os.replace(saved_path, new_path)
        _saved_file_paths[upload_id] = new_path
        logger.info("upload retargeted: %s -> %s", saved_path, new_path)
    except Exception:
        logger.exception("upload retarget failed: %s -> %s", saved_path, new_path)


def _may_receive_late_filename(meta: Dict[str, Any]) -> bool:
    path = str(meta.get("path") or "")
    return "/upload/" in path or "upload_id=" in path


def rekey_upload_id(old_upload_id: str, new_upload_id: str) -> bool:
    """Move registry and on-disk file from old upload_id to new upload_id (e.g. Claude server file_uuid)."""
    old = str(old_upload_id or "").strip()
    new = str(new_upload_id or "").strip()
    if not old or not new or old == new:
        return old == new
    cleanup_upload_registry()
    if new in _upload_id_map and old != new:
        logger.warning("rekey_upload_id blocked: target already registered %s", new)
        return False

    entry = _upload_id_map.pop(old, None)
    shared_filename = _read_shared_upload_filename(old)
    size = int(_saved_file_sizes.pop(old, 0) or 0)
    old_path = _resolve_existing_saved_path(old)
    _saved_file_paths.pop(old, None)

    save_dir = os.path.abspath(_UPLOAD_SAVE_DIR)
    os.makedirs(save_dir, exist_ok=True)
    new_path = os.path.join(save_dir, _upload_saved_basename(new))

    if old_path and os.path.isfile(old_path):
        if os.path.exists(new_path) and os.path.abspath(old_path) != os.path.abspath(new_path):
            logger.warning("rekey_upload_id blocked: destination exists %s", new_path)
            if entry:
                _upload_id_map[old] = entry
            if size:
                _saved_file_sizes[old] = size
            if old_path:
                _saved_file_paths[old] = old_path
            return False
        try:
            os.replace(old_path, new_path)
        except Exception:
            logger.exception("rekey_upload_id rename failed: %s -> %s", old_path, new_path)
            if entry:
                _upload_id_map[old] = entry
            if size:
                _saved_file_sizes[old] = size
            if old_path:
                _saved_file_paths[old] = old_path
            return False

    if entry:
        ts = entry[1]
        _upload_id_map[new] = entry
        _schedule_upload_expiry(new, ts)
    if new_path and os.path.isfile(new_path):
        _saved_file_paths[new] = new_path
        _saved_file_sizes[new] = size

    for alias_key, uid in list(_upload_server_uuid_alias.items()):
        if uid == old:
            _upload_server_uuid_alias[alias_key] = new

    hint_key = _canonical_uuid_from_upload_filename(new)
    if not hint_key and _STD_UUID_TOKEN_RE.match(new):
        hint_key = new.lower()
    if hint_key:
        register_upload_server_alias(hint_key, new)

    _remove_shared_upload_metadata(old)
    promoted_filename = str(entry[0] or "").strip() if entry else shared_filename
    if promoted_filename:
        _write_shared_upload_metadata(new, promoted_filename)

    logger.info("upload rekeyed: %s -> %s path=%s", old, new, new_path)
    return True


def _file_policy_decision(
    filename: str,
    incoming_size: int,
    policy: Optional[Dict[str, Any]] = None,
    current_size: int = 0,
    allow_unknown_extension: bool = False,
) -> Tuple[bool, str]:
    # 확장자/크기 차단은 pipeline file_block_filter (__isAllowFileExtAndSize)가 담당.
    # ai_engine에서는 저장·매핑만 하고 항상 통과 (기존 동작).
    # --- previous engine-side gate (kept for reference) ---
    # effective_policy = policy or collection_policy_from_request(None)
    # maxsize = int(effective_policy.get("maxsize") or _DEFAULT_FILE_CONTROL_MAXSIZE)
    # if current_size + max(0, incoming_size) > maxsize:
    #     return False, f"size_exceeded:{current_size + max(0, incoming_size)}>{maxsize}"
    #
    # allowed = effective_policy.get("allow_extensions")
    # if allowed:
    #     ext = _filename_extension(filename)
    #     if not ext:
    #         if allow_unknown_extension:
    #             return True, ""
    #         return False, "extension_missing"
    #     if ext not in allowed:
    #         return False, f"extension_denied:{ext}"
    _ = (filename, incoming_size, policy, current_size, allow_unknown_extension)
    return True, ""


def check_file_collection_policy(
    filename: str,
    incoming_size: int = 0,
    policy: Optional[Dict[str, Any]] = None,
    *,
    current_size: int = 0,
    allow_unknown_extension: bool = False,
) -> Tuple[bool, str]:
    """Public gate for allow-ext / maxsize (True, '') if allowed."""
    return _file_policy_decision(
        filename,
        incoming_size,
        policy,
        current_size=current_size,
        allow_unknown_extension=allow_unknown_extension,
    )


def normalize_upload_meta(meta: Dict[str, Any]) -> Tuple[str, str]:
    """Fill upload_id/upload_filename from any known metadata source."""
    response_headers = meta.get("response_headers") if isinstance(meta.get("response_headers"), dict) else {}
    upload_id = str(meta.get("upload_id") or response_headers.get("x-guploader-uploadid") or "").strip()
    if not upload_id:
        for key in ("x-goog-upload-url", "x-goog-upload-control-url"):
            val = str(response_headers.get(key) or "")
            match = re.search(r"upload_id=([^&\s\"'<>]+)", val, flags=re.IGNORECASE)
            if match:
                upload_id = str(match.group(1) or "").strip()
                break
    upload_filename = str(meta.get("upload_filename") or "").strip()
    if not upload_id:
        path = str(meta.get("path") or "")
        if path:
            parsed = urlparse(path)
            upload_id = str((parse_qs(parsed.query).get("upload_id") or [""])[0] or "").strip()
    if upload_id:
        meta["upload_id"] = upload_id
    if upload_filename:
        meta["upload_filename"] = upload_filename
    return upload_id, upload_filename


def cleanup_upload_registry() -> None:
    """Drop expired upload mappings and cap registry size."""
    global _next_cleanup_at
    now = time.monotonic()
    if now < _next_cleanup_at:
        return
    _next_cleanup_at = now + _CLEANUP_INTERVAL

    _cleanup_expired_uploads(now)
    _cleanup_expired_pending_requests(now)
    _cleanup_expired_scope_pending(now)
    _evict_over_limit_uploads()
    _evict_over_limit_pending_requests()
    _evict_over_limit_scope_pending()


def remember_pending_uploads_for_scope(scope_key: str, upload_ids: List[str]) -> None:
    """대화/조직 등 스코프별로 최근 업로드 id 큐에 넣는다 (completion에서 첨부 연결용)."""
    if not scope_key or not upload_ids:
        return
    cleanup_upload_registry()
    now = time.monotonic()
    sk = str(scope_key).strip()
    clean_ids = [str(u or "").strip() for u in upload_ids if str(u or "").strip()]
    if not clean_ids:
        return
    existing = _pending_upload_ids_by_scope.get(sk)
    if existing:
        old_ids, _old_ts = existing
        merged = list(old_ids)
        for u in clean_ids:
            if u not in merged:
                merged.append(u)
        merged = merged[-_MAX_IDS_PER_SCOPE:]
        _pending_upload_ids_by_scope[sk] = (merged, now)
    else:
        _pending_upload_ids_by_scope[sk] = (clean_ids[-_MAX_IDS_PER_SCOPE:], now)
    _schedule_scope_pending_expiry(sk, now)


def pop_pending_uploads_for_scope(scope_key: str) -> List[str]:
    """스코프에 쌓여 있던 업로드 id를 한 번에 꺼낸다 (FIFO 순서 유지)."""
    if not scope_key:
        return []
    cleanup_upload_registry()
    entry = _pending_upload_ids_by_scope.pop(str(scope_key).strip(), None)
    if not entry:
        return []
    upload_ids, _ = entry
    return list(upload_ids)


def peek_pending_uploads_for_scope(scope_key: str) -> List[str]:
    """스코프 큐를 소비하지 않고 조회한다 (FIFO 순서 유지)."""
    if not scope_key:
        return []
    cleanup_upload_registry()
    entry = _pending_upload_ids_by_scope.get(str(scope_key).strip())
    if not entry:
        return []
    upload_ids, _ = entry
    return list(upload_ids)


def claim_pending_uploads_for_scope(scope_key: str, count: int) -> List[str]:
    """스코프 큐 앞에서 count개만 꺼내고 나머지는 다시 넣는다 (병렬 업로드용)."""
    if not scope_key or count <= 0:
        return []
    pending = pop_pending_uploads_for_scope(scope_key)
    if not pending:
        return []
    claimed = pending[:count]
    rest = pending[count:]
    if rest:
        remember_pending_uploads_for_scope(scope_key, rest)
    return claimed


def forget_pending_uploads_from_scope(scope_key: str, upload_ids: List[str]) -> None:
    """request_id로 이미 finalize한 temp id를 스코프 큐에서 제거한다."""
    if not scope_key or not upload_ids:
        return
    cleanup_upload_registry()
    sk = str(scope_key).strip()
    entry = _pending_upload_ids_by_scope.get(sk)
    if not entry:
        return
    drop = {str(u or "").strip() for u in upload_ids if str(u or "").strip()}
    if not drop:
        return
    old_ids, ts = entry
    kept = [u for u in old_ids if u not in drop]
    if not kept:
        _pending_upload_ids_by_scope.pop(sk, None)
        return
    if len(kept) == len(old_ids):
        return
    _pending_upload_ids_by_scope[sk] = (kept, ts)


def _remember_pending_upload_ids(request_id: str, upload_ids: List[str]) -> None:
    if not request_id or not upload_ids:
        return
    cleanup_upload_registry()
    now = time.monotonic()
    _pending_upload_ids_by_request[request_id] = (list(upload_ids), now)
    _schedule_pending_request_expiry(request_id, now)


def _claim_pending_upload_ids(request_id: str) -> List[str]:
    if not request_id:
        return []
    cleanup_upload_registry()
    entry = _pending_upload_ids_by_request.pop(request_id, None)
    if not entry:
        # Inline tee uses "{rid}-up-{hex}" for upload request body; response keeps base rid.
        prefix = f"{request_id}-up-"
        matched_key = ""
        for key in _pending_upload_ids_by_request.keys():
            if key.startswith(prefix):
                matched_key = key
                break
        if matched_key:
            entry = _pending_upload_ids_by_request.pop(matched_key, None)
    if not entry:
        return []
    upload_ids, _ = entry
    return upload_ids


def discard_upload(upload_id: str) -> bool:
    if not upload_id:
        return False
    cleanup_upload_registry()
    removed = False
    _upload_id_map.pop(upload_id, None)
    _saved_file_sizes.pop(upload_id, None)
    _remove_shared_upload_metadata(upload_id)
    for seg, uid in list(_upload_ttl_segment_alias.items()):
        if uid == upload_id:
            _upload_ttl_segment_alias.pop(seg, None)
    saved_path = str(_saved_file_paths.pop(upload_id, "") or "")
    if saved_path and os.path.exists(saved_path):
        try:
            os.remove(saved_path)
            removed = True
            logger.info("upload discarded: %s (%s)", upload_id, saved_path)
        except Exception:
            logger.exception("upload discard failed: %s (%s)", upload_id, saved_path)
    return removed


def discard_pending_uploads(request_id: str) -> int:
    pending_upload_ids = _claim_pending_upload_ids(request_id)
    discarded = 0
    for pending_upload_id in pending_upload_ids:
        if discard_upload(pending_upload_id):
            discarded += 1
    if discarded:
        logger.warning("pending uploads discarded: request_id=%s count=%d", request_id, discarded)
    return discarded


def register_upload_ttl_segment(segment: str, upload_id: str) -> None:
    """Gemini upload response의 contrib_service/ttl_1d segment → upload_id."""
    seg = os.path.basename(str(segment or "").strip().strip("/"))
    uid = str(upload_id or "").strip()
    if seg and uid:
        _upload_ttl_segment_alias[seg.lower()] = uid


def lookup_upload_id_by_ttl_segment(segment: str) -> str:
    seg = os.path.basename(str(segment or "").strip().strip("/"))
    if not seg:
        return ""
    cleanup_upload_registry()
    return str(_upload_ttl_segment_alias.get(seg.lower()) or "")


def register_upload_server_alias(server_uuid: str, upload_id: str) -> None:
    """서버 fileMetadataId(UUID) → 내부 upload_id. 디스크 경로/파일명은 변경하지 않는다."""
    hint_key = _canonical_uuid_from_upload_filename(str(server_uuid or ""))
    if not hint_key and _STD_UUID_TOKEN_RE.match(str(server_uuid or "").strip()):
        hint_key = str(server_uuid or "").strip().lower()
    uid = str(upload_id or "").strip()
    if hint_key and uid:
        _upload_server_uuid_alias[hint_key] = uid


def _resolve_existing_saved_path(upload_id: str) -> str:
    """upload_id(확장자 없음) 경로를 우선; 레거시 grok-xxx.pdf 는 찾으면 확장자 없는 이름으로 통일."""
    saved_path = str(_saved_file_paths.get(upload_id) or "")
    save_dir = os.path.abspath(_UPLOAD_SAVE_DIR)
    base = _upload_saved_basename(upload_id)
    if not base:
        return saved_path

    canonical = os.path.join(save_dir, base)
    if os.path.isfile(canonical):
        _saved_file_paths[upload_id] = canonical
        return canonical

    candidates: List[str] = []
    if saved_path:
        candidates.append(saved_path)
    try:
        for name in os.listdir(save_dir):
            if name == base or name.startswith(f"{base}."):
                candidates.append(os.path.join(save_dir, name))
    except OSError:
        pass

    seen: Set[str] = set()
    for path in candidates:
        norm = os.path.abspath(path)
        if norm in seen or not os.path.isfile(norm):
            continue
        seen.add(norm)
        if os.path.basename(norm) != base:
            try:
                os.replace(norm, canonical)
                _saved_file_paths[upload_id] = canonical
                return canonical
            except OSError:
                _saved_file_paths[upload_id] = norm
                return norm
        _saved_file_paths[upload_id] = norm
        return norm
    # Stale map entries must not survive after the on-disk file is gone.
    if saved_path:
        _saved_file_paths.pop(upload_id, None)
        _saved_file_sizes.pop(upload_id, None)
    return ""


def register_upload_mapping(
    upload_id: str,
    filename: str,
    policy: Optional[Dict[str, Any]] = None,
    allow_unknown_extension: bool = False,
) -> bool:
    """Register upload_id -> original filename while keeping disk path keyed by upload_id."""
    if not upload_id or not filename:
        return False
    cleanup_upload_registry()
    existing = _upload_id_map.get(upload_id)
    if existing:
        old_name = str(existing[0] or "")
        if _filename_extension(old_name) and not _filename_extension(filename):
            logger.info("upload mapping kept: %s keeps %s over %s", upload_id, old_name, filename)
            return True
    saved_size = _saved_file_sizes.get(upload_id, 0)
    saved_path = _resolve_existing_saved_path(upload_id)
    effective_policy = policy
    if saved_size > 0 and saved_path:
        base = dict(policy or collection_policy_from_request(None))
        configured = int(base.get("maxsize") or 0)
        if saved_size > configured:
            base["maxsize"] = min(max(configured, saved_size), _MAX_FILE_CONTROL_MAXSIZE)
            effective_policy = base
    ok, reason = _file_policy_decision(
        filename,
        0,
        effective_policy,
        current_size=saved_size,
        allow_unknown_extension=allow_unknown_extension,
    )
    if not ok:
        discard_upload(upload_id)
        mark_collection_denied(upload_id, reason)
        logger.info("upload mapping skipped: %s filename=%s reason=%s", upload_id, filename, reason)
        return False
    now = time.monotonic()
    _upload_id_map[upload_id] = (filename, now)
    _write_shared_upload_metadata(upload_id, filename)
    _COLLECTION_DENIED.pop(upload_id, None)
    _schedule_upload_expiry(upload_id, now)
    logger.info("upload registered: %s -> %s", upload_id, filename)
    hint_key = _canonical_uuid_from_upload_filename(filename)
    if not hint_key and _STD_UUID_TOKEN_RE.match(str(filename or "").strip()):
        hint_key = str(filename or "").strip().lower()
    if hint_key:
        register_upload_server_alias(hint_key, upload_id)
    _retarget_saved_path_for_filename(upload_id, filename)
    return True


def append_upload_payload(
    upload_id: str,
    payload: bytes,
    policy: Optional[Dict[str, Any]] = None,
    allow_unknown_extension: bool = True,
) -> str:
    """Append request payload bytes to the on-disk file for an upload_id."""
    entry = _upload_id_map.get(upload_id)
    orig_filename = entry[0] if entry else upload_id[:48]
    current_size = _saved_file_sizes.get(upload_id, 0)
    ok, reason = _file_policy_decision(
        orig_filename,
        len(payload) if payload else 0,
        policy,
        current_size=current_size,
        allow_unknown_extension=allow_unknown_extension,
    )
    if not ok:
        discard_upload(upload_id)
        mark_collection_denied(upload_id, reason)
        logger.info("upload payload skipped: %s filename=%s reason=%s", upload_id, orig_filename, reason)
        return ""

    save_dir = os.path.abspath(_UPLOAD_SAVE_DIR)
    os.makedirs(save_dir, exist_ok=True)

    safe_name = _upload_saved_basename(upload_id, orig_filename)
    dest = os.path.join(save_dir, safe_name)
    with open(dest, "ab") as f:
        f.write(payload)
    _saved_file_paths[upload_id] = dest
    _saved_file_sizes[upload_id] = current_size + (len(payload) if payload else 0)
    logger.info("upload saved: %s (%d bytes)", dest, len(payload))
    return dest


def replace_upload_payload(
    upload_id: str,
    payload: bytes,
    filename: str = "",
    policy: Optional[Dict[str, Any]] = None,
    allow_unknown_extension: bool = True,
) -> str:
    """Atomically replace an upload with a complete object response.

    Unlike :func:`append_upload_payload`, this is for CDN/download responses
    that contain the whole file rather than a streamed request chunk.
    """
    uid = str(upload_id or "").strip()
    if not uid or not payload:
        return ""
    entry = _upload_id_map.get(uid)
    orig_filename = str(filename or (entry[0] if entry else "") or uid[:48]).strip()
    ok, reason = _file_policy_decision(
        orig_filename,
        len(payload),
        policy,
        current_size=0,
        allow_unknown_extension=allow_unknown_extension,
    )
    if not ok:
        discard_upload(uid)
        mark_collection_denied(uid, reason)
        logger.info("upload replacement skipped: %s filename=%s reason=%s", uid, orig_filename, reason)
        return ""

    save_dir = os.path.abspath(_UPLOAD_SAVE_DIR)
    os.makedirs(save_dir, exist_ok=True)
    safe_name = _upload_saved_basename(uid, orig_filename)
    dest = os.path.join(save_dir, safe_name)
    tmp_path = f"{dest}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_path, dest)
    except OSError:
        logger.exception("upload replacement failed: %s (%s)", uid, dest)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return ""

    now = time.monotonic()
    _upload_id_map[uid] = (orig_filename, now)
    _write_shared_upload_metadata(uid, orig_filename)
    _COLLECTION_DENIED.pop(uid, None)
    _schedule_upload_expiry(uid, now)
    _saved_file_paths[uid] = dest
    _saved_file_sizes[uid] = len(payload)
    logger.info("upload replaced: %s (%d bytes)", dest, len(payload))
    return dest


def get_saved_attachment(upload_id: str, fallback_name: str = "", fallback_mime: str = "") -> Optional[Dict[str, Any]]:
    """Return policy API attachment info for a saved upload_id.

    Legacy contract: attachments[].id is a local filesystem path readable by
    file_block_filter on the same host/mount. Storage is dedicated tmpfs
    (No-Persistent-Disk for payload). Extra fields are additive.
    """
    if not upload_id:
        return None
    cleanup_upload_registry()
    saved_path = _resolve_existing_saved_path(upload_id)
    if not saved_path or not os.path.isfile(saved_path):
        return None
    # policy API name/id: upload_id와 동일한 확장자 없는 basename
    display_name = (
        str(fallback_name or "").strip()
        or get_registered_upload_filename(upload_id)
        or _upload_saved_basename(upload_id)
    )
    size = int(_saved_file_sizes.get(upload_id, 0) or 0)
    if size <= 0:
        try:
            size = int(os.path.getsize(saved_path))
            _saved_file_sizes[upload_id] = size
        except OSError:
            size = 0
    return {
        "id": saved_path,
        "size": size,
        "name": display_name,
        "mime_type": fallback_mime or "",
        "storage_type": "ephemeral_tmpfs_path",
        "ephemeral": True,
        "attachment_id": display_name,
        "delivery_class": "ephemeral_file_inspection",
        "hash": {
            "algorithm": "sha256",
            "value": "",
            "scope": "unknown",
            "bytes": size,
        },
    }


def expected_saved_path(upload_id: str) -> str:
    """Canonical tmpfs path for upload_id (may not exist yet — race with chat)."""
    uid = str(upload_id or "").strip()
    if not uid:
        return ""
    base = _upload_saved_basename(uid)
    if not base:
        return ""
    return os.path.join(os.path.abspath(_UPLOAD_SAVE_DIR), base)


def wait_for_saved_attachment(
    upload_id: str,
    *,
    timeout_ms: int = 200,
    interval_ms: int = 25,
    fallback_name: str = "",
    fallback_mime: str = "",
) -> Optional[Dict[str, Any]]:
    """Poll briefly for upload bytes that may still be flushing to tmpfs.

    Chat (completion) often arrives milliseconds after the upload PUT finishes.
    Without a short wait, file_block_filter receives an empty/missing path and
    cannot inspect the file. Keep timeout small — Inline ChatGate holds the
    user request until policy returns.
    """
    uid = str(upload_id or "").strip()
    if not uid:
        return None

    immediate = get_saved_attachment(uid, fallback_name=fallback_name, fallback_mime=fallback_mime)
    if immediate:
        return immediate

    try:
        timeout = max(0, int(timeout_ms))
    except Exception:
        timeout = 200
    try:
        interval = max(5, int(interval_ms))
    except Exception:
        interval = 25

    deadline = time.monotonic() + (timeout / 1000.0)
    while time.monotonic() < deadline:
        time.sleep(interval / 1000.0)
        saved = get_saved_attachment(uid, fallback_name=fallback_name, fallback_mime=fallback_mime)
        if saved:
            logger.info(
                "upload wait resolved: id=%s waited_ms~%d path=%s",
                uid[:64],
                timeout,
                str(saved.get("id") or "")[:120],
            )
            return saved

    return None


def get_registered_upload_filename(upload_id: str) -> str:
    """Original filename registered for upload_id, if any."""
    if not upload_id:
        return ""
    cleanup_upload_registry()
    entry = _upload_id_map.get(upload_id)
    if not entry:
        shared = _read_shared_upload_filename(upload_id)
        if shared:
            now = time.monotonic()
            _upload_id_map[upload_id] = (shared, now)
            _schedule_upload_expiry(upload_id, now)
        return shared
    return str(entry[0] or "")


def iter_registered_upload_mappings(limit: int = 64) -> List[Tuple[str, str, float]]:
    """(upload_id, filename, ts) newest first."""
    cleanup_upload_registry()
    rows: List[Tuple[str, str, float]] = []
    for upload_id, (filename, ts) in list(_upload_id_map.items()):
        uid = str(upload_id or "").strip()
        name = str(filename or "").strip()
        if not uid:
            continue
        try:
            stamp = float(ts)
        except Exception:
            stamp = 0.0
        rows.append((uid, name, stamp))
    rows.sort(key=lambda item: item[2], reverse=True)
    cap = max(1, int(limit or 64))
    return rows[:cap]


def lookup_upload_ids_by_registered_filename(filename_hint: str) -> List[str]:
    """Match basename(filename_hint) to registered upload filenames; newest first."""
    hint = str(filename_hint or "").strip()
    if not hint:
        return []
    hint_norm = os.path.basename(hint.replace("\\", "/"))
    hint_lower = hint_norm.lower()
    cleanup_upload_registry()
    matches: List[Tuple[float, str]] = []
    for upload_id, (filename, ts) in list(_upload_id_map.items()):
        reg = str(filename or "").strip()
        base = os.path.basename(reg.replace("\\", "/"))
        if not base:
            continue
        if base.lower() == hint_lower:
            matches.append((ts, upload_id))
    matches.sort(key=lambda x: x[0], reverse=True)
    return [uid for _, uid in matches]


def lookup_upload_ids_by_saved_size(size: int, limit: int = 8) -> List[str]:
    """Match on-disk saved upload sizes; newest first. Used when TTL/filename mapping is missing."""
    try:
        want = int(size or 0)
    except Exception:
        want = 0
    if want <= 0:
        return []
    cleanup_upload_registry()
    exact: List[Tuple[float, str]] = []
    partial: List[Tuple[float, int, str]] = []  # ts, distance, uid
    for upload_id, saved_size in list(_saved_file_sizes.items()):
        try:
            sz = int(saved_size or 0)
        except Exception:
            sz = 0
        if sz <= 0:
            continue
        if not _resolve_existing_saved_path(upload_id):
            continue
        entry = _upload_id_map.get(upload_id)
        ts = float(entry[1]) if entry else 0.0
        if sz == want:
            exact.append((ts, upload_id))
        elif sz <= want:
            # Partial/chunked upload still on disk: prefer closest under-sized match.
            partial.append((ts, want - sz, upload_id))
    if exact:
        exact.sort(key=lambda x: x[0], reverse=True)
        return [uid for _, uid in exact[: max(1, int(limit))]]

    # Disk scan for exact size when registry memory was lost but file remains.
    seen = {uid for _, uid in exact} | {uid for _, _, uid in partial}
    save_dir = os.path.abspath(_UPLOAD_SAVE_DIR)
    try:
        for name in os.listdir(save_dir):
            if name in seen:
                continue
            path = os.path.join(save_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                sz = int(os.path.getsize(path))
            except OSError:
                continue
            if sz == want:
                _saved_file_paths[name] = path
                _saved_file_sizes[name] = sz
                exact.append((os.path.getmtime(path), name))
                seen.add(name)
            elif sz > 0 and sz <= want:
                partial.append((os.path.getmtime(path), want - sz, name))
                _saved_file_paths.setdefault(name, path)
                _saved_file_sizes.setdefault(name, sz)
                seen.add(name)
    except OSError:
        pass
    if exact:
        exact.sort(key=lambda x: x[0], reverse=True)
        return [uid for _, uid in exact[: max(1, int(limit))]]
    if partial:
        # Newest first, then closest size; unique ids only.
        partial.sort(key=lambda x: (-x[0], x[1]))
        out: List[str] = []
        for _, _, uid in partial:
            if uid not in out:
                out.append(uid)
            if len(out) >= max(1, int(limit)):
                break
        return out
    return []

def lookup_recent_saved_upload_ids(prefix: str = "AKh", limit: int = 8, max_age_sec: float = 180.0) -> List[str]:
    """Recent on-disk uploads (by mtime / registry ts), optional prefix filter.

    Also scans the save directory so files survive process restarts when the
    in-memory size map was cleared.
    """
    needle = str(prefix or "").strip()
    cleanup_upload_registry()
    now = time.time()
    mono_now = time.monotonic()
    matches: List[Tuple[float, str]] = []
    seen: Set[str] = set()

    def _consider(upload_id: str, path: str, sort_ts: float) -> None:
        uid = str(upload_id or "").strip()
        if not uid or uid in seen:
            return
        if needle and not uid.startswith(needle):
            return
        if not path or not os.path.isfile(path):
            return
        try:
            if int(os.path.getsize(path) or 0) <= 0:
                return
        except OSError:
            return
        seen.add(uid)
        matches.append((float(sort_ts), uid))

    for upload_id, saved_size in list(_saved_file_sizes.items()):
        try:
            if int(saved_size or 0) <= 0:
                continue
        except Exception:
            continue
        path = _resolve_existing_saved_path(upload_id)
        if not path:
            continue
        entry = _upload_id_map.get(upload_id)
        if entry:
            age = mono_now - float(entry[1])
            if max_age_sec > 0 and age > max_age_sec:
                continue
            _consider(upload_id, path, float(entry[1]))
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if max_age_sec > 0 and (now - mtime) > max_age_sec:
            continue
        _consider(upload_id, path, mtime)

    # Disk scan: pick up AKh* files missing from the in-memory maps (restart / race).
    save_dir = os.path.abspath(_UPLOAD_SAVE_DIR)
    try:
        for name in os.listdir(save_dir):
            if name in seen:
                continue
            if needle and not name.startswith(needle):
                continue
            path = os.path.join(save_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                mtime = os.path.getmtime(path)
                sz = int(os.path.getsize(path) or 0)
            except OSError:
                continue
            if sz <= 0:
                continue
            if max_age_sec > 0 and (now - mtime) > max_age_sec:
                continue
            _saved_file_paths.setdefault(name, path)
            _saved_file_sizes.setdefault(name, sz)
            _consider(name, path, mtime)
    except OSError:
        pass

    matches.sort(key=lambda x: x[0], reverse=True)
    return [uid for _, uid in matches[: max(1, int(limit))]]


def lookup_upload_id_by_registered_server_uuid(server_uuid: str) -> str:
    """Map server-side file UUID (stored as registered filename, often uuid.png) to our upload_id."""
    hint = str(server_uuid or "").strip()
    hint_key = _canonical_uuid_from_upload_filename(hint)
    if not hint_key and _STD_UUID_TOKEN_RE.match(hint):
        hint_key = hint.lower()
    if not hint_key:
        return ""
    cleanup_upload_registry()
    alias = _upload_server_uuid_alias.get(hint_key)
    if alias and _saved_file_paths.get(alias):
        return alias
    for upload_id, (filename, _) in list(_upload_id_map.items()):
        reg_key = _canonical_uuid_from_upload_filename(str(filename or ""))
        if reg_key and reg_key == hint_key:
            return upload_id
        # 등록명이 정확히 일치 (레거시·전체 경로 등)
        if str(filename or "").strip() == hint:
            return upload_id
    return ""


def lookup_upload_ids_embedded_in_text(blob: str, min_id_len: int = 16) -> List[str]:
    """Return upload_ids already known to the registry that appear verbatim in blob (newest first)."""
    if not blob:
        return []
    cleanup_upload_registry()
    found: List[Tuple[float, str]] = []
    for upload_id, (_, ts) in list(_upload_id_map.items()):
        if len(upload_id) < min_id_len:
            continue
        if upload_id in blob:
            found.append((ts, upload_id))
    found.sort(key=lambda x: x[0], reverse=True)
    out: List[str] = []
    seen: Set[str] = set()
    for _, uid in found:
        if uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out


def save_inline_upload(
    upload_id: str,
    filename: str,
    data: bytes,
    policy: Optional[Dict[str, Any]] = None,
    allow_unknown_extension: bool = False,
) -> str:
    """Save a file immediately when an adapter already has both name and bytes."""
    if not register_upload_mapping(upload_id, filename, policy, allow_unknown_extension):
        return ""
    return append_upload_payload(upload_id, data, policy, allow_unknown_extension=allow_unknown_extension)


def save_inline_upload_result(
    upload_id: str,
    filename: str,
    data: bytes,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    saved_path = save_inline_upload(upload_id, filename, data, policy)
    if not saved_path:
        return build_skipped_upload_result(upload_id, filename, "collection_policy")
    return _build_saved_upload_result([_build_saved_file_entry(upload_id, filename, saved_path)])


def save_inline_upload_batch(
    entries: List[Tuple[str, str, bytes]],
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    saved_files: List[Dict[str, str]] = []
    skipped = 0
    for upload_id, filename, data in entries:
        saved_path = save_inline_upload(upload_id, filename, data, policy)
        if saved_path:
            saved_files.append(_build_saved_file_entry(upload_id, filename, saved_path))
        else:
            skipped += 1
    if not saved_files and skipped:
        return build_skipped_upload_result(reason="collection_policy")
    return _build_saved_upload_result(saved_files)


def create_pending_upload_batch(
    request_id: str,
    payloads: List[bytes],
    prefix: str,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    files = [("", data) for data in payloads]
    return create_pending_upload_batch_from_files(request_id, files, prefix, policy)


def create_pending_upload_batch_from_files(
    request_id: str,
    files: List[Tuple[str, bytes]],
    prefix: str,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    pending_upload_ids: List[str] = []
    saved_files: List[Dict[str, str]] = []
    skipped = 0
    normalized_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", str(prefix or "upload")).strip("-") or "upload"
    for filename, data in files:
        if not data:
            continue
        upload_id = f"{normalized_prefix}-{uuid.uuid4().hex[:16]}"
        normalized_filename = str(filename or "").strip()
        effective_filename = normalized_filename or upload_id
        saved_path = save_inline_upload(
            upload_id,
            effective_filename,
            data,
            policy,
            allow_unknown_extension=not bool(normalized_filename),
        )
        if saved_path:
            pending_upload_ids.append(upload_id)
            saved_files.append(_build_saved_file_entry(upload_id, effective_filename, saved_path))
        else:
            skipped += 1
    if not saved_files and not skipped:
        return build_upload_result()
    if saved_files:
        _remember_pending_upload_ids(request_id, pending_upload_ids)
        return _build_saved_upload_result(saved_files)
    return build_skipped_upload_result(reason="collection_policy")


def extract_upload_filenames_from_payload(payload: bytes, filename_pattern: str) -> List[str]:
    """응답 본문에서 업로드 확정 파일명(예: fileMetadataId) 목록을 추출한다."""
    if not payload:
        return []
    try:
        byte_pattern = re.compile(filename_pattern.encode("ascii"))
        return [
            match.group(1).decode("utf-8", errors="replace").strip()
            for match in byte_pattern.finditer(payload)
            if match.group(1)
        ]
    except Exception:
        text = payload.decode("utf-8", errors="replace")
        return [str(match or "").strip() for match in re.findall(filename_pattern, text)]


def _finalize_upload_id_list(
    pending_upload_ids: List[str],
    filenames: List[str],
    policy: Optional[Dict[str, Any]] = None,
    log_context: str = "",
) -> Dict[str, Any]:
    if not pending_upload_ids:
        return build_upload_result()

    saved_files: List[Dict[str, str]] = []
    discarded = 0
    skipped = 0
    for index, pending_upload_id in enumerate(pending_upload_ids):
        normalized_name = str(filenames[index] or "").strip() if index < len(filenames) else ""
        existing_entry = _upload_id_map.get(pending_upload_id)
        existing_name = existing_entry[0] if existing_entry else ""
        if (
            normalized_name
            and not _filename_extension(normalized_name)
            and _filename_extension(existing_name)
            and not _STD_UUID_TOKEN_RE.match(normalized_name)
        ):
            normalized_name = existing_name
        if not normalized_name:
            if discard_upload(pending_upload_id):
                discarded += 1
            continue
        if not register_upload_mapping(pending_upload_id, normalized_name, policy):
            skipped += 1
            continue
        saved_files.append(
            _build_saved_file_entry(
                pending_upload_id,
                normalized_name,
                str(_saved_file_paths.get(pending_upload_id) or ""),
            )
        )
    if not saved_files and discarded:
        logger.warning(
            "pending upload finalize failed: context=%s discarded=%d",
            log_context or "-",
            discarded,
        )
        return build_upload_result(handled=True)
    if not saved_files and skipped:
        return build_skipped_upload_result(reason="collection_policy")
    return _build_saved_upload_result(saved_files)


def finalize_pending_uploads(
    request_id: str,
    filenames: List[str],
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    pending_upload_ids = _claim_pending_upload_ids(request_id)
    return _finalize_upload_id_list(
        pending_upload_ids,
        filenames,
        policy,
        log_context=f"request_id={request_id}",
    )


def finalize_scope_pending_uploads(
    scope_key: str,
    filenames: List[str],
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """request_id가 다른 응답(예: Grok /?_rsc=)에서 fileMetadataId를 받을 때 스코프 큐로 매핑.

    filenames가 있으면 그만큼만 claim한다. 등록된 원본 파일명이 있으면 그것으로
    매칭하고, 없으면 FIFO. 병렬 업로드 시 응답 1건이 큐 전체를 pop·discard 하지 않는다.
    """
    if not filenames:
        pending_upload_ids = pop_pending_uploads_for_scope(scope_key)
        return _finalize_upload_id_list(
            pending_upload_ids,
            filenames,
            policy,
            log_context=f"scope={scope_key}",
        )

    all_pending = pop_pending_uploads_for_scope(scope_key)
    if not all_pending:
        return build_upload_result()

    remaining = list(all_pending)
    claimed: List[str] = []
    for raw_name in filenames:
        name = str(raw_name or "").strip()
        match_i = -1
        if name:
            for i, uid in enumerate(remaining):
                existing = get_registered_upload_filename(uid)
                if existing and existing == name:
                    match_i = i
                    break
        if match_i < 0 and remaining:
            match_i = 0
        if match_i >= 0:
            claimed.append(remaining.pop(match_i))
    if remaining:
        remember_pending_uploads_for_scope(scope_key, remaining)

    return _finalize_upload_id_list(
        claimed,
        filenames,
        policy,
        log_context=f"scope={scope_key}",
    )


def finalize_pending_uploads_from_payload(
    request_id: str,
    payload: bytes,
    filename_pattern: str,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not request_id or not payload:
        return build_upload_result()
    filenames = extract_upload_filenames_from_payload(payload, filename_pattern)
    if not filenames:
        # fileMetadataId가 같은 응답에 없어도 이미 저장된 파일은 유지 (스코프/정책 fallback용).
        logger.debug("upload finalize pattern missed (kept pending): request_id=%s", request_id)
        return build_upload_result()
    return finalize_pending_uploads(request_id, filenames, policy)


def finalize_scope_pending_uploads_from_payload(
    scope_key: str,
    payload: bytes,
    filename_pattern: str,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not scope_key or not payload:
        return build_upload_result()
    filenames = extract_upload_filenames_from_payload(payload, filename_pattern)
    if not filenames:
        return build_upload_result()
    return finalize_scope_pending_uploads(scope_key, filenames, policy)


def list_recent_upload_ids_by_prefix(prefix: str, limit: int = 8) -> List[str]:
    """저장된 upload_id 중 prefix로 시작하는 항목을 최신순으로 반환."""
    needle = str(prefix or "").strip()
    if not needle:
        return []
    cleanup_upload_registry()
    matches: List[Tuple[float, str]] = []
    for upload_id, (_, ts) in list(_upload_id_map.items()):
        if not str(upload_id).startswith(needle):
            continue
        if not str(_saved_file_paths.get(upload_id) or ""):
            continue
        matches.append((ts, upload_id))
    matches.sort(key=lambda x: x[0], reverse=True)
    return [uid for _, uid in matches[: max(1, int(limit))]]


def parse_multipart(payload: bytes, content_type: str = "") -> List[Dict[str, Any]]:
    """multipart/form-data payload를 파싱하여 part 리스트 반환.
    각 part: {"name": str, "filename": str, "content_type": str, "body": bytes}
    """
    boundary = b""
    if content_type:
        for seg in content_type.split(";"):
            seg = seg.strip()
            if seg.lower().startswith("boundary="):
                boundary = seg[9:].strip().strip('"').encode("ascii", errors="replace")
                break
    if not boundary and payload:
        first_line = payload.split(b"\r\n", 1)[0].strip()
        if first_line.startswith(b"--") and len(first_line) > 4:
            boundary = first_line[2:]
    if not boundary:
        return []

    delimiter = b"--" + boundary
    raw_parts = payload.split(delimiter)
    parts: List[Dict[str, Any]] = []
    for raw in raw_parts:
        if not raw or raw.strip(b"\r\n- ") == b"":
            continue
        raw = raw.lstrip(b"\r\n")
        if raw.startswith(b"--"):
            continue
        sep = raw.find(b"\r\n\r\n")
        if sep < 0:
            continue
        header_text = raw[:sep].decode("utf-8", errors="replace")
        body = raw[sep + 4:]
        if body.endswith(b"\r\n"):
            body = body[:-2]

        name = ""
        filename = ""
        ct = ""
        for line in header_text.split("\r\n"):
            ll = line.lower()
            if "content-disposition" in ll:
                m = re.search(r'name="([^"]*)"', line)
                if m:
                    name = m.group(1)
                m = re.search(r'filename="([^"]*)"', line)
                if m:
                    filename = m.group(1)
            if ll.strip().startswith("content-type:"):
                ct = line.split(":", 1)[1].strip()
        parts.append({"name": name, "filename": filename, "content_type": ct, "body": body})
    return parts


def handle_upload_event(req: Dict[str, Any], payload: bytes) -> Dict[str, Any]:
    """Handle generic upload request/response events after adapter normalization."""

    direction = str(req.get("direction") or "").lower()
    unit_type = str(req.get("unit_type") or "").lower()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    upload_id, upload_filename = normalize_upload_meta(meta)
    policy = collection_policy_from_request(req)

    logger.info("handle_upload_event: dir=%s unit=%s uid=%.40s fname=%s payload=%d",
                direction, unit_type, upload_id, upload_filename, len(payload) if payload else 0)

    result = build_upload_result(upload_id=upload_id, upload_filename=upload_filename)

    # Init request may only expose filename; adapter-specific logic will map it later.
    if not upload_id and upload_filename and direction == "request":
        return result

    # Actual upload request: register name if known, then append the payload.
    if upload_id and direction == "request" and unit_type in ("raw_body", "form_body", "json_body"):
        allowed_exts = policy.get("allow_extensions")
        filename_has_ext = bool(_filename_extension(upload_filename))
        allow_unknown_extension = not filename_has_ext and _may_receive_late_filename(meta)
        if upload_filename and (not allowed_exts or filename_has_ext):
            if not register_upload_mapping(upload_id, upload_filename, policy):
                why = collection_denied_reason(upload_id) or "collection_policy"
                return build_skipped_upload_result(upload_id, upload_filename, why)

        if payload:
            try:
                saved = append_upload_payload(
                    upload_id,
                    payload,
                    policy,
                    allow_unknown_extension=allow_unknown_extension,
                )
                result["saved_path"] = saved
                if not saved:
                    why = collection_denied_reason(upload_id) or "collection_policy"
                    return build_skipped_upload_result(upload_id, upload_filename, why)
            except Exception:
                logger.exception("upload save failed: upload_id=%s", upload_id)

        entry = _upload_id_map.get(upload_id)
        result["filename"] = entry[0] if entry else upload_id[:48]
        result["upload_filename"] = result["filename"]
        result["saved_files"] = [
            _build_saved_file_entry(upload_id, result["filename"], result["saved_path"])
        ] if result["saved_path"] else []
        result["handled"] = True
        return result

    # Init response may only carry the upload_id mapping.
    if upload_id and direction == "response":
        if upload_filename:
            existing = _upload_id_map.get(upload_id)
            if existing and _filename_extension(str(existing[0] or "")) and not _filename_extension(upload_filename):
                logger.info(
                    "handle_upload_event: keep registered filename upload_id=%s name=%s",
                    upload_id,
                    existing[0],
                )
            elif not register_upload_mapping(upload_id, upload_filename, policy):
                why = collection_denied_reason(upload_id) or "collection_policy"
                return build_skipped_upload_result(upload_id, upload_filename, why)
        result["handled"] = True
        return result

    return result


# Shared by every adapter upload module. The policy event loop and one worker
# thread both touch these maps; the lock makes that safe. RLock because public
# entry points call each other. wait_for_saved_attachment stays outside so its
# sleep does not pin the worker that is flushing the file.
UPLOAD_STATE_LOCK = threading.RLock()
_UPLOAD_LOCK_EXEMPT = frozenset({"wait_for_saved_attachment", "bind_upload_state_lock"})


def _guard_upload_state(fn):
    def wrapped(*args, **kwargs):
        with UPLOAD_STATE_LOCK:
            return fn(*args, **kwargs)

    wrapped.__wrapped__ = fn
    wrapped.__aivax_upload_locked__ = True
    wrapped.__name__ = getattr(fn, "__name__", "wrapped")
    wrapped.__doc__ = getattr(fn, "__doc__", None)
    wrapped.__module__ = getattr(fn, "__module__", None)
    return wrapped


def bind_upload_state_lock(namespace, exempt=()):
    """Serialize public functions in an upload module."""
    skip = _UPLOAD_LOCK_EXEMPT | set(exempt)
    module_name = namespace.get("__name__")
    for name, fn in list(namespace.items()):
        if not isinstance(name, str) or not name or name.startswith("_"):
            continue
        if name in skip or not callable(fn) or isinstance(fn, type):
            continue
        if getattr(fn, "__module__", None) != module_name:
            continue
        if getattr(fn, "__aivax_upload_locked__", False):
            continue
        namespace[name] = _guard_upload_state(fn)


bind_upload_state_lock(globals())
