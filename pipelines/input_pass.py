from typing import Optional, Dict, Any, List, Union
from pydantic import BaseModel, Field
from datetime import datetime, timezone
# import json
# import logging
# import base64
# import ssl

from lib_include import *

from type_hint import *

# from datetime import datetime

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
        
        user_id:str = ""
        user_role:str = getattr(self.valves, "default_user_role", None)
        user_email:str = ""
        ai_service_type:int = AI_SERVICE_DEFINE.SERVICE_UNDEFINE
        uuid:str = ""
        
        #__user__ 거슬린다.
        dictUserInfo:dict = __user__
        
        if None != dictUserInfo:
            
            user_id = dictUserInfo.get(ApiParameterDefine.NAME, "") #TODO: 이름이 현재는 없다.
            user_role = dictUserInfo.get(ApiParameterDefine.ROLE, "") #TODO: 2단계만 수집 가능
            user_email = dictUserInfo.get(ApiParameterDefine.EMAIL, "") #TODO: 2단계만 수집 가능
            
            ai_service_type:int = AI_SERVICE_DEFINE.SERVICE_UNDEFINE
            
        #ai service 명, TOOD: 이 기능이 Filter마다 반복, 공통화가 필요하다.
        strAIServiceName:str = AI_SERVICE_NAME_MAP.get(ai_service_type, "") #혹여 아예 엉뚱한 값이 들어오면, 공백으로 저장

        # user_id = (__user__ or {}).get("name") if isinstance(__user__, dict) else None
        # user_role = (__user__ or {}).get("role") if isinstance(__user__, dict) else getattr(self.valves, "default_user_role", None)
        # user_email = (__user__ or {}).get("email") if isinstance(__user__, dict) else None
        
        # 위험한 코드, 향후 다른 형태로 개발
        # client_ip = __request__.client.host
        client_ip = ""

        # 저장 문서        
        dictOpensearchDoc = {
            "@timestamp": ts_isoz(),
            "event":   {"id": message_id, "type": "query"},
            "request": {"id": message_id},
            "session": {"id": session_id},
            # "user":    {"id": user_id, "role": user_role, "email": user_email},
            "user": {"id": user_id, "role": user_role, "email": user_email, "uuid" : uuid},
            "src":     {"ip": client_ip}, 
            # "src":     {"ip": src_ip},
            "channel": channel,
            
            #25.12.02 ai 서비스 유형 추가
            "ai_service" : strAIServiceName,
            "query":   {"text": query_text},
        }

        # print(os_doc)

        # (C) 아이템포턴트 저장: 동일 message_id는 한 번만
        # try:
        #     doc_id = f"{message_id}:query" if message_id else None
        #     self._index_opensearch(os_doc, doc_id=doc_id)
        # except Exception as e:
        #     logger.warning("[inlet->OS] index error: %r", e)
        
        self.AddLogData(LOG_INDEX_DEFINE.KEY_INPUT_FILTER, dictOpensearchDoc)

        #불필요한 전달, 제거 2단계가 필요하면 그때 다시 설계
        # return body
        return ERR_OK

    ################################################# 지울 소스
    