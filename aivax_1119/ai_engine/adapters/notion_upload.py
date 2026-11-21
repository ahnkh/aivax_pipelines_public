"""
Notion file upload and policy attachment assembly.

Flow (log/content.pcap):
  POST /api/v3/getUploadFileUrlForAssistantChatTranscriptUpload
    {"name":"75a4b688-....pdf","contentType":"application/pdf","contentLength":468694,...}
  response:
    {"url":"attachment:7ed69fea-...:75a4b688-....pdf","signedUploadPostUrl":"https://prod-files-secure-...s3.../",...}
  POST S3 multipart (key=spaceId/fileId/storageName.pdf, file bytes)
  POST /api/v3/runInferenceTranscript
    transcript[] includes {"type":"attachment","fileUrl":"attachment:fileId:storageName.pdf",...}

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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import file_upload_manager as _fum

logger = logging.getLogger(__name__)

POLICY_ATTACHMENTS_FINAL = True

_NOTION_FILE_INDEX: Dict[str, Dict[str, Any]] = {}
_NOTION_FILE_ORDER: deque[str] = deque()
_NOTION_FILE_LIMIT = 512
_SIGNED_FETCH_TIMEOUT_SEC = 20
_SIGNED_FETCH_MAX_BYTES = 50 * 1024 * 1024
_SIGNED_FETCHED: set[str] = set()
# file.notion.com CDN GET 에만 실리는 file_token — app.notion.com API 쿠키에는 없음.
_CACHED_FILE_TOKEN = ""

_UPLOAD_INIT_PATH = "getuploadfileurlforassistantchattranscriptupload"
_SIGNED_URLS_PATH = "getsignedfileurls"
_ATTACHMENT_URL_RE = re.compile(
    r"^attachment:([0-9a-fA-F-]{36}):([^:]+)$",
    re.IGNORECASE,
)
_FILE_ID_FROM_KEY_RE = re.compile(
    r"/([0-9a-fA-F-]{36})/[^/]+$",
    re.IGNORECASE,
)
_FILE_CDN_PATH_RE = re.compile(
    r"/f/f/[0-9a-fA-F-]{36}/([0-9a-fA-F-]{36})/([^/?#]+)",
    re.IGNORECASE,
)
_HTTPS_URL_RE = re.compile(r"https://[^\s\"'\\<>]+", re.IGNORECASE)
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ATTACHMENT_URL_JSON_RE = re.compile(
    r'"url"\s*:\s*"(attachment:[^"]+)"',
    re.IGNORECASE,
)
_FILE_NAME_JSON_RE = re.compile(
    r'"fileName"\s*:\s*"((?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)
_CONTENT_TYPE_RE = re.compile(
    r'"contentType"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
_FILE_SIZE_RE = re.compile(
    r'"fileSizeBytes"\s*:\s*(\d+)',
    re.IGNORECASE,
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


def _path_base(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("path") or "").split("?", 1)[0].lower()


def _host(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("host") or "").lower()


def is_notion_upload_init_path(path: str) -> bool:
    return _UPLOAD_INIT_PATH in str(path or "").lower()


def is_notion_signed_urls_path(path: str) -> bool:
    return _SIGNED_URLS_PATH in str(path or "").lower()


def is_notion_s3_upload_host(host: str) -> bool:
    low = str(host or "").lower()
    return "prod-files-secure" in low and "amazonaws.com" in low


def is_notion_file_cdn_host(host: str) -> bool:
    return "file.notion.com" in str(host or "").lower()


def is_notion_upload_init_request(req: Dict[str, Any]) -> bool:
    return is_notion_upload_init_path(_path_base(req))


def is_notion_s3_upload_request(req: Dict[str, Any]) -> bool:
    return is_notion_s3_upload_host(_host(req))


def _parse_attachment_url(url: str) -> Tuple[str, str]:
    text = str(url or "").strip()
    m = _ATTACHMENT_URL_RE.match(text)
    if not m:
        return "", ""
    return m.group(1).strip(), m.group(2).strip()


def _file_id_from_s3_key(key_value: str) -> str:
    text = str(key_value or "").strip().replace("\\", "/")
    parts = [p for p in text.split("/") if p]
    if len(parts) >= 2 and _UUID_RE.match(parts[-2]):
        return parts[-2].lower()
    m = _FILE_ID_FROM_KEY_RE.search(text)
    return m.group(1).lower() if m else ""


def _storage_slug_from_name(name: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/"))
    if not base:
        return ""
    stem, _ext = os.path.splitext(base)
    return stem.lower() if stem else base.lower()


def _notion_scope_from_pointer(pointer: Any) -> str:
    if not isinstance(pointer, dict):
        return "notion:orphan"
    table = str(pointer.get("table") or "").strip().lower()
    pid = str(pointer.get("id") or "").strip().lower()
    if table == "thread" and pid:
        return f"thread:{pid}"
    return "notion:orphan"


def _remember_notion_file_binding(
    file_id: str,
    file_name: str = "",
    size_bytes: int = 0,
    mime_type: str = "",
    storage_slug: str = "",
) -> None:
    uid = str(file_id or "").strip().lower()
    if not uid:
        return
    if uid not in _NOTION_FILE_INDEX:
        _NOTION_FILE_ORDER.append(uid)
        while len(_NOTION_FILE_ORDER) > _NOTION_FILE_LIMIT:
            oldest = _NOTION_FILE_ORDER.popleft()
            _NOTION_FILE_INDEX.pop(oldest, None)
    row = dict(_NOTION_FILE_INDEX.get(uid) or {})
    row.update(
        {
            "upload_id": file_id,
            "file_name": str(file_name or row.get("file_name") or "").strip(),
            "size_bytes": int(size_bytes or row.get("size_bytes") or 0),
            "mime_type": str(mime_type or row.get("mime_type") or "").strip(),
            "storage_slug": str(storage_slug or row.get("storage_slug") or "").strip(),
        }
    )
    _NOTION_FILE_INDEX[uid] = row


def _lookup_notion_upload_meta(key: str) -> Dict[str, Any]:
    k = str(key or "").strip().lower()
    if not k:
        return {}
    return dict(_NOTION_FILE_INDEX.get(k) or {})


def _promote_storage_slug_to_file_id(storage_slug: str, file_id: str) -> None:
    slug = str(storage_slug or "").strip().lower()
    fid = str(file_id or "").strip()
    if not slug or not fid:
        return
    meta = _lookup_notion_upload_meta(slug)
    if meta:
        _remember_notion_file_binding(
            fid,
            str(meta.get("file_name") or ""),
            int(meta.get("size_bytes") or 0),
            str(meta.get("mime_type") or ""),
            storage_slug=slug,
        )
        _NOTION_FILE_INDEX.pop(slug, None)
        try:
            _NOTION_FILE_ORDER.remove(slug)
        except ValueError:
            pass
    _fum.register_upload_server_alias(slug, fid)


def _resolve_notion_file_ref(file_id: str) -> Optional[Dict[str, Any]]:
    key = str(file_id or "").strip().lower()
    if not key:
        return None
    cached = _NOTION_FILE_INDEX.get(key)
    if cached:
        return dict(cached)
    if _fum.get_saved_attachment(file_id) or _fum.get_registered_upload_filename(file_id):
        return {
            "upload_id": file_id,
            "file_name": _fum.get_registered_upload_filename(file_id),
            "size_bytes": 0,
            "mime_type": "",
        }
    legacy = _fum.lookup_upload_id_by_registered_server_uuid(file_id)
    if legacy:
        return {
            "upload_id": legacy,
            "file_name": _fum.get_registered_upload_filename(legacy),
            "size_bytes": 0,
            "mime_type": "",
        }
    return None


def _parse_upload_init_request(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = str(obj.get("name") or "").strip()
    if not name:
        return None
    try:
        size = int(obj.get("contentLength") or obj.get("content_length") or 0)
    except Exception:
        size = 0
    mime = str(obj.get("contentType") or obj.get("content_type") or "").strip()
    pointer = obj.get("assistantChatTranscriptSessionPointer")
    scope = _notion_scope_from_pointer(pointer)
    storage_slug = _storage_slug_from_name(name)
    if storage_slug:
        _remember_notion_file_binding(
            storage_slug,
            file_name=name,
            size_bytes=size,
            mime_type=mime,
            storage_slug=storage_slug,
        )
    return {
        "storage_slug": storage_slug,
        "file_name": name,
        "size_bytes": size,
        "mime_type": mime,
        "scope": scope,
    }


def _parse_upload_init_response_items(payload: bytes) -> List[Dict[str, Any]]:
    if not payload:
        return []
    text = _safe_utf8(payload)
    items: List[Dict[str, Any]] = []

    try:
        obj = json.loads(text)
    except Exception:
        obj = None

    def add_row(url: str, file_name: str = "", mime: str = "", size: int = 0) -> None:
        file_id, storage_name = _parse_attachment_url(url)
        if not file_id:
            return
        display_name = file_name or storage_name
        items.append(
            {
                "file_id": file_id,
                "storage_name": storage_name,
                "file_name": display_name,
                "mime_type": mime,
                "size_bytes": size,
            }
        )

    if isinstance(obj, dict):
        url = str(obj.get("url") or "").strip()
        if url:
            add_row(
                url,
                str(obj.get("fileName") or ""),
                str(obj.get("contentType") or ""),
                int(obj.get("fileSizeBytes") or obj.get("contentLength") or 0),
            )
        for key in ("results", "files", "uploaded_files"):
            nested = obj.get(key)
            if isinstance(nested, list):
                for row in nested:
                    if not isinstance(row, dict):
                        continue
                    req_obj = row.get("request")
                    status = row.get("status")
                    if isinstance(req_obj, dict):
                        url = str(req_obj.get("url") or "").strip()
                    else:
                        url = str(row.get("url") or "").strip()
                    mime = ""
                    size = 0
                    if isinstance(status, dict):
                        result = status.get("result")
                        if isinstance(result, dict):
                            data = result.get("data")
                            if isinstance(data, dict):
                                mime = str(data.get("contentType") or "")
                                try:
                                    size = int(data.get("fileSizeBytes") or 0)
                                except Exception:
                                    size = 0
                    if url:
                        add_row(url, mime=mime, size=size)

    if not items:
        urls = [_decode_json_string(u) for u in _ATTACHMENT_URL_JSON_RE.findall(text)]
        names = [_decode_json_string(n) for n in _FILE_NAME_JSON_RE.findall(text)]
        mimes = _CONTENT_TYPE_RE.findall(text)
        sizes = [int(x) for x in _FILE_SIZE_RE.findall(text)]
        for idx, url in enumerate(urls):
            add_row(
                url,
                names[idx] if idx < len(names) else "",
                mimes[idx] if idx < len(mimes) else "",
                sizes[idx] if idx < len(sizes) else 0,
            )
    return items


def _signed_get_url_from_init_payload(payload: bytes) -> str:
    urls = _extract_notion_download_urls(payload)
    for url in urls:
        if "file.notion.com" in url.lower() or any(
            url.lower().endswith(ext) or f"{ext}?" in url.lower()
            for ext in (".pptx", ".pdf", ".png", ".jpg", ".jpeg", ".docx", ".xlsx")
        ):
            return url
    return urls[0] if urls else ""


def _finalize_upload_init_response(req: Dict[str, Any], payload: bytes) -> Dict[str, Any]:
    items = _parse_upload_init_response_items(payload)
    if not items:
        return _fum.build_upload_result(handled=True)

    policy = _fum.collection_policy_from_request(req)
    for row in items:
        file_id = str(row.get("file_id") or "").strip()
        storage_name = str(row.get("storage_name") or "").strip()
        file_name = str(row.get("file_name") or storage_name).strip()
        mime = str(row.get("mime_type") or "").strip()
        try:
            size = int(row.get("size_bytes") or 0)
        except Exception:
            size = 0
        if not file_id:
            continue
        slug = _storage_slug_from_name(storage_name)
        if slug:
            _promote_storage_slug_to_file_id(slug, file_id)
        _remember_notion_file_binding(file_id, file_name, size, mime, storage_slug=slug)
        if file_name:
            _fum.register_upload_mapping(file_id, file_name, policy, allow_unknown_extension=True)

    logger.info("notion upload init response: mapped %d file(s)", len(items))

    # S3 POST는 미러에 안 잡히는 경우가 많음 → signedGetUrl로 즉시 회수 (라이브에서 유효).
    signed_get = _signed_get_url_from_init_payload(payload)
    if signed_get:
        file_id, storage_name = _file_id_from_download_url(signed_get)
        dedupe = (file_id or signed_get).lower()
        existing = _fum.get_saved_attachment(file_id) if file_id else None
        already = bool(existing and os.path.isfile(str(existing.get("id") or "")))
        body = b""
        if dedupe not in _SIGNED_FETCHED and not already:
            body = _download_signed_url(signed_get, req)
        if body:
            if not file_id:
                file_id = storage_name or f"notion-{len(body)}"
            result = _save_notion_bytes(req, file_id, storage_name or file_id, body)
            if isinstance(result, dict) and result.get("handled"):
                _SIGNED_FETCHED.add(dedupe)
                return result

    return _fum.build_upload_result(handled=True)


def _handle_upload_init_request(req: Dict[str, Any], obj: Dict[str, Any]) -> Dict[str, Any]:
    row = _parse_upload_init_request(obj)
    if not row:
        return _fum.build_upload_result(handled=True)
    logger.info(
        "notion upload init request: name=%s scope=%s",
        row.get("file_name"),
        row.get("scope"),
    )
    return _fum.build_upload_result(
        upload_filename=str(row.get("file_name") or ""),
        handled=True,
    )


def _handle_s3_multipart_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    content_type = str(req.get("content_type") or "")
    is_multipart = "multipart/form-data" in content_type.lower()
    if not is_multipart and payload[:2] == b"--" and b"Content-Disposition" in payload[:512]:
        is_multipart = True
    if not is_multipart:
        return None

    parts = _fum.parse_multipart(payload, content_type)
    if not parts:
        return None

    key_value = ""
    file_body = b""
    for part in parts:
        pname = str(part.get("name") or "")
        body = part.get("body", b"")
        if pname == "key" and body:
            key_value = body.decode("utf-8", errors="replace").strip()
        elif pname == "file" or part.get("filename"):
            if body:
                file_body = body

    if not file_body:
        for part in reversed(parts):
            chunk = part.get("body", b"")
            if isinstance(chunk, (bytes, bytearray)) and len(chunk) > 256:
                file_body = chunk
                break
    if not file_body:
        return None

    file_id = _file_id_from_s3_key(key_value)
    if not file_id:
        logger.warning("notion s3 upload missing file_id key=%s", key_value[:160])
        return None

    cached = _resolve_notion_file_ref(file_id) or _lookup_notion_upload_meta(file_id)
    storage_name = str(key_value or "").split("/")[-1]
    filename = str(cached.get("file_name") or storage_name or file_id).strip()
    mime = str(cached.get("mime_type") or "").strip()
    try:
        cached_size = int(cached.get("size_bytes") or 0)
    except Exception:
        cached_size = 0

    try:
        policy = _fum.collection_policy_from_request(req)
        result = _fum.save_inline_upload_result(file_id, filename or file_id, file_body, policy)
        if result.get("handled"):
            _fum.register_upload_mapping(file_id, filename, policy, allow_unknown_extension=True)
            size = len(file_body)
            if cached_size > 0:
                size = cached_size
            _remember_notion_file_binding(file_id, filename, size, mime)
            logger.info(
                "notion s3 upload saved id=%s name=%s (%d bytes)",
                file_id,
                filename,
                len(file_body),
            )
        return result
    except Exception:
        logger.exception("notion s3 upload save failed: %s", file_id)
    return None


def _parse_file_cdn_path(path: str) -> Tuple[str, str]:
    """Return (file_id, storage_name) from /f/f/{space}/{fileId}/{name}."""
    text = str(path or "").split("?", 1)[0]
    m = _FILE_CDN_PATH_RE.search(text)
    if not m:
        return "", ""
    return m.group(1).strip(), unquote(m.group(2).strip())


def _download_name_from_meta(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    raw_path = str(meta.get("path") or "")
    try:
        qs = parse_qs(urlparse(raw_path).query)
    except Exception:
        return ""
    for key in ("downloadName", "downloadname", "name"):
        vals = qs.get(key) or []
        if vals:
            return unquote(str(vals[0] or "")).strip()
    return ""


def _save_notion_bytes(
    req: Dict[str, Any],
    file_id: str,
    filename: str,
    body: bytes,
    mime: str = "",
) -> Optional[Dict[str, Any]]:
    if not file_id or not body:
        return None
    cached = _resolve_notion_file_ref(file_id) or _lookup_notion_upload_meta(file_id)
    display = str(filename or "").strip()
    if not display:
        display = str((cached or {}).get("file_name") or file_id).strip()
    mime_type = str(mime or (cached or {}).get("mime_type") or "").strip()
    try:
        policy = _fum.collection_policy_from_request(req)
        result = _fum.save_inline_upload_result(file_id, display or file_id, body, policy)
        if result.get("handled"):
            _fum.register_upload_mapping(file_id, display, policy, allow_unknown_extension=True)
            _remember_notion_file_binding(file_id, display, len(body), mime_type)
            logger.info(
                "notion file saved id=%s name=%s (%d bytes)",
                file_id,
                display,
                len(body),
            )
        return result
    except Exception:
        logger.exception("notion file save failed: %s", file_id)
    return None


def _handle_file_cdn_response(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    if not payload or len(payload) < 32:
        return None
    # 미러된 CDN GET 쿠키에서 file_token 확보 → 이후 pptx/pdf signed fetch 에 재사용.
    _remember_file_token_from_cookie(_cookie_from_req(req))
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    raw_path = str(meta.get("path") or "")
    file_id, storage_name = _parse_file_cdn_path(raw_path)
    if not file_id:
        return None
    filename = _download_name_from_meta(req) or storage_name or file_id
    result = _save_notion_bytes(req, file_id, filename, payload)
    return result


def _is_notion_download_url(url: str) -> bool:
    low = str(url or "").lower()
    if not low.startswith("https://"):
        return False
    return (
        "file.notion.com" in low
        or ("prod-files-secure" in low and "amazonaws.com" in low)
        or "notionusercontent.com" in low
    )


def _cookie_from_req(req: Optional[Dict[str, Any]]) -> str:
    if not isinstance(req, dict):
        return ""
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    cookie = str(meta.get("cookie") or "").strip()
    if cookie:
        return cookie
    headers = meta.get("request_headers") if isinstance(meta.get("request_headers"), dict) else {}
    for key, val in headers.items():
        if str(key).lower() == "cookie" and val:
            return str(val).strip()
    return ""


def _extract_cookie_value(cookie: str, name: str) -> str:
    if not cookie or not name:
        return ""
    m = re.search(
        r"(?:^|;\s*)" + re.escape(name) + r"=([^;]+)",
        cookie,
        re.IGNORECASE,
    )
    return (m.group(1).strip() if m else "")


def _remember_file_token_from_cookie(cookie: str) -> None:
    global _CACHED_FILE_TOKEN
    token = _extract_cookie_value(cookie, "file_token")
    if token and token != _CACHED_FILE_TOKEN:
        _CACHED_FILE_TOKEN = token
        logger.info("notion cached file_token len=%d", len(token))


def _cookie_with_file_token(req: Optional[Dict[str, Any]]) -> str:
    """API 쿠키에 file_token 이 없으면 CDN에서 캐시한 값을 합친다."""
    cookie = _cookie_from_req(req)
    _remember_file_token_from_cookie(cookie)
    if _extract_cookie_value(cookie, "file_token"):
        return cookie
    if not _CACHED_FILE_TOKEN:
        return cookie
    extra = f"file_token={_CACHED_FILE_TOKEN}"
    return f"{cookie}; {extra}" if cookie else extra


def _notion_fetch_headers(req: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """브라우저 getSignedFileUrls 요청 헤더를 CDN GET에 최대한 재사용."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.notion.so/",
        "Origin": "https://www.notion.so",
        "Accept": "*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    if not isinstance(req, dict):
        cookie = _cookie_with_file_token(None)
        if cookie:
            headers["Cookie"] = cookie
        return headers
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    src = meta.get("request_headers") if isinstance(meta.get("request_headers"), dict) else {}
    lower_map = {str(k).lower(): str(v) for k, v in src.items() if v is not None}
    for name in (
        "user-agent",
        "cookie",
        "referer",
        "origin",
        "x-notion-active-user-header",
        "x-notion-space-id",
        "notion-client-version",
        "notion-audit-log-platform",
        "accept-language",
    ):
        val = lower_map.get(name) or (meta.get("cookie") if name == "cookie" else None)
        if val:
            # HTTP header names
            hdr = "-".join(p.capitalize() for p in name.split("-"))
            if name.startswith("x-") or name.startswith("notion-"):
                hdr = name  # keep lowercase-ish notion custom headers as-is
            headers[name if name.startswith("x-") or name.startswith("notion-") else hdr] = str(val).strip()
    cookie = _cookie_with_file_token(req)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _extract_notion_download_urls(payload: bytes) -> List[str]:
    """JSON 우선 파싱. regex 는 \\u0026 에서 잘리므로 보조만 사용."""
    text = _safe_utf8(payload)
    urls: List[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        u = str(url or "").strip().rstrip("\\").rstrip(",")
        if not u or not _is_notion_download_url(u) or u in seen:
            return
        seen.add(u)
        urls.append(u)

    try:
        obj = json.loads(text)
    except Exception:
        obj = None
    if isinstance(obj, dict):
        for key in ("signedGetUrl", "signed_get_url", "getUrl", "downloadUrl"):
            add(str(obj.get(key) or ""))
        for key in ("signedUrls", "urls", "getUrls"):
            nested = obj.get(key)
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, str):
                        add(item)
                    elif isinstance(item, dict):
                        add(str(item.get("url") or item.get("signedGetUrl") or ""))
            elif isinstance(nested, dict):
                for val in nested.values():
                    if isinstance(val, str):
                        add(val)

    if not urls:
        # \\u0026 → & 로 풀어 regex 보조
        expanded = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda m: chr(int(m.group(1), 16)),
            text,
        )
        for match in _HTTPS_URL_RE.finditer(expanded):
            add(match.group(0))
    return urls


def _download_signed_url(url: str, req: Optional[Dict[str, Any]] = None) -> bytes:
    text = str(url or "").strip()
    if not text or any(ord(c) < 32 for c in text):
        return b""
    headers = _notion_fetch_headers(req)
    # 브라우저 CDN GET에는 Origin 없음.
    headers.pop("Origin", None)
    headers.pop("origin", None)
    raw_cookie = str(headers.get("Cookie") or headers.get("cookie") or "")
    has_file_token = "file_token=" in raw_cookie
    http_req = urllib.request.Request(text, headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(http_req, timeout=_SIGNED_FETCH_TIMEOUT_SEC) as resp:
            chunks: List[bytes] = []
            total = 0
            while True:
                piece = resp.read(64 * 1024)
                if not piece:
                    break
                total += len(piece)
                if total > _SIGNED_FETCH_MAX_BYTES:
                    logger.warning("notion signed fetch exceeded max bytes url=%s", text[:120])
                    return b""
                chunks.append(piece)
            body = b"".join(chunks)
            logger.info("notion signed fetch ok bytes=%d", len(body))
            return body
    except urllib.error.HTTPError as exc:
        logger.warning(
            "notion signed fetch failed: %s has_file_token=%s url=%s",
            exc,
            has_file_token,
            text[:160],
        )
        return b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("notion signed fetch failed: %s url=%s", exc, text[:160])
        return b""


def _file_id_from_download_url(url: str) -> Tuple[str, str]:
    try:
        path = unquote(urlparse(str(url or "")).path or "")
    except Exception:
        path = str(url or "").split("?", 1)[0]
    file_id, storage_name = _parse_file_cdn_path(path)
    if file_id:
        return file_id, storage_name
    m = _FILE_ID_FROM_KEY_RE.search(path.replace("\\", "/"))
    if m:
        return m.group(1), os.path.basename(path)
    return "", os.path.basename(path.replace("\\", "/"))


def _handle_signed_urls_response(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    urls = _extract_notion_download_urls(payload)
    if not urls:
        return _fum.build_upload_result(handled=True)

    saved = 0
    last: Optional[Dict[str, Any]] = None
    for url in urls:
        file_id, storage_name = _file_id_from_download_url(url)
        dedupe = (file_id or url).lower()
        if dedupe in _SIGNED_FETCHED:
            continue
        existing = _fum.get_saved_attachment(file_id) if file_id else None
        if existing and os.path.isfile(str(existing.get("id") or "")):
            _SIGNED_FETCHED.add(dedupe)
            continue
        body = _download_signed_url(url, req)
        if not body:
            continue
        if not file_id:
            file_id = storage_name or f"notion-{len(body)}"
        result = _save_notion_bytes(req, file_id, storage_name or file_id, body)
        if isinstance(result, dict) and result.get("handled"):
            _SIGNED_FETCHED.add(dedupe)
            saved += 1
            last = result
    if saved:
        return last or _fum.build_upload_result(handled=True)
    return _fum.build_upload_result(handled=True)


def normalize_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    direction = str(req.get("direction") or "").lower()
    path = _path_base(req)
    host = _host(req)

    if direction == "response":
        if is_notion_upload_init_path(path):
            return _finalize_upload_init_response(req, payload)
        if is_notion_signed_urls_path(path):
            return _handle_signed_urls_response(req, payload)
        if is_notion_file_cdn_host(host):
            return _handle_file_cdn_response(req, payload)
        return None

    if direction != "request" or not payload:
        return None

    if is_notion_upload_init_path(path):
        try:
            obj = json.loads(_safe_utf8(payload))
        except Exception:
            obj = None
        if isinstance(obj, dict):
            return _handle_upload_init_request(req, obj)
        return _fum.build_upload_result(handled=True)

    if is_notion_s3_upload_host(host):
        result = _handle_s3_multipart_upload(req, payload)
        return result

    return None


def _iter_transcript_blocks(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return []
    return [x for x in tr if isinstance(x, dict)]


def _attachment_row_from_block(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if str(block.get("type") or "").lower() != "attachment":
        return None
    file_url = str(block.get("fileUrl") or block.get("file_url") or "").strip()
    file_id, storage_name = _parse_attachment_url(file_url)
    if not file_id and not file_url:
        return None
    file_name = str(block.get("fileName") or block.get("file_name") or storage_name or "").strip()
    mime = str(block.get("contentType") or block.get("content_type") or "").strip()
    meta = block.get("metadata")
    size = 0
    if isinstance(meta, dict):
        try:
            size = int(meta.get("fileSizeBytes") or meta.get("file_size_bytes") or 0)
        except Exception:
            size = 0
        if not mime:
            mime = str(meta.get("contentType") or meta.get("content_type") or "").strip()
    return {
        "file_id": file_id,
        "file_name": file_name,
        "mime_type": mime,
        "size_bytes": size,
        "storage_name": storage_name,
    }


def strip_transcript_attachments(obj: Dict[str, Any]) -> int:
    """Remove type=attachment blocks from runInferenceTranscript transcript."""
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return 0
    kept: List[Any] = []
    removed = 0
    for block in tr:
        if isinstance(block, dict) and str(block.get("type") or "").lower() == "attachment":
            removed += 1
            continue
        kept.append(block)
    if removed:
        obj["transcript"] = kept
    return removed


def _collect_transcript_attachment_refs(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for block in _iter_transcript_blocks(obj):
        row = _attachment_row_from_block(block)
        if not row:
            continue
        fid = str(row.get("file_id") or "").strip().lower()
        key = fid or str(row.get("file_name") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        refs.append(row)
    return refs


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
        if display_name and _UUID_RE.match(display_name):
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
            "notion policy attachment not on disk yet: uid=%s expected=%s",
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


def _resolve_saved_upload_id_for_ref(ref: Dict[str, Any]) -> Tuple[str, bool]:
    """transcript attachment → 저장된 upload_id. (upload_id, saved_ok)"""
    file_id = str(ref.get("file_id") or "").strip()
    name = str(ref.get("file_name") or "")
    storage_name = str(ref.get("storage_name") or "")
    mime = str(ref.get("mime_type") or "")

    candidates: List[str] = []
    if file_id:
        candidates.append(file_id)
    resolved = _resolve_notion_file_ref(file_id) if file_id else None
    if resolved and str(resolved.get("upload_id") or "").strip():
        candidates.append(str(resolved["upload_id"]))
    if file_id:
        legacy = _fum.lookup_upload_id_by_registered_server_uuid(file_id)
        if legacy:
            candidates.append(legacy)
    for hint in (name, storage_name):
        if hint:
            candidates.extend(_fum.lookup_upload_ids_by_registered_filename(hint))
    slug = _storage_slug_from_name(name or storage_name)
    if slug:
        meta = _lookup_notion_upload_meta(slug)
        if str(meta.get("upload_id") or "").strip():
            candidates.append(str(meta["upload_id"]))

    seen: set[str] = set()
    for uid in candidates:
        uid = str(uid or "").strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        if _fum.get_saved_attachment(uid, fallback_name=name, fallback_mime=mime):
            return uid, True
        # Brief wait for CDN/S3 harvest that may still be writing.
        if _fum.wait_for_saved_attachment(uid, fallback_name=name, fallback_mime=mime):
            return uid, True
    return file_id, False


def build_policy_attachments(
    req: Dict[str, Any],
    obj: Dict[str, Any],
    payload: bytes,
) -> List[Dict[str, Any]]:
    """Build file_block_filter attachments from runInferenceTranscript transcript attachments."""
    del payload
    refs = _collect_transcript_attachment_refs(obj)
    if not refs:
        return [{"id": "", "size": 0, "name": "", "mime_type": ""}]

    out: List[Dict[str, Any]] = []
    for ref in refs:
        upload_id, saved_ok = _resolve_saved_upload_id_for_ref(ref)
        name = str(ref.get("file_name") or "")
        mime = str(ref.get("mime_type") or "")
        try:
            size = int(ref.get("size_bytes") or 0)
        except Exception:
            size = 0

        if not saved_ok:
            logger.warning(
                "notion inference: unresolved attachment file_id=%s name=%s path=%s",
                str(ref.get("file_id") or ""),
                name,
                str((req.get("meta") or {}).get("path") or ""),
            )
            out.append({"id": "", "size": size, "name": name, "mime_type": mime})
            continue

        out.append(_finalize_policy_attachment(upload_id, name, mime, size))

    return out if out else [{"id": "", "size": 0, "name": "", "mime_type": ""}]


# Public entry points share file_upload_manager.UPLOAD_STATE_LOCK so the
# policy loop can run normalize/persist on a worker without racing extract.
_fum.bind_upload_state_lock(globals())
