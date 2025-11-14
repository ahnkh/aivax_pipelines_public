from typing import Optional, Dict, Any, List, Tuple
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import json
import logging
import base64
import ssl
import hashlib

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# ---------------------------
# 유틸
# ---------------------------
def _ts_isoz() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_get(d: Dict[str, Any], *keys, default=None):
    cur = d
    try:
        for k in keys:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k)
        return cur if cur is not None else default
    except Exception:
        return default


def _truncate_bytes(s: Optional[str], limit: int) -> Tuple[Optional[str], Optional[int]]:
    if s is None or limit is None or limit <= 0:
        return s, None
    b = s.encode("utf-8", errors="ignore")
    if len(b) <= limit:
        return s, None
    tb = b[:limit]
    # 바이트 → 문자열 복원
    cut = tb.decode("utf-8", errors="ignore")
    return cut, len(b)


def _hash_text(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


# ---------------------------
# 세션/채널 복구 헬퍼 (★추가)
# ---------------------------
def _fallback_session(user: Optional[dict], channel: Optional[str]) -> str:
    """유저/채널/날짜 기반 의사 세션ID 생성"""
    uid = (user or {}).get("id") or "anon"
    ch = channel or "web"
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    seed = f"{uid}:{ch}:{day}".encode("utf-8")
    return "ps-" + hashlib.sha1(seed).hexdigest()[:16]


def _get_session_id(body: Dict[str, Any], user: Optional[dict]) -> Tuple[str, bool]:
    """
    inlet에서 복제해둔 metadata.__sid를 최우선으로 사용.
    없으면 가능한 모든 후보에서 회수, 그래도 없으면 fallback 생성.
    반환값: (session_id, is_fallback)
    """
    meta: Dict[str, Any] = body.get("metadata") or {}

    # 1) inlet에서 보존한 세션 (권장 방식)
    sid = meta.get("__sid")
    if sid:
        return sid, False

    # 2) 일반적인 후보들
    for keys in [
        ("metadata", "session_id"),
        ("metadata", "conversation_id"),
        ("metadata", "thread_id"),
        ("metadata", "chat_id"),
        ("session", "id"),
        ("conversation", "id"),
        ("session_id",),
    ]:
        val = _safe_get(body, *keys, default=None)
        if val:
            return val, False

    # 3) _filters에 들어있을 가능성 
    filt_sid = _safe_get(body, "_filters", "inlet", "__sid", default=None) \
               or _safe_get(body, "_filters", "__sid", default=None)
    if filt_sid:
        return filt_sid, False

    # 4) 최후: fallback 생성
    ch = meta.get("channel") or body.get("channel")
    return _fallback_session(user, ch), True


def _get_channel(body: Dict[str, Any]) -> Optional[str]:
    meta: Dict[str, Any] = body.get("metadata") or {}
    return meta.get("channel") or body.get("channel") or "web"


# ---------------------------
# 파이프라인
# ---------------------------
class Pipeline:
    def __init__(self):
        self.type = "filter"
        self.id = "output_filter"
        self.name = "output_filter"

        class Valves(BaseModel):
            pipelines: List[str] = Field(default_factory=lambda: ["*"])
            priority: int = 0
            enabled: bool = True

            # OpenSearch 설정
            os_enabled: bool = True
            os_url: str = "https://vax-opensearch:9200"
            os_index: str = "output_filter"
            os_user: Optional[str] = "admin"
            os_pass: Optional[str] = "Sniper123!@#"
            os_insecure: bool = True
            os_timeout: int = 3

            # 저장 옵션
            store_response_text: bool = True          # 응답 전문 저장 여부
            response_max_bytes: int = 200_000         # 응답 텍스트 최대 바이트(UTF-8 기준)
            hash_only: bool = False                   # 전문 대신 해시만 저장
            include_filters_meta: bool = True         # body["_filters"] 저장
            include_usage: bool = True                # 토큰/지연 등 사용량 저장

        self.Valves = Valves
        self.valves = Valves()

    # 프레임워크 훅
    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass

    async def on_valves_updated(self):
        pass

    # ---------------------------
    # OpenSearch 인덱싱
    # ---------------------------
    def _index_opensearch(self, doc: Dict[str, Any]) -> bool:
        v = self.valves
        if not v.os_enabled:
            return False

        url = f"{v.os_url.rstrip('/')}/{v.os_index}/_doc"
        payload = json.dumps(doc, ensure_ascii=False).encode("utf-8")

        # 1) requests 우선
        try:
            import requests
            auth = (v.os_user, v.os_pass) if v.os_user else None
            verify = not v.os_insecure
            r = requests.post(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                auth=auth,
                verify=verify,
                timeout=v.os_timeout,
            )
            ok = r.status_code in (200, 201)
            if not ok:
                logger.warning("[response->OS] status=%s body=%s", r.status_code, r.text[:400])
            return ok
        except Exception as e:
            logger.debug("[response->OS] requests failed: %r -> fallback to urllib", e)

        # 2) urllib 폴백
        try:
            from urllib.request import Request, urlopen
            headers = {"Content-Type": "application/json"}
            if v.os_user:
                token = base64.b64encode(f"{v.os_user}:{v.os_pass or ''}".encode()).decode()
                headers["Authorization"] = f"Basic {token}"

            req = Request(url, data=payload, headers=headers, method="POST")
            ctx = None
            if url.startswith("https://") and v.os_insecure:
                ctx = ssl._create_unverified_context()

            with urlopen(req, timeout=v.os_timeout, context=ctx) as resp:
                status = getattr(resp, "status", 200)
                ok = status in (200, 201)
                if not ok:
                    body = resp.read(512).decode("utf-8", "ignore")
                    logger.warning("[response->OS] urllib bad status=%s body=%s", status, body)
                return ok
        except Exception as e:
            logger.warning("[response->OS] urllib failed: %r", e)
            return False

    # ---------------------------
    # 어시스턴트 텍스트 추출
    # ---------------------------
    def _extract_assistant_text(self, body: Dict[str, Any]) -> Optional[str]:
        # 1) messages[*].role in ("assistant", "model")
        msgs = body.get("messages")
        if isinstance(msgs, list) and msgs:
            for m in reversed(msgs):
                role = (m or {}).get("role")
                if role in ("assistant", "model"):
                    txt = (m or {}).get("content")
                    if isinstance(txt, str) and txt:
                        return txt

        # 2) choices[0].message.content (OpenAI 호환)
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            ch0 = choices[0] or {}
            msg = ch0.get("message") or {}
            if isinstance(msg, dict):
                txt = msg.get("content")
                if isinstance(txt, str) and txt:
                    return txt
            txt = ch0.get("text")
            if isinstance(txt, str) and txt:
                return txt

        # 3) 기타 관용 키
        for key in ("response", "output", "result", "assistant"):
            val = body.get(key)
            if isinstance(val, str) and val:
                return val
            if isinstance(val, dict):
                txt = val.get("content")
                if isinstance(txt, str) and txt:
                    return txt
        return None

    # ---------------------------
    # 저장 문서 구성
    # ---------------------------
    def _make_doc(self, body: Dict[str, Any], user: Optional[dict]) -> Dict[str, Any]:
        v = self.valves

        # 메타/기본 정보
        meta: Dict[str, Any] = body.get("metadata") or {}

        session_id, is_fallback = _get_session_id(body, user)

        # 채널 복구 (없으면 web)
        channel = _get_channel(body)

        user_id = (user or {}).get("name") if isinstance(user, dict) else None
        user_role = (user or {}).get("role") if isinstance(user, dict) else None

        # 모델/사용량/지연
        model_name = _safe_get(body, "model", default=None) or _safe_get(meta, "model", default=None)


        # 필터링 메타

        # 어시스턴트 응답 추출 및 저장 정책 적용
        resp_text = self._extract_assistant_text(body)
        if v.hash_only:
            resp_text_to_store = None
        elif v.store_response_text:
            resp_text_to_store, original_size_bytes = _truncate_bytes(resp_text, v.response_max_bytes)
        else:
            resp_text_to_store = None

        doc = {
            "@timestamp": _ts_isoz(),
            "response": {
                "text": resp_text_to_store,
                "model": model_name,
            },
            "session": {
                "id": session_id,
            },
            "user": {"id": user_id, "role": user_role},
        }
        print(doc)
        return doc

    # ---------------------------
    # outlet 훅
    # ---------------------------
    async def outlet(self, body: Dict[str, Any], user: Optional[dict] = None) -> Dict[str, Any]:
        if not self.valves.enabled:
            return body
        try:
            sid, _ = _get_session_id(body, user)
            meta = body.get("metadata") or {}
            if "__sid" not in meta:
                meta["__sid"] = sid
            body["metadata"] = meta

            doc = self._make_doc(body, user)
            ok = self._index_opensearch(doc)
            if not ok:
                logger.warning("[response_opensearch] OpenSearch index failed")
        except Exception as e:
            logger.exception("[response_opensearch] outlet error: %s", e)
        return body
