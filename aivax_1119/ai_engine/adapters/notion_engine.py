"""
Notion AI (runInferenceTranscript) 어댑터

요청 JSON(transcript)에서 사용자 프롬프트·계정 context를,
응답 NDJSON(연속 JSON 또는 줄 단위)에서 assistant 스트림을 추출한다.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from ai_service_codes import AI_SERVICE_NOTION

try:
    import account_lookup as _account_lookup
except ImportError:
    _account_lookup = None

from adapters import notion_upload as _notion_upload
from adapters.notion_upload import (
    POLICY_ATTACHMENTS_FINAL,
    build_policy_attachments,
    is_notion_file_cdn_host,
    is_notion_s3_upload_request,
    is_notion_signed_urls_path,
    is_notion_upload_init_request,
    strip_transcript_attachments,
)

_INFERENCE_PATH_MARKER = "runinferencetranscript"
_SAVE_TRANSACTIONS_PATH_MARKER = "savetransactionsfanout"
_CONTENT_PATCH_RE = re.compile(r"/s/\d+/value/(\d+)/content$")
_EDIT_REFERENCE_RE = re.compile(
    r"<edit_reference\b[^>]*>.*?</edit_reference>",
    re.DOTALL | re.IGNORECASE,
)

# 스트리밍 응답이 청크로 나뉠 때 NDJSON 파싱 잔여·누적 객체 (request_id 기준)
_NOTION_TAIL_BY_RID: Dict[str, str] = {}
_NOTION_OBJS_BY_RID: Dict[str, List[Dict[str, Any]]] = {}
_NOTION_RID_ORDER: List[str] = []
_NOTION_MAP_LIMIT = 256
# 요청(request_id) → threadId (응답 쪽 세션 매칭용)
_NOTION_THREAD_BY_RID: Dict[str, str] = {}
# BLOCK 시 응답 NDJSON에 차단 문구를 한 번만 싣고 이후 스트림 덧붙임 제거 (threadId 또는 request_id 키)
_NOTION_BLOCK_RW_EMITTED: Dict[str, bool] = {}
# runInferenceTranscript context → saveTransactions 등 후속 요청에서 user_id/email 복원
_NOTION_CONTEXT_BY_THREAD: Dict[str, Dict[str, Any]] = {}
_NOTION_CONTEXT_THREAD_ORDER: List[str] = []
_NOTION_CONTEXT_LIMIT = 256
# policy input용 userId/userEmail — [MASKED]로 덮어쓰지 않음
_NOTION_IDENTITY_BY_THREAD: Dict[str, Dict[str, str]] = {}

logger = logging.getLogger(__name__)


def _touch_rid(rid: str) -> None:
    if not rid:
        return
    try:
        _NOTION_RID_ORDER.remove(rid)
    except ValueError:
        pass
    _NOTION_RID_ORDER.append(rid)
    while len(_NOTION_RID_ORDER) > _NOTION_MAP_LIMIT:
        old = _NOTION_RID_ORDER.pop(0)
        _clear_rid_buffers(old)


def _clear_rid_buffers(rid: str) -> None:
    if not rid:
        return
    _NOTION_TAIL_BY_RID.pop(rid, None)
    _NOTION_OBJS_BY_RID.pop(rid, None)
    tid = _NOTION_THREAD_BY_RID.pop(rid, None)
    if tid:
        _NOTION_BLOCK_RW_EMITTED.pop(tid, None)
    _NOTION_BLOCK_RW_EMITTED.pop(rid, None)
    try:
        _NOTION_RID_ORDER.remove(rid)
    except ValueError:
        pass


def clear_response_override_state(session_id: str) -> None:
    """세션 종료 시 버퍼 정리(다른 어댑터와 동일 인터페이스)."""
    sid = (session_id or "").strip()
    if not sid:
        return
    _NOTION_BLOCK_RW_EMITTED.pop(sid, None)
    stale = [rid for rid, objs in _NOTION_OBJS_BY_RID.items() if _session_hint_from_objs(objs) == sid]
    for rid in stale or [sid]:
        _clear_rid_buffers(rid)


def _session_hint_from_objs(objs: List[Dict[str, Any]]) -> str:
    for o in objs:
        if not isinstance(o, dict):
            continue
        tid = o.get("threadId")
        if isinstance(tid, str) and tid:
            return tid
        tr = o.get("traceId")
        if isinstance(tr, str) and tr:
            return tr
    return ""


def _safe_utf8(data: Union[bytes, bytearray, str]) -> str:
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _path_from_meta(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("path") or "")


def _host_from_meta(req: Dict[str, Any]) -> str:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    return str(meta.get("host") or "").lower()


def _is_notion_host(req: Dict[str, Any]) -> bool:
    host = _host_from_meta(req)
    return "notion.so" in host or host.endswith("notion.site") or "notion.com" in host


def is_notion_inference_transcript(req: Dict[str, Any]) -> bool:
    path = _path_from_meta(req).lower()
    if _INFERENCE_PATH_MARKER not in path:
        return False
    return _is_notion_host(req)


def is_notion_save_transactions(req: Dict[str, Any]) -> bool:
    path = _path_from_meta(req).lower()
    if _SAVE_TRANSACTIONS_PATH_MARKER not in path:
        return False
    return _is_notion_host(req)


def _infer_notion_request_kind(req: Dict[str, Any]) -> str:
    path = _path_from_meta(req).lower()
    if _SAVE_TRANSACTIONS_PATH_MARKER in path:
        return "save_transactions"
    return "inference"


def _iter_save_transaction_user_steps(
    obj: Dict[str, Any],
) -> Iterator[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """(operation, step) 쌍 중 type=user/user-injected 만."""
    for tx in obj.get("transactions") or []:
        if not isinstance(tx, dict):
            continue
        for op in tx.get("operations") or []:
            if not isinstance(op, dict):
                continue
            args = op.get("args")
            if not isinstance(args, dict):
                continue
            step = args.get("step")
            if isinstance(step, dict) and _is_prompt_like_block(step):
                yield op, step


def _extract_prompt_from_save_transactions(obj: Dict[str, Any]) -> str:
    latest = ""
    for _, step in _iter_save_transaction_user_steps(obj):
        t = _user_text_from_transcript_item(step)
        if _is_meaningful_user_prompt(t):
            latest = t
    return latest.strip()


def _touch_context_thread(tid: str) -> None:
    if not tid:
        return
    try:
        _NOTION_CONTEXT_THREAD_ORDER.remove(tid)
    except ValueError:
        pass
    _NOTION_CONTEXT_THREAD_ORDER.append(tid)
    while len(_NOTION_CONTEXT_THREAD_ORDER) > _NOTION_CONTEXT_LIMIT:
        old = _NOTION_CONTEXT_THREAD_ORDER.pop(0)
        _NOTION_CONTEXT_BY_THREAD.pop(old, None)
        _NOTION_IDENTITY_BY_THREAD.pop(old, None)


def _context_pii_is_masked(ctx: Dict[str, Any]) -> bool:
    if not ctx:
        return False
    email = str(ctx.get("userEmail") or "")
    uid = str(ctx.get("userId") or "")
    return email == "[MASKED]" or uid == "[MASKED]"


def _resolve_notion_context(
    incoming: Optional[Dict[str, Any]], thread_id: str
) -> Dict[str, Any]:
    """[MASKED] transcript context보다 캐시된 원본 계정 정보를 우선."""
    inc = dict(incoming) if isinstance(incoming, dict) else {}
    cached = _lookup_cached_context(thread_id) if thread_id else {}
    if inc and not _context_pii_is_masked(inc):
        return inc
    if cached and not _context_pii_is_masked(cached):
        return dict(cached)
    if inc:
        return inc
    return dict(cached) if cached else {}


def _cache_context_for_thread(tid: str, ctx: Dict[str, Any]) -> None:
    if not tid or not ctx:
        return
    if _context_pii_is_masked(ctx):
        existing = _lookup_cached_context(tid)
        if existing and not _context_pii_is_masked(existing):
            return
    _NOTION_CONTEXT_BY_THREAD[tid] = dict(ctx)
    _touch_context_thread(tid)


def _lookup_cached_context(tid: str) -> Dict[str, Any]:
    if not tid:
        return {}
    ctx = _NOTION_CONTEXT_BY_THREAD.get(tid)
    return dict(ctx) if isinstance(ctx, dict) else {}


def _stash_notion_identity(thread_id: str, ctx: Dict[str, Any]) -> None:
    """원본 userId/userEmail을 보존(policy API·로그용). [MASKED] 값은 저장하지 않음."""
    if not thread_id or not ctx or _context_pii_is_masked(ctx):
        return
    uid = str(ctx.get("userId") or "").strip()
    email = str(ctx.get("userEmail") or "").strip()
    space = str(ctx.get("spaceViewId") or "").strip()
    if not uid and not email:
        return
    _NOTION_IDENTITY_BY_THREAD[thread_id] = {"userId": uid, "userEmail": email}
    if _account_lookup:
        _account_lookup.set_account("notion", thread_id, uid, email)
        if space and space != "[MASKED]":
            _account_lookup.set_account("notion", f"space:{space}", uid, email)


def _lookup_notion_identity(
    thread_id: str, ctx_hint: Optional[Dict[str, Any]] = None
) -> Dict[str, str]:
    if thread_id:
        hit = _NOTION_IDENTITY_BY_THREAD.get(thread_id)
        if hit and not _context_pii_is_masked(hit):
            return dict(hit)
        if _account_lookup:
            info = _account_lookup.get("notion", thread_id)
            if info and (info[0] or info[1]):
                return {"userId": info[0], "userEmail": info[1]}
    hint = ctx_hint if isinstance(ctx_hint, dict) else {}
    space = str(hint.get("spaceViewId") or "").strip()
    if space and space != "[MASKED]" and _account_lookup:
        info = _account_lookup.get("notion", f"space:{space}")
        if info and (info[0] or info[1]):
            return {"userId": info[0], "userEmail": info[1]}
    return {}


def _apply_notion_identity(ctx: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    out = dict(ctx) if ctx else {}
    uid = str(out.get("userId") or "")
    email = str(out.get("userEmail") or "")
    if uid == "[MASKED]" or email == "[MASKED]" or (not uid and not email):
        ident = _lookup_notion_identity(thread_id, out)
        if ident:
            if ident.get("userId") and ident["userId"] != "[MASKED]":
                out["userId"] = ident["userId"]
            if ident.get("userEmail") and ident["userEmail"] != "[MASKED]":
                out["userEmail"] = ident["userEmail"]
    return out


def _is_save_transactions_admin_operation(obj: Dict[str, Any]) -> bool:
    """thread 삭제·alive 업데이트 등 user 프롬프트가 없는 saveTransactions."""
    if _extract_prompt_from_save_transactions(obj):
        return False
    for tx in obj.get("transactions") or []:
        if not isinstance(tx, dict):
            continue
        for op in tx.get("operations") or []:
            if isinstance(op, dict):
                return True
    return False


def notion_should_skip_policy(req: Dict[str, Any]) -> bool:
    """정책 API·프롬프트 rewrite 대상이 아닌 Notion 요청."""
    if req.get("_notion_skip_policy"):
        return True
    if is_notion_inference_transcript(req):
        if str(req.get("_notion_extracted_prompt") or "").strip():
            return False
        if req.get("_notion_has_attachments"):
            return False
        return True
    if not is_notion_save_transactions(req):
        return False
    return not bool(str(req.get("_notion_extracted_prompt") or "").strip())


def _extract_thread_id_from_save_transactions(obj: Dict[str, Any]) -> str:
    for tx in obj.get("transactions") or []:
        if not isinstance(tx, dict):
            continue
        for op in tx.get("operations") or []:
            if not isinstance(op, dict):
                continue
            args = op.get("args")
            if not isinstance(args, dict):
                continue
            pid = args.get("parent_id")
            if isinstance(pid, str) and pid.strip():
                return pid.strip()
    return ""


def _register_save_transactions_context(req: Dict[str, Any], obj: Dict[str, Any]) -> None:
    tid = _extract_thread_id_from_save_transactions(obj)
    rid = str(req.get("request_id") or "")
    if tid:
        req["_notion_thread_id"] = tid
        if rid:
            _NOTION_THREAD_BY_RID[rid] = tid
        resolved = _apply_notion_identity(_resolve_notion_context(None, tid), tid)
        if resolved:
            req["_notion_context"] = resolved


def _iter_json_documents(text: str) -> Iterator[Dict[str, Any]]:
    dec = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = dec.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        idx = end
        if isinstance(obj, dict):
            yield obj


def _consume_json_values(buffer: str) -> Tuple[List[Dict[str, Any]], str]:
    objs: List[Dict[str, Any]] = []
    dec = json.JSONDecoder()
    idx = 0
    n = len(buffer)
    while idx < n:
        while idx < n and buffer[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = dec.raw_decode(buffer, idx)
        except json.JSONDecodeError:
            break
        idx = end
        if isinstance(obj, dict):
            objs.append(obj)
    return objs, buffer[idx:]


def _flatten_rich_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_flatten_rich_value(x) for x in value)
    return ""


_PROMPT_LIKE_TYPES = frozenset({"user", "user-injected"})


def _is_prompt_like_block(item: Dict[str, Any]) -> bool:
    return str(item.get("type") or "").lower() in _PROMPT_LIKE_TYPES


def _user_text_from_transcript_item(item: Dict[str, Any]) -> str:
    if not _is_prompt_like_block(item):
        return ""
    raw = item.get("value")
    return _flatten_rich_value(raw).strip()


def _first_context_block(obj: Dict[str, Any]) -> Dict[str, Any]:
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return {}
    for it in tr:
        if isinstance(it, dict) and it.get("type") == "context":
            val = it.get("value")
            if isinstance(val, dict):
                return val
    return {}


def _best_context_block(obj: Dict[str, Any]) -> Dict[str, Any]:
    """transcript 내 context 중 [MASKED]가 아닌 블록을 우선 반환."""
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return {}
    fallback: Dict[str, Any] = {}
    for it in tr:
        if not isinstance(it, dict) or str(it.get("type") or "").lower() != "context":
            continue
        val = it.get("value")
        if not isinstance(val, dict):
            continue
        if not fallback:
            fallback = val
        if not _context_pii_is_masked(val):
            return dict(val)
    return dict(fallback) if fallback else {}


_ATTACHMENT_ONLY_USER_TEXTS = frozenset(
    {
        "pdf attachment",
        "image attachment",
        "file attachment",
        "csv attachment",
    }
)


def _is_meaningful_user_prompt(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return t.lower() not in _ATTACHMENT_ONLY_USER_TEXTS


def _extract_latest_user_prompt(obj: Dict[str, Any]) -> str:
    """transcript 내 마지막 실질 사용자 메시지(첨부 플레이스홀더 제외)."""
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return ""
    latest = ""
    for it in tr:
        if not isinstance(it, dict):
            continue
        if not _is_prompt_like_block(it):
            continue
        t = _user_text_from_transcript_item(it)
        if _is_meaningful_user_prompt(t):
            latest = t
    return latest.strip()


def _extract_agent_prebuilt_prompt(obj: Dict[str, Any]) -> str:
    """custom agent 생성 등 agent-prebuilt-prompt 블록 텍스트."""
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return ""
    latest = ""
    for it in tr:
        if not isinstance(it, dict):
            continue
        if str(it.get("type") or "").lower() != "agent-prebuilt-prompt":
            continue
        t = _flatten_rich_value(it.get("value")).strip()
        if t:
            latest = t
    return latest


def _extract_policy_prompt_from_obj(obj: Dict[str, Any]) -> str:
    """정책 API·rewrite 대상 텍스트(user → agent-prebuilt 순)."""
    user = _extract_latest_user_prompt(obj)
    if user:
        return user
    return _extract_agent_prebuilt_prompt(obj)


def _is_notion_workflow_setup_request(obj: Dict[str, Any]) -> bool:
    """에이전트 생성·workflow 스레드 등 구조적 inference 요청."""
    if obj.get("createThread") is True:
        return True
    if str(obj.get("threadType") or "").lower() == "workflow":
        return True
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return False
    for it in tr:
        if not isinstance(it, dict):
            continue
        t = str(it.get("type") or "").lower()
        if t == "agent-prebuilt-prompt":
            return True
        if t == "config":
            val = it.get("value")
            if isinstance(val, dict) and (
                val.get("isCustomAgent")
                or val.get("isCustomAgentBuilder")
                or val.get("type") == "workflow"
            ):
                return True
    return False


def _all_user_prompts(obj: Dict[str, Any]) -> str:
    return _extract_policy_prompt_from_obj(obj)


def _has_transcript_attachments(obj: Dict[str, Any]) -> bool:
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return False
    return any(
        isinstance(it, dict) and str(it.get("type") or "").lower() == "attachment"
        for it in tr
    )


def _register_request_context(req: Dict[str, Any], obj: Dict[str, Any]) -> None:
    incoming = _best_context_block(obj)
    tid = obj.get("threadId")
    tid_s = tid.strip() if isinstance(tid, str) else ""
    if tid_s:
        req["_notion_thread_id"] = tid_s
        if incoming and not _context_pii_is_masked(incoming):
            _stash_notion_identity(tid_s, incoming)
        resolved = _apply_notion_identity(_resolve_notion_context(incoming, tid_s), tid_s)
        if resolved:
            req["_notion_context"] = resolved
            if not _context_pii_is_masked(resolved):
                _cache_context_for_thread(tid_s, resolved)
    elif incoming:
        req["_notion_context"] = incoming
    rid = str(req.get("request_id") or "")
    if isinstance(tid, str) and tid and rid:
        _NOTION_THREAD_BY_RID[rid] = tid
    trid = obj.get("traceId")
    if isinstance(trid, str) and trid:
        req["_notion_trace_id"] = trid


def _agent_inference_seed_text(v: Dict[str, Any]) -> str:
    val = v.get("value")
    if not isinstance(val, list):
        return ""
    return _join_agent_inference_text_blocks(val)


def _join_blocks_dict(blocks: Dict[int, str]) -> str:
    if not blocks:
        return ""
    cleaned: Dict[int, str] = {}
    for idx, text in blocks.items():
        part = _strip_internal_markup(text)
        if part:
            cleaned[idx] = part
    if not cleaned:
        return ""
    return "".join(cleaned[i] for i in sorted(cleaned))


def _seed_blocks_from_agent_inference(inner: Dict[str, Any]) -> Dict[int, str]:
    blocks: Dict[int, str] = {}
    val = inner.get("value")
    if not isinstance(val, list):
        return blocks
    for i, block in enumerate(val):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        c = block.get("content")
        if isinstance(c, str) and c.strip():
            blocks[i] = c
    return blocks


def _join_agent_inference_text_blocks(blocks: List[Any]) -> str:
    by_idx: Dict[int, str] = {}
    for i, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        c = block.get("content")
        if isinstance(c, str) and c.strip():
            by_idx[i] = c
    return _join_blocks_dict(by_idx)


def _strip_internal_markup(text: str) -> str:
    t = _EDIT_REFERENCE_RE.sub("", text or "")
    t = re.sub(r"<edit_reference\b[^>]*/>", "", t, flags=re.IGNORECASE)
    return t.strip()


_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")

_INTERNAL_AGENT_TEXT_MARKERS = (
    '"function":',
    '"function" :',
    '{"function":',
    "connections.fs.",
    "adminUserConnections",
    "integration://",
    "updateAgent",
    "Let me now explain",
    "Looking at the current",
    "is complete. Let me",
    "I need to:",
    "{{agent-",
    "modules/notion/integration",
)

_ENGLISH_REASONING_MARKERS = (
    "which means",
    "in Korean",
    "not a task request",
    "This is just a",
    "The user is",
    "The user said",
    "casual exclamation",
    "Let me ",
    "I'll ",
    "I need to ",
)


def _contains_english_reasoning(text: str) -> bool:
    return any(m in (text or "") for m in _ENGLISH_REASONING_MARKERS)


def _is_internal_agent_text(text: str) -> bool:
    t = _strip_internal_markup(text or "")
    if not t:
        return True
    for marker in _INTERNAL_AGENT_TEXT_MARKERS:
        if marker in t:
            return True
    if t.startswith("{") and '"function"' in t[:300]:
        return True
    if t.startswith("me read the") and "integration.ts" in t:
        return True
    if re.search(r"</?edit_reference\b", text or "", re.IGNORECASE) and not _HANGUL_RE.search(t):
        return True
    return False



def _clean_assistant_segment(text: str) -> str:
    """단일 agent-inference 블록에서 영문 추론 접두를 제거하고 사용자 대면 문장만 남긴다."""
    t = _strip_internal_markup(text or "")
    if not t or _is_internal_agent_text(t):
        return ""
    if _contains_english_reasoning(t) and _HANGUL_RE.search(t):
        emoji_ko = re.search(
            r"[\U0001F300-\U0001FAFF\u2600-\u27BF]\s*([\uac00-\ud7a3].+)",
            t,
            re.DOTALL,
        )
        if emoji_ko and len(emoji_ko.group(1).strip()) >= 6:
            prefix = t[: emoji_ko.start()]
            if _contains_english_reasoning(prefix) or (
                len(prefix) > 40 and len(_HANGUL_RE.findall(prefix)) < 3
            ):
                return emoji_ko.group(1).strip()
        for marker in _ENGLISH_REASONING_MARKERS:
            pos = t.find(marker)
            if pos < 0:
                continue
            after = t[pos + len(marker) :]
            hm = _HANGUL_RE.search(after)
            if hm:
                tail = after[hm.start() :].strip()
                if len(tail) >= 6:
                    return tail
        hm = _HANGUL_RE.search(t)
        if hm and hm.start() > 40:
            prefix = t[: hm.start()]
            if len(_HANGUL_RE.findall(prefix)) < 3:
                return t[hm.start() :].strip()
    return t


def _score_user_facing_segment(text: str) -> int:
    if not text or _is_internal_agent_text(text):
        return -1
    score = len(text)
    if _HANGUL_RE.search(text):
        score += 200
    if _contains_english_reasoning(text):
        score -= 100
    return score


def _pick_user_facing_assistant_text(candidates: List[str]) -> str:
    segments: List[str] = []
    for c in candidates:
        cleaned = _clean_assistant_segment(c)
        if cleaned:
            segments.append(cleaned)
    if not segments:
        return ""
    return max(segments, key=_score_user_facing_segment)


def _merge_assistant_snap_and_stream(
    snap: str, stream: str, *, prefer_snap: bool = False
) -> str:
    snap_c = _clean_assistant_segment(snap)
    stream_c = _clean_assistant_segment(stream)
    if prefer_snap and snap_c:
        if not stream_c or len(snap_c) >= len(stream_c) * 0.85:
            return snap_c
    if snap_c and stream_c:
        if _contains_english_reasoning(stream) and _HANGUL_RE.search(snap_c):
            if not _contains_english_reasoning(snap_c):
                return snap_c
        snap_score = _score_user_facing_segment(snap_c)
        stream_score = _score_user_facing_segment(stream_c)
        if snap_score >= stream_score:
            return snap_c
        return stream_c
    return snap_c or stream_c


def _walk_agent_inference_contents(obj: Any, out: List[str]) -> None:
    if isinstance(obj, dict):
        step = obj.get("step")
        if isinstance(step, dict) and step.get("type") == "agent-inference":
            val = step.get("value")
            if isinstance(val, list):
                joined = _join_agent_inference_text_blocks(val)
                if joined:
                    out.append(joined)
        for v in obj.values():
            _walk_agent_inference_contents(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _walk_agent_inference_contents(x, out)


def _last_record_map_object(objs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    last: Optional[Dict[str, Any]] = None
    for o in objs:
        if isinstance(o, dict) and isinstance(o.get("recordMap"), dict):
            last = o
    return last


def _snapshot_assistant_from_objects(objs: List[Dict[str, Any]]) -> str:
    """최종 record-map 등에 들어 있는 agent-inference 텍스트(사용자 대면 문장 우선)."""
    candidates: List[str] = []
    rm = _last_record_map_object(objs)
    sources: List[Any] = [rm] if rm else list(objs)
    for o in sources:
        if not isinstance(o, dict):
            continue
        acc: List[str] = []
        _walk_agent_inference_contents(o, acc)
        for t in acc:
            if t:
                candidates.append(t)
    return _pick_user_facing_assistant_text(candidates)


def _stream_assistant_from_patches(objs: List[Dict[str, Any]]) -> str:
    """patch 스트림(o=a agent-inference 시드 + o=x content 덧붙이기) 재구성."""
    segments: List[str] = []
    current_blocks: Dict[int, str] = {}

    def flush_blocks() -> None:
        nonlocal current_blocks
        joined = _join_blocks_dict(current_blocks)
        if joined:
            segments.append(joined)
        current_blocks = {}

    for o in objs:
        if not isinstance(o, dict) or o.get("type") != "patch":
            continue
        vlist = o.get("v")
        if not isinstance(vlist, list):
            continue
        for op in vlist:
            if not isinstance(op, dict):
                continue
            kind = op.get("o")
            p = str(op.get("p") or "")
            if kind == "a":
                inner = op.get("v")
                if isinstance(inner, dict) and inner.get("type") == "agent-inference":
                    flush_blocks()
                    current_blocks = _seed_blocks_from_agent_inference(inner)
                elif p.endswith("/value/-") and isinstance(inner, dict) and inner.get("type") == "text":
                    c = inner.get("content")
                    if isinstance(c, str):
                        next_idx = max(current_blocks.keys(), default=-1) + 1
                        current_blocks[next_idx] = current_blocks.get(next_idx, "") + c
            elif kind == "x":
                frag = op.get("v")
                if not isinstance(frag, str):
                    continue
                m = _CONTENT_PATCH_RE.search(p)
                if m or p.endswith("/content"):
                    idx = int(m.group(1)) if m else 0
                    current_blocks[idx] = current_blocks.get(idx, "") + frag

    flush_blocks()
    return _pick_user_facing_assistant_text(segments)


def _response_stream_done_hint(objs: List[Dict[str, Any]]) -> bool:
    blob = json.dumps(objs, ensure_ascii=False) if objs else ""
    return '"completed": true' in blob and "agent-turn-full-record-map" in blob


def assistant_text_from_ndjson(text: str) -> str:
    """파일/버퍼 전체 NDJSON 문자열에서 assistant 답변 추출(공개 헬퍼)."""
    objs = list(_iter_json_documents(text))
    snap = _snapshot_assistant_from_objects(objs)
    stream = _stream_assistant_from_patches(objs)
    done = _response_stream_done_hint(objs)
    return _merge_assistant_snap_and_stream(snap, stream, prefer_snap=done).strip()


def extract_user_and_context_from_request_body(payload: Union[bytes, str]) -> Tuple[str, Dict[str, Any]]:
    """요청 바디에서 (사용자 프롬프트, 첫 context dict) 반환."""
    text = _safe_utf8(payload)
    try:
        obj = json.loads(text)
    except Exception:
        return "", {}
    if not isinstance(obj, dict):
        return "", {}
    return _all_user_prompts(obj), _first_context_block(obj)


def normalize_upload(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    host = str(meta.get("host") or "").lower()
    path = str(meta.get("path") or req.get("path") or "").lower()
    should_forward = (
        is_notion_upload_init_request(req)
        or is_notion_s3_upload_request(req)
        or is_notion_file_cdn_host(host)
        or is_notion_signed_urls_path(path)
    )
    if should_forward:
        return _notion_upload.normalize_upload(req, payload)
    return None


def extract(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    direction = (req.get("direction") or "").lower()
    if is_notion_save_transactions(req):
        if direction != "request":
            return None
        rid = str(req.get("request_id") or "")
        _clear_rid_buffers(rid)
        req["_notion_request_kind"] = "save_transactions"
        unit_type = (req.get("unit_type") or "").lower()
        content_type = (req.get("content_type") or "").lower()
        if unit_type != "json_body" and "application/json" not in content_type:
            return None
        try:
            obj = json.loads(_safe_utf8(payload))
        except Exception:
            req["_extract_final"] = True
            return _safe_utf8(payload)
        if not isinstance(obj, dict):
            return None
        _register_save_transactions_context(req, obj)
        if _is_save_transactions_admin_operation(obj):
            req["_notion_skip_policy"] = True
        prompt = _extract_prompt_from_save_transactions(obj)
        req["_notion_extracted_prompt"] = prompt or ""
        req["_extract_final"] = True
        return prompt or None

    if not is_notion_inference_transcript(req):
        return None

    unit_type = (req.get("unit_type") or "").lower()
    content_type = (req.get("content_type") or "").lower()
    rid = str(req.get("request_id") or "")

    if direction == "request":
        _clear_rid_buffers(rid)
        if unit_type == "json_body" or "application/json" in content_type:
            text = _safe_utf8(payload)
            try:
                obj = json.loads(text)
            except Exception:
                req["_extract_final"] = True
                return None
            if isinstance(obj, dict):
                _register_request_context(req, obj)
                has_attachments = _has_transcript_attachments(obj)
                req["_notion_has_attachments"] = has_attachments
                prompt = _extract_policy_prompt_from_obj(obj)
                req["_notion_extracted_prompt"] = prompt or ""
                if prompt:
                    req["_extract_final"] = True
                    return prompt
                if has_attachments:
                    placeholder = "사용자가 파일을 업로드하였습니다."
                    req["_notion_extracted_prompt"] = placeholder
                    req["_extract_final"] = True
                    return placeholder
                if _is_notion_workflow_setup_request(obj):
                    req["_notion_skip_policy"] = True
                    req["_extract_final"] = True
                    return None
                req["_extract_final"] = True
                return None
        return None

    if direction != "response":
        return None

    if not (
        "ndjson" in content_type
        or "application/json" in content_type
        or unit_type in ("json_body", "raw_body", "")
    ):
        return None

    _touch_rid(rid)
    acc = _NOTION_TAIL_BY_RID.get(rid, "") + _safe_utf8(payload)
    new_objs, tail = _consume_json_values(acc)
    _NOTION_TAIL_BY_RID[rid] = tail
    bucket = _NOTION_OBJS_BY_RID.setdefault(rid, [])
    bucket.extend(new_objs)

    snap = _snapshot_assistant_from_objects(bucket)
    stream = _stream_assistant_from_patches(bucket)
    done = _response_stream_done_hint(bucket)
    merged = _merge_assistant_snap_and_stream(snap, stream, prefer_snap=done)
    req["_extract_final"] = done
    return merged


def extract_full_body(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    """Suricata 등 전체 바디 한 번에 넘기는 경로."""
    direction = (req.get("direction") or "").lower()
    if is_notion_save_transactions(req):
        if direction != "request":
            return None
        req["_notion_request_kind"] = "save_transactions"
        try:
            obj = json.loads(_safe_utf8(payload))
        except Exception:
            return None
        if isinstance(obj, dict):
            _register_save_transactions_context(req, obj)
            if _is_save_transactions_admin_operation(obj):
                req["_notion_skip_policy"] = True
            out = _extract_prompt_from_save_transactions(obj)
            req["_notion_extracted_prompt"] = out or ""
            req["_extract_final"] = True
            return out or None
        return None

    if not is_notion_inference_transcript(req):
        return None
    text = _safe_utf8(payload)

    if direction == "request":
        try:
            obj = json.loads(text)
        except Exception:
            return text.strip() or None
        if isinstance(obj, dict):
            _register_request_context(req, obj)
            out = _extract_policy_prompt_from_obj(obj)
            req["_notion_extracted_prompt"] = out or ""
            req["_extract_final"] = True
            if out:
                return out
            if _has_transcript_attachments(obj):
                return "사용자가 파일을 업로드하였습니다."
            if _is_notion_workflow_setup_request(obj):
                req["_notion_skip_policy"] = True
            return None
        return None

    if direction == "response":
        out = assistant_text_from_ndjson(text)
        req["_extract_final"] = bool(out and out.strip())
        return out.strip() if out else None

    return None


def extract_session_id(req: Dict[str, Any], payload: bytes) -> str:
    rid = str(req.get("request_id") or "")
    if is_notion_save_transactions(req):
        hint = str(req.get("_notion_thread_id") or _NOTION_THREAD_BY_RID.get(rid) or "")
        if hint:
            return hint
        try:
            obj = json.loads(_safe_utf8(payload))
        except Exception:
            return ""
        if isinstance(obj, dict):
            return _extract_thread_id_from_save_transactions(obj)
        return ""

    if not is_notion_inference_transcript(req):
        return ""
    hint = str(req.get("_notion_thread_id") or _NOTION_THREAD_BY_RID.get(rid) or "")
    if hint:
        return hint
    direction = (req.get("direction") or "").lower()
    if direction == "request":
        try:
            obj = json.loads(_safe_utf8(payload))
        except Exception:
            return ""
        if isinstance(obj, dict):
            tid = obj.get("threadId")
            if isinstance(tid, str) and tid:
                return tid
            tr = obj.get("traceId")
            if isinstance(tr, str) and tr:
                return tr
    return ""


def build_policy_api_payload(
    req: Dict[str, Any], payload: bytes, extracted_text: str
) -> Optional[Dict[str, Any]]:
    if not (is_notion_inference_transcript(req) or is_notion_save_transactions(req)):
        return None
    direction = (req.get("direction") or "").lower()
    meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
    client_host = str(meta.get("client_ip") or meta.get("host") or "")

    ctx = req.get("_notion_context")
    if not isinstance(ctx, dict):
        ctx = {}

    rid = str(req.get("request_id") or "")
    sess = str(req.get("_notion_thread_id") or _NOTION_THREAD_BY_RID.get(rid) or "")

    payload_ctx: Dict[str, Any] = {}
    if direction == "request":
        try:
            parsed = json.loads(_safe_utf8(payload))
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            payload_ctx = _best_context_block(parsed)

    ctx = _apply_notion_identity(
        _resolve_notion_context(ctx or payload_ctx or None, sess), sess
    )
    if ctx:
        req["_notion_context"] = ctx

    uid = str(ctx.get("userId") or "")
    email = str(ctx.get("userEmail") or "")

    if direction == "response":
        return {
            "llm_output": extracted_text,
            "user_id": uid,
            "email": email,
            "ai_service": AI_SERVICE_NOTION,
            "client_host": client_host,
            "session_id": sess,
            "message_id": rid,
        }

    obj: Optional[Dict[str, Any]] = None
    try:
        parsed = json.loads(_safe_utf8(payload))
        if isinstance(parsed, dict):
            obj = parsed
    except Exception:
        obj = None

    prompt = extracted_text
    attachments: List[Dict[str, Any]] = []
    if obj is not None:
        if is_notion_save_transactions(req):
            latest = _extract_prompt_from_save_transactions(obj)
            if latest:
                prompt = latest
        else:
            latest = _extract_policy_prompt_from_obj(obj)
            if latest:
                prompt = latest
            elif _has_transcript_attachments(obj) and not (prompt or "").strip():
                prompt = "사용자가 파일을 업로드하였습니다."
            attachments = build_policy_attachments(req, obj or {}, payload)

    return {
        "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
        "prompt": prompt,
        "encoding": False,
        "user_id": uid,
        "email": email,
        "ai_service": AI_SERVICE_NOTION,
        "client_host": client_host,
        "session_id": sess,
        "attachments": attachments,
        "message_id": rid,
    }


def preprocess(req: Dict[str, Any], payload: bytes) -> Optional[bytes]:
    """gzip 바디를 한 번 풀어준다(요청·응답). 매직 바이트(1f 8b)로만 판별."""
    if not (is_notion_inference_transcript(req) or is_notion_save_transactions(req)):
        return None
    if len(payload) < 2 or payload[0:2] != b"\x1f\x8b":
        return None
    import zlib

    try:
        return zlib.decompress(payload, 16 + zlib.MAX_WBITS)
    except Exception:
        logger.debug("notion preprocess gzip decompress failed", exc_info=True)
        return None


def rewrite(
    req: Dict[str, Any], payload: bytes, src: str, dst: str
) -> Optional[Tuple[bytes, int]]:
    return None


_BLOCK_SYSINST_VERTICAL_GAP = "\n" * 2


def _payload_was_gzip(payload: bytes) -> bool:
    return len(payload) >= 2 and payload[0:2] == b"\x1f\x8b"


def _decode_payload_bytes(req: Dict[str, Any], payload: bytes) -> bytes:
    """gzip 응답/요청이면 한 번 풀어 rewrite·파싱에 사용한다."""
    if not payload:
        return payload
    if _payload_was_gzip(payload):
        out = preprocess(req, payload)
        if isinstance(out, (bytes, bytearray)) and out:
            return bytes(out)
    return payload


def _encode_payload_bytes(req: Dict[str, Any], body: bytes, original_payload: bytes) -> bytes:
    """원본이 gzip이면 rewrite 결과도 gzip으로 다시 감싼다."""
    if not _payload_was_gzip(original_payload):
        return body
    import zlib

    try:
        return zlib.compress(body, 9, 16 + zlib.MAX_WBITS)
    except Exception:
        logger.debug("notion gzip recompress failed", exc_info=True)
        return body


def _parse_request_dict(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    if (req.get("direction") or "").lower() != "request":
        return None
    if not (is_notion_inference_transcript(req) or is_notion_save_transactions(req)):
        return None
    req["_notion_request_kind"] = _infer_notion_request_kind(req)
    raw = _decode_payload_bytes(req, payload)
    try:
        obj = json.loads(_safe_utf8(raw))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _find_latest_user_replace_index(tr: List[Any]) -> Optional[int]:
    """extract와 동일: 마지막 실질 prompt-like 블록 우선, 없으면 마지막 user 블록."""
    fallback: Optional[int] = None
    for idx in range(len(tr) - 1, -1, -1):
        it = tr[idx]
        if not isinstance(it, dict) or not _is_prompt_like_block(it):
            continue
        if fallback is None:
            fallback = idx
        if _is_meaningful_user_prompt(_user_text_from_transcript_item(it)):
            return idx
    return fallback


def _set_prompt_like_value(item: Dict[str, Any], text: str) -> None:
    item["value"] = [[text]]


def _redact_sensitive_prompt_in_transcript(
    obj: Dict[str, Any], sensitive: str, replacement: str
) -> int:
    """연속 대화에서 중복·user-injected 등에 남은 동일 프롬프트를 모두 치환."""
    sens = (sensitive or "").strip()
    repl = str(replacement or "")
    if not sens or not repl:
        return 0
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return 0
    changed = 0

    def walk_replace(node: Any) -> Any:
        nonlocal changed
        if isinstance(node, str):
            if node == sens or node.strip() == sens:
                changed += 1
                return repl
            return node
        if isinstance(node, list):
            return [walk_replace(x) for x in node]
        if isinstance(node, dict):
            return {k: walk_replace(v) for k, v in node.items()}
        return node

    for it in tr:
        if not isinstance(it, dict) or not _is_prompt_like_block(it):
            continue
        flat = _user_text_from_transcript_item(it)
        if flat == sens or flat.strip() == sens:
            _set_prompt_like_value(it, repl)
            changed += 1
            continue
        val = it.get("value")
        if val is not None:
            new_val = walk_replace(val)
            if new_val is not val:
                it["value"] = new_val
    return changed


def _deep_scrub_sensitive_in_transcript(
    obj: Dict[str, Any], sensitive: str, replacement: str
) -> int:
    """user/user-injected 블록에서만 민감 문자열 잔존 분을 치환(context·ID 필드 보호)."""
    sens = (sensitive or "").strip()
    repl = str(replacement or "")
    if not sens or not repl:
        return 0
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return 0
    changed = 0

    def walk(node: Any) -> Any:
        nonlocal changed
        if isinstance(node, str):
            if sens in node:
                changed += 1
                if node.strip() == sens:
                    return repl
                return node.replace(sens, repl)
            return node
        if isinstance(node, list):
            return [walk(x) for x in node]
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        return node

    for it in tr:
        if not isinstance(it, dict) or not _is_prompt_like_block(it):
            continue
        val = it.get("value")
        if val is None:
            continue
        new_val = walk(val)
        if new_val is not val:
            it["value"] = new_val
    return changed


def _replace_agent_prebuilt_plain_text(obj: Dict[str, Any], replacement_plain: str) -> bool:
    """agent-prebuilt-prompt 블록만 치환(에이전트 생성 등)."""
    text = str(replacement_plain or "")
    if not text:
        return False
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return False
    for it in reversed(tr):
        if not isinstance(it, dict):
            continue
        if str(it.get("type") or "").lower() != "agent-prebuilt-prompt":
            continue
        _set_prompt_like_value(it, text)
        return True
    return False


def _replace_latest_user_plain_text(obj: Dict[str, Any], replacement_plain: str) -> bool:
    """마지막 실질 user 블록 치환(MASKING/BLOCK). user가 없으면 transcript 끝에 추가."""
    text = str(replacement_plain or "")
    if not text:
        return False
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        tr = []
        obj["transcript"] = tr
    target_idx = _find_latest_user_replace_index(tr)
    if target_idx is not None:
        _set_prompt_like_value(tr[target_idx], text)
        return True
    tr.append({"type": "user", "value": [[text]]})
    return True


def _rewrite_transcript_prompt(
    obj: Dict[str, Any], replacement_plain: str
) -> Tuple[bool, Optional[int], str, int]:
    sensitive = _extract_policy_prompt_from_obj(obj)
    user_sensitive = _extract_latest_user_prompt(obj)
    prebuilt_only = bool(_extract_agent_prebuilt_prompt(obj)) and not user_sensitive

    if prebuilt_only:
        ok = _replace_agent_prebuilt_plain_text(obj, replacement_plain)
        return ok, None, sensitive, 1 if ok else 0

    target_idx = _find_latest_user_replace_index(
        obj.get("transcript") if isinstance(obj.get("transcript"), list) else []
    )
    if not _replace_latest_user_plain_text(obj, replacement_plain):
        return False, target_idx, sensitive, 0
    extra = 0
    if user_sensitive:
        extra = _redact_sensitive_prompt_in_transcript(obj, user_sensitive, replacement_plain)
    return True, target_idx, sensitive, extra


def _rewrite_save_transactions_prompt(
    obj: Dict[str, Any], replacement_plain: str
) -> Tuple[bool, Optional[int], str, int]:
    """saveTransactionsFanout: thread_message에 저장되기 전 user step 치환."""
    sensitive = _extract_prompt_from_save_transactions(obj)
    if not sensitive:
        return False, None, "", 0
    changed = 0
    target_idx: Optional[int] = None
    idx = 0
    for _, step in _iter_save_transaction_user_steps(obj):
        flat = _user_text_from_transcript_item(step)
        if not _is_meaningful_user_prompt(flat) and flat.strip() != sensitive.strip():
            continue
        if flat.strip() == sensitive.strip() or flat == sensitive:
            _set_prompt_like_value(step, replacement_plain)
            changed += 1
            target_idx = idx
        idx += 1
    if changed <= 0:
        for _, step in _iter_save_transaction_user_steps(obj):
            flat = _user_text_from_transcript_item(step)
            if _is_meaningful_user_prompt(flat):
                _set_prompt_like_value(step, replacement_plain)
                changed += 1
                target_idx = 0
                break
    if changed <= 0:
        return False, target_idx, sensitive, 0
    extra = changed
    for _, step in _iter_save_transaction_user_steps(obj):
        flat = _user_text_from_transcript_item(step)
        if flat.strip() == sensitive.strip() and flat != replacement_plain:
            _set_prompt_like_value(step, replacement_plain)
            extra += 1
    return True, target_idx, sensitive, extra


def _rewrite_notion_request_body(
    req: Dict[str, Any], obj: Dict[str, Any], replacement_plain: str
) -> Tuple[bool, Optional[int], str, int]:
    kind = str(req.get("_notion_request_kind") or _infer_notion_request_kind(req))
    if kind == "save_transactions":
        return _rewrite_save_transactions_prompt(obj, replacement_plain)
    return _rewrite_transcript_prompt(obj, replacement_plain)


def _rewrite_notion_payload(
    req: Dict[str, Any],
    obj: Dict[str, Any],
    replacement_plain: str,
    original_payload: bytes,
    *,
    redact_context_pii: bool = True,
) -> Optional[Tuple[bytes, int]]:
    """Prompt 치환 + transcript attachment strip (gzip 재압축 포함)."""
    kind = str(req.get("_notion_request_kind") or _infer_notion_request_kind(req))
    had_attachments = _has_transcript_attachments(obj)
    changed = 0

    if replacement_plain:
        ok, _, _, extra = _rewrite_notion_request_body(req, obj, replacement_plain)
        if ok:
            changed += max(1, extra)
        elif not had_attachments:
            return None

    strip_cnt = strip_transcript_attachments(obj)
    if kind != "save_transactions" and redact_context_pii:
        if _redact_transcript_context_pii(obj):
            changed += 1

    if changed <= 0 and strip_cnt <= 0 and not had_attachments:
        return None
    if changed <= 0 and (strip_cnt > 0 or had_attachments):
        changed = 1

    encoded = _encode_request_json(req, obj, original_payload)
    return encoded, changed


_CONTEXT_PII_KEYS = ("userEmail", "userName", "userId", "spaceViewId")


def _redact_transcript_context_pii(obj: Dict[str, Any]) -> bool:
    """transcript 내 context 블록에서 계정·식별자 필드를 마스킹."""
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return False
    changed = False
    for it in tr:
        if not isinstance(it, dict):
            continue
        if str(it.get("type") or "").lower() != "context":
            continue
        val = it.get("value")
        if not isinstance(val, dict):
            continue
        for k in _CONTEXT_PII_KEYS:
            if k in val and val.get(k) not in (None, "", "[MASKED]"):
                val[k] = "[MASKED]"
                changed = True
    return changed


def _encode_request_json(
    req: Dict[str, Any], obj: Dict[str, Any], original_payload: bytes
) -> bytes:
    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _encode_payload_bytes(req, encoded, original_payload)


def _is_transcript_attachment_block(block: Any) -> bool:
    return isinstance(block, dict) and str(block.get("type") or "").lower() == "attachment"


def consent_attachment_state(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    """Transcript attachment blocks of the blocked turn. The S3 upload already ran."""
    obj = _parse_request_dict(req, payload)
    if not isinstance(obj, dict):
        return None
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return None
    blocks = [
        json.loads(json.dumps(block)) for block in tr if _is_transcript_attachment_block(block)
    ]
    if not blocks:
        return None
    return {"blocks": blocks}


def restore_consent_attachments(
    req: Dict[str, Any],
    payload: bytes,
    state: Any,
    prompt: str = "",
) -> Optional[bytes]:
    """Re-add the attachment blocks the rewrite stripped, ahead of the user block."""
    del prompt
    if not isinstance(state, dict):
        return None
    blocks = state.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return None
    obj = _parse_request_dict(req, payload)
    if not isinstance(obj, dict):
        return None
    tr = obj.get("transcript")
    if not isinstance(tr, list):
        return None
    kept = [block for block in tr if not _is_transcript_attachment_block(block)]
    restored = [json.loads(json.dumps(block)) for block in blocks]
    idx = _find_latest_user_replace_index(kept)
    if idx is None:
        kept.extend(restored)
    else:
        kept[idx:idx] = restored
    obj["transcript"] = kept
    return _encode_request_json(req, obj, payload)


def rewrite_prompt(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    policy_code: str,
) -> Optional[Tuple[bytes, str]]:
    obj = _parse_request_dict(req, payload)
    if obj is None or not replacement_text:
        return None
    tid = obj.get("threadId")
    if isinstance(tid, str) and tid.strip():
        _stash_notion_identity(tid.strip(), _best_context_block(obj))
    out = _rewrite_notion_payload(req, obj, replacement_text, payload)
    if out is None:
        return None
    encoded, _ = out
    code = (policy_code or "masking").strip().lower()
    return encoded, f"adapter:notion_policy_{code}"


def rewrite_prompt_masking(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    """MASKING 전용: transcript 프롬프트 치환 + attachment strip."""
    del source_prompt
    obj = _parse_request_dict(req, payload)
    if obj is None or not replacement_text:
        return None
    tid = obj.get("threadId")
    if isinstance(tid, str) and tid.strip():
        _stash_notion_identity(tid.strip(), _best_context_block(obj))
    out = _rewrite_notion_payload(req, obj, replacement_text, payload)
    if out is None:
        return None
    encoded, _ = out
    return encoded, "adapter:notion_policy_masking"


def rewrite_prompt_for_block(
    req: Dict[str, Any],
    payload: bytes,
    user_prompt: str,
    system_instruction: str,
) -> Optional[Tuple[bytes, str]]:
    obj = _parse_request_dict(req, payload)
    if obj is None:
        return None
    tid = obj.get("threadId") or _extract_thread_id_from_save_transactions(obj)
    if isinstance(tid, str) and tid.strip():
        _NOTION_BLOCK_RW_EMITTED.pop(tid.strip(), None)
    rid = str(req.get("request_id") or "")
    if rid:
        _NOTION_BLOCK_RW_EMITTED.pop(rid, None)
    sys_t = (system_instruction or "").strip()
    user_t = (user_prompt or "").strip()
    if sys_t and user_t:
        combined = f"{user_t}{_BLOCK_SYSINST_VERTICAL_GAP}{sys_t}"
    else:
        combined = user_t or sys_t
    if not combined:
        return None
    if isinstance(tid, str) and tid.strip():
        _stash_notion_identity(tid.strip(), _best_context_block(obj))
    out = _rewrite_notion_payload(req, obj, combined, payload)
    if out is None:
        return None
    encoded, _ = out
    return encoded, "adapter:notion_policy_block_sysinstruct"


def _rw_state_key(req: Dict[str, Any]) -> str:
    rid = str(req.get("request_id") or "")
    tid = str(req.get("_notion_thread_id") or _NOTION_THREAD_BY_RID.get(rid) or "")
    return tid or rid or "__notion__"


def _deep_block_agent_inferences(objs: List[Dict[str, Any]], replacement_text: str) -> int:
    """record-map 등 중첩된 agent-inference 텍스트: 가장 긴 블록에 차단 문구, 나머지 비움."""
    text_blocks: List[Dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            step = node.get("step")
            if isinstance(step, dict) and step.get("type") == "agent-inference":
                val = step.get("value")
                if isinstance(val, list):
                    for blk in val:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            text_blocks.append(blk)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    for o in objs:
        walk(o)
    if not text_blocks:
        return 0
    changed = 0
    text_blocks.sort(key=lambda b: len(str(b.get("content") or "")), reverse=True)
    for i, blk in enumerate(text_blocks):
        want = replacement_text if i == 0 else ""
        if str(blk.get("content") or "") != want:
            blk["content"] = want
            changed += 1
    return changed


def _mutate_ndjson_for_block_override(
    objs: List[Dict[str, Any]],
    replacement_text: str,
    state_key: str,
) -> int:
    changed = 0
    emitted = _NOTION_BLOCK_RW_EMITTED.get(state_key, False)

    for obj in objs:
        if not isinstance(obj, dict) or obj.get("type") != "patch":
            continue
        vlist = obj.get("v")
        if not isinstance(vlist, list):
            continue
        for op in vlist:
            if not isinstance(op, dict):
                continue
            kind = op.get("o")
            if kind == "a":
                inner = op.get("v")
                if not isinstance(inner, dict) or inner.get("type") != "agent-inference":
                    continue
                val = inner.get("value")
                if not isinstance(val, list) or not val:
                    inner["value"] = [{"type": "text", "content": ""}]
                    changed += 1
                else:
                    first = val[0]
                    if isinstance(first, dict) and first.get("type") == "text":
                        if not emitted:
                            if first.get("content") != replacement_text:
                                first["content"] = replacement_text
                                changed += 1
                            _NOTION_BLOCK_RW_EMITTED[state_key] = True
                            emitted = True
                        else:
                            if first.get("content") != "":
                                first["content"] = ""
                                changed += 1
                    else:
                        inner["value"] = [{"type": "text", "content": replacement_text if not emitted else ""}]
                        changed += 1
                        if not emitted:
                            _NOTION_BLOCK_RW_EMITTED[state_key] = True
                            emitted = True
            elif kind == "x":
                p = str(op.get("p") or "")
                if _CONTENT_PATCH_RE.search(p) or p.endswith("/content"):
                    if _NOTION_BLOCK_RW_EMITTED.get(state_key, False):
                        cur = op.get("v")
                        if cur != "":
                            op["v"] = ""
                            changed += 1
    return changed


def rewrite_response(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
) -> Optional[Tuple[bytes, str]]:
    if not replacement_text or not is_notion_inference_transcript(req):
        return None
    if (req.get("direction") or "").lower() != "response":
        return None
    decoded = _decode_payload_bytes(req, payload)
    text = _safe_utf8(decoded)
    objs, tail = _consume_json_values(text)
    if not objs:
        objs = list(_iter_json_documents(text))
    if not objs:
        return None
    state_key = _rw_state_key(req)
    c1 = _mutate_ndjson_for_block_override(objs, replacement_text, state_key)
    c2 = _deep_block_agent_inferences(objs, replacement_text)
    if c1 + c2 <= 0:
        return None
    rebuilt = "".join(
        json.dumps(o, ensure_ascii=False, separators=(",", ":")) for o in objs
    )
    if tail.strip():
        rebuilt += tail
    out = rebuilt.encode("utf-8")
    out = _encode_payload_bytes(req, out, payload)
    return out, "adapter:notion_policy_block_message"


def response_stream_done(req: Dict[str, Any], payload: bytes) -> bool:
    if not is_notion_inference_transcript(req):
        return False
    if (req.get("direction") or "").lower() != "response":
        return False
    raw = _safe_utf8(payload)
    objs, _ = _consume_json_values(raw)
    if not objs:
        objs = list(_iter_json_documents(raw))
    return _response_stream_done_hint(objs)


def collect_session_ids_for_masking_clear(
    req: Dict[str, Any], payload: bytes
) -> List[str]:
    if not is_notion_inference_transcript(req):
        return []
    rid = str(req.get("request_id") or "")
    out: List[str] = []
    tid = _NOTION_THREAD_BY_RID.get(rid)
    if isinstance(tid, str) and tid.strip():
        out.append(tid.strip())
    obj = _parse_request_dict(req, payload)
    if obj:
        x = obj.get("threadId")
        if isinstance(x, str) and x.strip():
            t = x.strip()
            if t not in out:
                out.append(t)
    return out


def _cli_main() -> None:
    ap = argparse.ArgumentParser(description="Notion runInferenceTranscript 요청/응답 추출")
    ap.add_argument("--request", "-r", help="요청 JSON 파일 경로")
    ap.add_argument("--response", "-s", help="응답 NDJSON 파일 경로(연속 JSON 또는 NDJSON)")
    args = ap.parse_args()
    if not args.request and not args.response:
        ap.print_help()
        return
    if args.request:
        with open(args.request, "r", encoding="utf-8") as f:
            prompt, ctx = extract_user_and_context_from_request_body(f.read())
        print("=== Prompt ===")
        print(prompt)
        print("=== Context (계정·워크스페이스) ===")
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
    if args.response:
        with open(args.response, "r", encoding="utf-8") as f:
            body = f.read()
        out = assistant_text_from_ndjson(body)
        print("=== Assistant response ===")
        print(out)


if __name__ == "__main__":
    _cli_main()
