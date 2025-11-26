from typing import Optional, Dict, Any, List, Union
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import json
import logging
import base64
import ssl

from lib_include import *

from type_hint import *

from datetime import datetime

'''
opensearch, 저장 pipeline
'''

class Pipeline(PipelineBase):
    
    def __init__(self):
        '''
        '''
        super().__init__()
        
        self.type = "filter"
        self.id = "input_filter"
        self.name = "input_filter"

        class Valves(BaseModel):
            pipelines: List[str] = Field(default_factory=lambda: ["*"])
            priority: int = 0
            enabled: bool = True

            # # OpenSearch 저장 설정
            # os_enabled: bool = True
            # os_url: str = "https://vax-opensearch:9200"   # 예: http://os:9200
            # os_index: str = "input_filter"  # 자동 생성 허용 시 그대로 사용
            # os_user: Optional[str] = "admin"           # 예: "admin"
            # os_pass: Optional[str] = "Sniper123!@#"           # 예: "admin"
            # os_insecure: bool = True                # https 자가서명 시 True
            # os_timeout: int = 3                  # 초

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
    # 메인: inlet
    # ----------------------------        
    async def inlet(self, body: Dict[str, Any], __user__: Optional[dict] = None, dictExtParameter:dict = None, dictOuputResponse:dict = None, __request__: Optional[Request] = None) : #-> Dict[str, Any]:
        '''
        TODO: 2단계 모델이 비활성화 되어, 입력 body의 전달도 불필요 하여 주석처리
        '''
        
        # 불필요 기능, 제거
        # if not self.valves.enabled:
        #     return body
        
        if dictOuputResponse is None:
            dictOuputResponse = {} 
        
        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW

        meta: Dict[str, Any] = body.get("metadata") or {}
        
        # openwebui가 아니면 불필요 기능, 제거
        # stage = meta.get("stage") or meta.get("hook") or body.get("stage")
        # if stage and str(stage).lower() != "inlet":
        #     # return body
        #     raise Exception(f"invalid stage, id = {self.id}, stage = {stage}")

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
            raise Exception(f"invalid role, not exist user role, id = {self.id}, query_text = {query_text}")
            # return body

        # 메타에서 id, 세션, ip, 채널 등 추출(없으면 None/기본값)
        message_id = meta.get("message_id")
        session_id = meta.get("session_id")
        
        src_ip = (
            meta.get("client_ip")
            or meta.get("src_ip")
            or meta.get("ip")
            or safe_get(meta, "request", "ip", default=None)
        )
        channel = meta.get("channel") or getattr(self.valves, "default_channel", "web")

        user_id = (__user__ or {}).get("name") if isinstance(__user__, dict) else None
        user_role = (__user__ or {}).get("role") if isinstance(__user__, dict) else getattr(self.valves, "default_user_role", None)
        user_email = (__user__ or {}).get("email") if isinstance(__user__, dict) else None
        
        # 위험한 코드, 향후 다른 형태로 개발
        # client_ip = __request__.client.host
        client_ip = ""

        # 저장 문서
        os_doc = {
            "@timestamp": ts_isoz(),
            "event":   {"id": message_id, "type": "query"},
            "request": {"id": message_id},
            "session": {"id": session_id},
            "user":    {"id": user_id, "role": user_role, "email": user_email},
            "src":     {"ip": client_ip}, 
            # "src":     {"ip": src_ip},
            "channel": channel,
            "query":   {"text": query_text},
        }

        # print(os_doc)

        # (C) 아이템포턴트 저장: 동일 message_id는 한 번만
        # try:
        #     doc_id = f"{message_id}:query" if message_id else None
        #     self._index_opensearch(os_doc, doc_id=doc_id)
        # except Exception as e:
        #     logger.warning("[inlet->OS] index error: %r", e)
        
        self.AddLogData(LOG_INDEX_DEFINE.KEY_INPUT_FILTER, os_doc)

        #불필요한 전달, 제거 2단계가 필요하면 그때 다시 설계
        # return body
        return ERR_OK

    ################################################# 지울 소스
    
    # ----------------------------
# OpenSearch 인덱싱 (아이템포턴트)
# ----------------------------
    # def _index_opensearch(self, doc: Dict[str, Any], doc_id: Optional[str] = None) -> bool:
    #     v = self.valves
    #     if not v.os_enabled:
    #         return False

    #     base = f"{v.os_url.rstrip('/')}/{v.os_index}/_doc"
    #     url = f"{base}/{doc_id}?op_type=create" if doc_id else base
    #     payload = json.dumps(doc, ensure_ascii=False).encode("utf-8")

    #     # 1) requests 경로
    #     try:
    #         import requests
    #         auth = (v.os_user, v.os_pass) if v.os_user else None
    #         verify = not v.os_insecure
    #         method = requests.put if doc_id else requests.post
    #         r = method(url, data=payload, headers={"Content-Type": "application/json"},
    #                 auth=auth, verify=verify, timeout=v.os_timeout)
    #         if r.status_code in (200, 201, 409):  # 409 = 이미 저장됨(중복 방지 OK)
    #             if r.status_code == 409:
    #                 LOG().info("[inlet->OS] idempotent skip (doc_id=%s)", doc_id)
    #             return True
    #         LOG().warning("[inlet->OS] status=%s body=%s", r.status_code, r.text[:400])
    #         return False
    #     except Exception:
    #         LOG().debug("[inlet->OS] requests path failed; fallback to urllib")

    #     # 2) urllib 폴백
    #     try:
    #         from urllib.request import Request, urlopen
    #         from urllib.error import HTTPError
    #         headers = {"Content-Type": "application/json"}
    #         if v.os_user:
    #             token = base64.b64encode(f"{v.os_user}:{v.os_pass or ''}".encode()).decode()
    #             headers["Authorization"] = f"Basic {token}"
    #         req = Request(url, data=payload, headers=headers, method=("PUT" if doc_id else "POST"))
    #         ctx = ssl._create_unverified_context() if url.startswith("https://") and v.os_insecure else None
    #         try:
    #             with urlopen(req, timeout=v.os_timeout, context=ctx) as resp:
    #                 status = getattr(resp, "status", 200)
    #                 if status in (200, 201):
    #                     return True
    #                 body = resp.read(512).decode("utf-8", "ignore")
    #                 LOG().warning("[inlet->OS] urllib bad status=%s body=%s", status, body)
    #                 return False
    #         except HTTPError as he:
    #             if getattr(he, "code", None) == 409:
    #                 LOG().info("[inlet->OS] idempotent skip (doc_id=%s)", doc_id)
    #                 return True
    #             raise
    #     except Exception as e:
    #         LOG().warning("[inlet->OS] urllib path failed: %r", e)
    #         return False
