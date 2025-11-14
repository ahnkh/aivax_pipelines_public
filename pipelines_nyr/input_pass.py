from typing import Optional, Dict, Any, List, Union
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import json
import logging
import base64
import ssl

# ----------------------------------------------------
# 로깅 설정
# ----------------------------------------------------
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# ----------------------------------------------------
# 유틸
# ----------------------------------------------------
def _ts_isoz() -> str:
    # UTC ISO 8601 + Z
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


# ----------------------------------------------------
# 파이프라인 
# ----------------------------------------------------
class Pipeline:
    def __init__(self):
        self.type = "filter"
        self.id = "input_filter"
        self.name = "input_filter"

        class Valves(BaseModel):
            pipelines: List[str] = Field(default_factory=lambda: ["*"])
            priority: int = 0
            enabled: bool = True

            # OpenSearch 저장 설정
            os_enabled: bool = True
            os_url: str = "https://vax-opensearch:9200"   # 예: http://os:9200
            os_index: str = "input_filter"  # 자동 생성 허용 시 그대로 사용
            os_user: Optional[str] = "admin"           # 예: "admin"
            os_pass: Optional[str] = "Sniper123!@#"           # 예: "admin"
            os_insecure: bool = True                # https 자가서명 시 True
            os_timeout: int = 3                  # 초

            # 필드 기본값
            default_channel: str = "web"
            default_user_role: Optional[str] = None

        self.Valves = Valves
        self.valves = Valves()

    # 프레임워크 훅
    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass

    async def on_valves_updated(self):
        pass

# ----------------------------
# OpenSearch 인덱싱 (아이템포턴트)
# ----------------------------
    def _index_opensearch(self, doc: Dict[str, Any], doc_id: Optional[str] = None) -> bool:
        v = self.valves
        if not v.os_enabled:
            return False

        base = f"{v.os_url.rstrip('/')}/{v.os_index}/_doc"
        url = f"{base}/{doc_id}?op_type=create" if doc_id else base
        payload = json.dumps(doc, ensure_ascii=False).encode("utf-8")

        # 1) requests 경로
        try:
            import requests
            auth = (v.os_user, v.os_pass) if v.os_user else None
            verify = not v.os_insecure
            method = requests.put if doc_id else requests.post
            r = method(url, data=payload, headers={"Content-Type": "application/json"},
                    auth=auth, verify=verify, timeout=v.os_timeout)
            if r.status_code in (200, 201, 409):  # 409 = 이미 저장됨(중복 방지 OK)
                if r.status_code == 409:
                    logger.info("[inlet->OS] idempotent skip (doc_id=%s)", doc_id)
                return True
            logger.warning("[inlet->OS] status=%s body=%s", r.status_code, r.text[:400])
            return False
        except Exception:
            logger.debug("[inlet->OS] requests path failed; fallback to urllib")

        # 2) urllib 폴백
        try:
            from urllib.request import Request, urlopen
            from urllib.error import HTTPError
            headers = {"Content-Type": "application/json"}
            if v.os_user:
                token = base64.b64encode(f"{v.os_user}:{v.os_pass or ''}".encode()).decode()
                headers["Authorization"] = f"Basic {token}"
            req = Request(url, data=payload, headers=headers, method=("PUT" if doc_id else "POST"))
            ctx = ssl._create_unverified_context() if url.startswith("https://") and v.os_insecure else None
            try:
                with urlopen(req, timeout=v.os_timeout, context=ctx) as resp:
                    status = getattr(resp, "status", 200)
                    if status in (200, 201):
                        return True
                    body = resp.read(512).decode("utf-8", "ignore")
                    logger.warning("[inlet->OS] urllib bad status=%s body=%s", status, body)
                    return False
            except HTTPError as he:
                if getattr(he, "code", None) == 409:
                    logger.info("[inlet->OS] idempotent skip (doc_id=%s)", doc_id)
                    return True
                raise
        except Exception as e:
            logger.warning("[inlet->OS] urllib path failed: %r", e)
            return False


    # ----------------------------
    # 메인: inlet
    # ----------------------------
    async def inlet(self, body: Dict[str, Any], user: Optional[dict] = None) -> Dict[str, Any]:
        if not self.valves.enabled:
            return body

        meta: Dict[str, Any] = body.get("metadata") or {}
        stage = meta.get("stage") or meta.get("hook") or body.get("stage")
        if stage and str(stage).lower() != "inlet":
            return body

        msgs: List[Dict[str, Any]] = body.get("messages") or []
        query_text = None
        last_role = None
        if isinstance(msgs, list) and msgs:
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "user":
                    query_text = m.get("content")
                    last_role = "user"
                    break

        # user 메시지가 없으면 저장 스킵 (assistant/tool/system만 있는 경우)
        if last_role != "user" or not query_text:
            return body

        # 메타에서 id, 세션, ip, 채널 등 추출(없으면 None/기본값)
        message_id = meta.get("message_id")
        session_id = meta.get("session_id")
        src_ip = (
            meta.get("client_ip")
            or meta.get("src_ip")
            or meta.get("ip")
            or _safe_get(meta, "request", "ip", default=None)
        )

        user_id = (user or {}).get("name") if isinstance(user, dict) else None
        user_email = (user or {}).get("email") if isinstance(user, dict) else None
        user_role = (user or {}).get("role") if isinstance(user, dict) else getattr(self.valves, "default_user_role", None)

        # 저장 문서
        os_doc = {
            "@timestamp": _ts_isoz(),
            "session": {"id": session_id},
            "user":    {"id": user_id, "role": user_role, "email": user_email},
            "src":     {"ip": src_ip},
            "query":   {"text": query_text},
        }
        print(os_doc)

        # (C) 아이템포턴트 저장: 동일 message_id는 한 번만
        try:
            doc_id = f"{message_id}:query" if message_id else None
            self._index_opensearch(os_doc, doc_id=doc_id)
        except Exception as e:
            logger.warning("[inlet->OS] index error: %r", e)

        return body
