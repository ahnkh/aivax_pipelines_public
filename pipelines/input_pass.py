import ipaddress
from typing import Optional, Dict, Any, List, Union
from pydantic import BaseModel, Field
from datetime import datetime, timezone

from lib_include import *

from type_hint import *

from block_filter_modules.etc_utils.filter_custom_utils import FilterCustomUtils


'''
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

            default_channel: str = "web"
            default_user_role: Optional[str] = None

        self.valves = Valves()
        
        self.__filterCustomUtil:FilterCustomUtils = FilterCustomUtils()

    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass

    async def on_valves_updated(self):
        pass

    async def inlet(self, body: Dict[str, Any], __user__: Optional[dict] = None, customFilterConfigItem : PipelineCustomFilterConfigItem = None, dictLogBuffer:dict = None, dictOuputResponse:dict = None, __request__: Optional[Request] = None) : #-> Dict[str, Any]:
        
        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW

        metadata:dict = body.get(ApiParameterDefine.META_DATA)

        msgs: List[Dict[str, Any]] = body.get(ApiParameterDefine.MESSAGES, [])
        query_text = None
        last_role = None
        
        if isinstance(msgs, list) and msgs:
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "user":
                    query_text = m.get("content")
                    last_role = "user"
                    break

        # user 메시지가 없으면 저장 스킵 (assistant/tool/system만 있는 경우)
        # if last_role != "user" or not query_text:
        #     raise Exception(f"invalid role, not exist user role, id = {self.id}, query_text = {query_text}")
        #     # return body

        # 메타에서 id, 세션, ip, 채널 등 추출(없으면 None/기본값)
        message_id:str = metadata.get(ApiParameterDefine.MESSAGE_ID)
        # session_id:str = metadata.get(ApiParameterDefine.SESSION_ID)
        
        #TODO: 불합리한 로직, 개선 필요.
        # src_ip = (
        #     meta.get("client_ip")
        #     or meta.get("src_ip")
        #     or meta.get("ip")
        #     or safe_get(meta, "request", "ip", default=None)
        # )
        #TODO: channel 정보 미수집, getattr은 불합리
        # channel = metadata.get("channel") or getattr(self.valves, "default_channel", "web")
        # channel:str = self.valves.default_channel
        
        # user_role:str = getattr(self.valves, "default_user_role", None)
        user_role:str = self.valves.default_user_role
        
        user_id:str = ""        
        user_email:str = ""
        ai_service_type:int = AI_SERVICE_DEFINE.SERVICE_UNDEFINE
        uuid:str = ""
        client_host:str = ""
        
        #__user__ 거슬린다.
        # dictUserInfo:dict = __user__
        
        # if None != dictUserInfo:
            
        #     user_id = dictUserInfo.get(ApiParameterDefine.NAME, "") #TODO: 이름이 현재는 없다.
        #     user_role = dictUserInfo.get(ApiParameterDefine.ROLE, "") #TODO: 2단계만 수집 가능
        #     user_email = dictUserInfo.get(ApiParameterDefine.EMAIL, "") #TODO: 2단계만 수집 가능
            
        #     client_host = dictUserInfo.get(ApiParameterDefine.CLIENT_HOST, "") #TODO: 2단계만 수집 가능
            
        #     ai_service_type = __user__.get(ApiParameterDefine.AI_SERVICE, AI_SERVICE_DEFINE.SERVICE_UNDEFINE)
        #     uuid = __user__.get(ApiParameterDefine.UUID, "")
            
        #ai service 명, TOOD: 이 기능이 Filter마다 반복, 공통화가 필요하다.
        # strAIServiceName:str = AI_SERVICE_NAME_MAP.get(ai_service_type, "") #혹여 아예 엉뚱한 값이 들어오면, 공백으로 저장

        # user_id = (__user__ or {}).get("name") if isinstance(__user__, dict) else None
        # user_role = (__user__ or {}).get("role") if isinstance(__user__, dict) else getattr(self.valves, "default_user_role", None)
        # user_email = (__user__ or {}).get("email") if isinstance(__user__, dict) else None
        
        # 위험한 코드, 향후 다른 형태로 개발
        # client_ip = __request__.client.host
        # client_ip = ""
        
        (user_id, user_email, ai_service_type, uuid, client_host) = self.__filterCustomUtil.GetUserData(__user__)
        
        nClientHostCIDR:int = 0
        
        if 0 < len(client_host):        
            nClientHostCIDR = int(ipaddress.ip_address(client_host.strip()))

        # 저장 문서  
        '''
        이전 버전        
        # dictOpensearchDoc = {
        #     "@timestamp": ts_isoz(),
        #     "event":   {"id": message_id, "type": "query"}, # TODO: 제거 필요
        #     "request": {"id": message_id},
        #     "session": {"id": session_id}, # TODO: 제거 필요
        #     # "user":    {"id": user_id, "role": user_role, "email": user_email},
        #     "user": {"id": user_id, "role": user_role, "email": user_email, "uuid" : uuid},
        #     "src":     {"ip": client_host}, 
        #     # "src":     {"ip": src_ip},
        #     "channel": channel, # TODO: 제거 필요
            
        #     #25.12.02 ai 서비스 유형 추가
        #     "ai_service" : AI_SERVICE_NAME_MAP.get(ai_service_type, ""),
        #     "query":   {"text": query_text}, #TODO: text보다 query로 저장 여부, opensearch에 문제가 되는지 확인 필요
        # }
        '''        
        dictOpensearchDoc = {
            "@timestamp": ts_isoz(),
            "message_id" : message_id,
            # "event":   {"id": message_id, "type": "query"}, # TODO: 제거 필요
            # "request": {"id": message_id},
            # "session": {"id": session_id}, # TODO: 제거 필요
            # "user":    {"id": user_id, "role": user_role, "email": user_email},
            "user": {"id": user_id, "role": user_role, "email": user_email, "uuid" : uuid},
            "client_host" : client_host,
            "client_host_cidr" : nClientHostCIDR,
            # "src":     {"ip": client_host}, 
            # "src":     {"ip": src_ip},
            # "channel": channel, # TODO: 제거 필요
            
            #25.12.02 ai 서비스 유형 추가
            "ai_service" : AI_SERVICE_NAME_MAP.get(ai_service_type, ""),
            # "query":   {"text": query_text}, #TODO: text보다 query로 저장 여부, opensearch에 문제가 되는지 확인 필요
            "prompt" : query_text,
            
            # timestamp와 별도로, 요청시간을 추가한다.
            "request_date" : datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        }
        
        # TODO: 사양 변경, 로그 병합 기능으로 구현한다.
        # 다만, 완전히 저장 전까지는 동일한 구조도 같이 저장한다.
        # self.AddLogData(LOG_INDEX_DEFINE.KEY_INPUT_FILTER, dictOpensearchDoc)
        
        # 항상 None이 아니어야 한다. + 불필요 필드는 향후 맞춘다.
        dictLogBuffer.update(dictOpensearchDoc)

        #불필요한 전달, 제거 2단계가 필요하면 그때 다시 설계
        # return body
        return ERR_OK

    ################################################# 지울 소스
    