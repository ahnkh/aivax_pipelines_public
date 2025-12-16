from typing import Optional, Dict, Any, List, Tuple
from pydantic import BaseModel, Field
import hashlib

from lib_include import *

from type_hint import *

from block_filter_modules.etc_utils.filter_custom_utils import FilterCustomUtils

# ---------------------------
# 파이프라인
# ---------------------------
class Pipeline(PipelineBase):
    
    def __init__(self):
        
        '''
        '''
        super().__init__()
        
        self.type = "filter"
        self.id = "output_filter"
        self.name = "output_filter"

        class Valves(BaseModel):
            pipelines: List[str] = Field(default_factory=lambda: ["*"])
            priority: int = 0
            enabled: bool = True

            # 저장 옵션
            store_response_text: bool = True          # 응답 전문 저장 여부
            response_max_bytes: int = 200_000         # 응답 텍스트 최대 바이트(UTF-8 기준)
            hash_only: bool = False                   # 전문 대신 해시만 저장
            include_filters_meta: bool = True         # body["_filters"] 저장
            include_usage: bool = True                # 토큰/지연 등 사용량 저장

        # self.Valves = Valves
        self.valves = Valves()
        
        # 공용 helper
        self.__filterCustomUtil:FilterCustomUtils = FilterCustomUtils()
        pass

    # 프레임워크 훅
    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass

    async def on_valves_updated(self):
        pass

    # ---------------------------
    # outlet 훅
    # ---------------------------
    # async def outlet(self, body: Dict[str, Any], user: Optional[dict] = None) -> Dict[str, Any]:
    async def outlet(self, body: Dict[str, Any], user: Optional[dict] = None, dictOuputResponse:dict = None) : #-> Dict[str, Any]:
        
        '''
        '''
        
        # 기능 제거
        # if not self.valves.enabled:
        #     return body
        
        #TODO: 기능 식별이 안되어, 제거, session id는 api의 응답으로 가져온다.
        # sid, _ = _get_session_id(body, dictUser)
            
        # meta = body.get("metadata") or {}
        
        # if "__sid" not in meta:
        #     meta["__sid"] = sid
        
        # body["metadata"] = meta

        # doc = self.__makeOpensearchDocument(body, dictUser)
        #ok = self._index_opensearch(doc)
        
        v = self.valves

        # 메타/기본 정보
        # meta: Dict[str, Any] = body.get("metadata") or {}
        metadata: Dict[str, Any] = body.get(ApiParameterDefine.META_DATA, {})
        
        #TODO: 이값, 정리 필요
        # message_id_req = metadata.get(ApiParameterDefine.MESSAGE_ID) or safe_get(body, "request", "id", default=None)
        # response_id = metadata.get("response_id") or safe_get(body, "response", "id", default=None)
        
        message_id:str = metadata.get(ApiParameterDefine.MESSAGE_ID)
        session_id:str = metadata.get(ApiParameterDefine.SESSION_ID)
        
        response_id:str = metadata.get("response_id", safe_get(body, "response", "id", default=None)) 

        #sessionid, api에서 바로 가져온다.        
        # session_id, is_fallback = _get_session_id(body, user)
        # session_id, is_fallback = _get_session_id(body, user)
        is_fallback = False

        # 채널 복구 (없으면 web)
        # channel = _get_channel(body)
        channel = "web" #TODO: 없는 데이터, web으로 통일
        
        user_role:str = ""
        
        # TODO: 동일하게 사용
        user_id:str = ""
        ai_service_type:int = AI_SERVICE_DEFINE.SERVICE_UNDEFINE
        user_email:str = ""
        uuid:str = ""
        client_host:str = ""
        
        (user_id, user_email, ai_service_type, uuid, client_host) = self.__filterCustomUtil.GetUserData(user)
        
        # if None != user:
            
        #     user_id = user.get(ApiParameterDefine.NAME, "")
        #     user_role = user.get(ApiParameterDefine.ROLE, "")
        #     user_email = user.get(ApiParameterDefine.EMAIL, "")
        #     # pass

        # user_id = (user or {}).get("name") if isinstance(user, dict) else None
        # user_role = (user or {}).get("role") if isinstance(user, dict) else None
        # user_email = (user or {}).get("email") if isinstance(user, dict) else None

        # 모델/사용량/지연 => 이 값은 무시.
        model_name = safe_get(body, "model", default=None) or safe_get(metadata, "model", default=None)
        latency_ms = safe_get(body, "latency_ms", default=None) or safe_get(metadata, "latency_ms", default=None)
        
        usage = body.get("usage") or {}
        prompt_tokens = safe_get(usage, "prompt_tokens", default=None)
        completion_tokens = safe_get(usage, "completion_tokens", default=None)
        total_tokens = safe_get(usage, "total_tokens", default=None)

        # 필터링 메타
        filters_meta = body.get("_filters") if v.include_filters_meta else None

        # 어시스턴트 응답 추출 및 저장 정책 적용
        lstMessage:list = body.get(ApiParameterDefine.MESSAGES)
        
        strContent:str = ""
        
        #message, 처음 메시지.
        if 0 < len(lstMessage):            
            strContent = lstMessage[0].get("content")
    
        resp_text = strContent
        
        original_size_bytes = None
        
        resp_hash:str = ""
        resp_text_to_store:str = ""
        original_size_bytes:int = 0
        
        if v.hash_only:
            resp_hash = self.__hashText(resp_text)
            resp_text_to_store = None
            
        elif v.store_response_text:
            resp_text_to_store, original_size_bytes = self.__truncateBytes(resp_text, v.response_max_bytes)
            #TODO: hash는 옵션으로 저장, 설정
            # resp_hash = _hash_text(resp_text)
            
        else:
            resp_text_to_store = None
            # resp_hash = _hash_text(resp_text)

        doc = {
            "@timestamp": ts_isoz(),
            "event": {"id": response_id or message_id, "type": "response"},
            "request": {"id": message_id},
            
            "response": {
                "id": response_id,
                "text": resp_text_to_store,
                "text_truncated_bytes": original_size_bytes,
                "hash_sha256": resp_hash,
                "model": model_name,
                "latency_ms": latency_ms,
                "tokens": {
                    "prompt": prompt_tokens,
                    "completion": completion_tokens,
                    "total": total_tokens,
                } if v.include_usage else None,
            },
            "session": {
                "id": session_id,
                "present": not is_fallback,   
            },
            "user": {"id": user_id, "role": user_role, "email": user_email},
            "channel": channel,
            "filters": filters_meta,
        }
        
        #TODO: 좀더 개선후 추가
        self.AddLogData(LOG_INDEX_DEFINE.KEY_OUTPUT_FILTER, doc)
        
        # try:
            
            
        #     #if not ok:
        #     #    logger.warning("[response_opensearch] OpenSearch index failed")
        # except Exception as e:
        #     LOG().error(traceback.format_exc())
        # return body
        
        # 2단계가 필요한 시점에, body 전달 방식 재검토.
        return ERR_OK
    
    
    ############################################################## private
    
    def __truncateBytes(self, s: Optional[str], limit: int) -> Tuple[Optional[str], Optional[int]]:
        '''
        '''
        if s is None or limit is None or limit <= 0:
            return s, None
        
        b = s.encode("utf-8", errors="ignore")
        if len(b) <= limit:
            return s, None
        
        tb = b[:limit]
        # 바이트 → 문자열 복원
        cut = tb.decode("utf-8", errors="ignore")
        return cut, len(b)

    def __hashText(self, s: Optional[str]) -> Optional[str]:
        
        if not s:
            return None
        return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()
    
    
    # # ---------------------------
    # # 세션/채널 복구 헬퍼 (★추가)
    # # ---------------------------
    # def _fallback_session(user: Optional[dict], channel: Optional[str]) -> str:
    #     '''
    #     유저/채널/날짜 기반 의사 세션ID 생성
    #     '''
        
    #     uid = (user or {}).get("id") or "anon"
    #     ch = channel or "web"
        
    #     day = datetime.datetime.now(timezone.utc).strftime("%Y%m%d")
    #     seed = f"{uid}:{ch}:{day}".encode("utf-8")
    #     return "ps-" + hashlib.sha1(seed).hexdigest()[:16]

    # def _get_session_id(body: Dict[str, Any], user: Optional[dict]) -> Tuple[str, bool]:
    #     """
    #     inlet에서 복제해둔 metadata.__sid를 최우선으로 사용.
    #     없으면 가능한 모든 후보에서 회수, 그래도 없으면 fallback 생성.
    #     반환값: (session_id, is_fallback)
    #     """
    #     meta: Dict[str, Any] = body.get("metadata") or {}

    #     # 1) inlet에서 보존한 세션 (권장 방식)
    #     sid = meta.get("__sid")
    #     if sid:
    #         return sid, False

    #     # 2) 일반적인 후보들
    #     for keys in [
    #         ("metadata", "session_id"),
    #         ("metadata", "conversation_id"),
    #         ("metadata", "thread_id"),
    #         ("metadata", "chat_id"),
    #         ("session", "id"),
    #         ("conversation", "id"),
    #         ("session_id",),
    #     ]:
    #         val = safe_get(body, *keys, default=None)
    #         if val:
    #             return val, False

    #     # 3) _filters에 들어있을 가능성 
    #     filt_sid = safe_get(body, "_filters", "inlet", "__sid", default=None) \
    #                or safe_get(body, "_filters", "__sid", default=None)
    #     if filt_sid:
    #         return filt_sid, False

    #     # 4) 최후: fallback 생성
    #     ch = meta.get("channel") or body.get("channel")
    #     return _fallback_session(user, ch), True


    # def _get_channel(body: Dict[str, Any]) -> Optional[str]:
    #     meta: Dict[str, Any] = body.get("metadata") or {}
    #     return meta.get("channel") or body.get("channel") or "web"
    
    # # ---------------------------
    # # 저장 문서 구성
    # # ---------------------------
    # def __makeOpensearchDocument(self, body: Dict[str, Any], user: Optional[dict]) -> Dict[str, Any]:
        
    #     '''
    #     '''
    #     v = self.valves

    #     # 메타/기본 정보
    #     # meta: Dict[str, Any] = body.get("metadata") or {}
    #     metadata: Dict[str, Any] = body.get(ApiParameterDefine.META_DATA) or {}
        
    #     #TODO: 이값, 정리 필요
    #     # message_id_req = metadata.get(ApiParameterDefine.MESSAGE_ID) or safe_get(body, "request", "id", default=None)
    #     # response_id = metadata.get("response_id") or safe_get(body, "response", "id", default=None)
        
    #     message_id:str = metadata.get(ApiParameterDefine.MESSAGE_ID)
    #     session_id:str = metadata.get(ApiParameterDefine.SESSION_ID)
        
    #     response_id = metadata.get("response_id") or safe_get(body, "response", "id", default=None)

    #     #sessionid, api에서 바로 가져온다.        
    #     # session_id, is_fallback = _get_session_id(body, user)
    #     # session_id, is_fallback = _get_session_id(body, user)
    #     is_fallback = False

    #     # 채널 복구 (없으면 web)
    #     # channel = _get_channel(body)
    #     channel = "web" #TODO: 없는 데이터, web으로 통일
        
    #     user_role:str = ""
        
    #     # TODO: 동일하게 사용
    #     user_id:str = ""
    #     ai_service_type:int = AI_SERVICE_DEFINE.SERVICE_UNDEFINE
    #     user_email:str = ""
    #     uuid:str = ""
    #     client_host:str = ""
        
    #     (user_id, user_email, ai_service_type, uuid, client_host) = self.__filterCustomUtil.GetUserData(user)
        
    #     # if None != user:
            
    #     #     user_id = user.get(ApiParameterDefine.NAME, "")
    #     #     user_role = user.get(ApiParameterDefine.ROLE, "")
    #     #     user_email = user.get(ApiParameterDefine.EMAIL, "")
    #     #     # pass

    #     # user_id = (user or {}).get("name") if isinstance(user, dict) else None
    #     # user_role = (user or {}).get("role") if isinstance(user, dict) else None
    #     # user_email = (user or {}).get("email") if isinstance(user, dict) else None

    #     # 모델/사용량/지연 => 이 값은 무시.
    #     model_name = safe_get(body, "model", default=None) or safe_get(metadata, "model", default=None)
    #     latency_ms = safe_get(body, "latency_ms", default=None) or safe_get(metadata, "latency_ms", default=None)
        
    #     usage = body.get("usage") or {}
    #     prompt_tokens = safe_get(usage, "prompt_tokens", default=None)
    #     completion_tokens = safe_get(usage, "completion_tokens", default=None)
    #     total_tokens = safe_get(usage, "total_tokens", default=None)

    #     # 필터링 메타
    #     filters_meta = body.get("_filters") if v.include_filters_meta else None

    #     # 어시스턴트 응답 추출 및 저장 정책 적용
    #     resp_text = self._extract_assistant_text(body)
        
    #     original_size_bytes = None
        
    #     resp_hash:str = ""
    #     resp_text_to_store:str = ""
    #     original_size_bytes:int = 0
        
    #     if v.hash_only:
    #         resp_hash = _hash_text(resp_text)
    #         resp_text_to_store = None
            
    #     elif v.store_response_text:
    #         resp_text_to_store, original_size_bytes = _truncate_bytes(resp_text, v.response_max_bytes)
    #         #TODO: hash는 옵션으로 저장, 설정
    #         # resp_hash = _hash_text(resp_text)
            
    #     else:
    #         resp_text_to_store = None
    #         # resp_hash = _hash_text(resp_text)

    #     doc = {
    #         "@timestamp": ts_isoz(),
    #         "event": {"id": response_id or message_id, "type": "response"},
    #         "request": {"id": message_id},
            
    #         "response": {
    #             "id": response_id,
    #             "text": resp_text_to_store,
    #             "text_truncated_bytes": original_size_bytes,
    #             "hash_sha256": resp_hash,
    #             "model": model_name,
    #             "latency_ms": latency_ms,
    #             "tokens": {
    #                 "prompt": prompt_tokens,
    #                 "completion": completion_tokens,
    #                 "total": total_tokens,
    #             } if v.include_usage else None,
    #         },
    #         "session": {
    #             "id": session_id,
    #             "present": not is_fallback,   
    #         },
    #         "user": {"id": user_id, "role": user_role, "email": user_email},
    #         "channel": channel,
    #         "filters": filters_meta,
    #     }
    #     return doc
    
    ########################################################### 지울 코드
    
    # # ---------------------------
    # # OpenSearch 인덱싱
    # # ---------------------------
    # def _index_opensearch(self, doc: Dict[str, Any]) -> bool:
    #     v = self.valves
    #     if not v.os_enabled:
    #         return False

    #     url = f"{v.os_url.rstrip('/')}/{v.os_index}/_doc"
    #     payload = json.dumps(doc, ensure_ascii=False).encode("utf-8")

    #     # 1) requests 우선
    #     try:
    #         import requests
    #         auth = (v.os_user, v.os_pass) if v.os_user else None
    #         verify = not v.os_insecure
    #         r = requests.post(
    #             url,
    #             data=payload,
    #             headers={"Content-Type": "application/json"},
    #             auth=auth,
    #             verify=verify,
    #             timeout=v.os_timeout,
    #         )
    #         ok = r.status_code in (200, 201)
    #         if not ok:
    #             logger.warning("[response->OS] status=%s body=%s", r.status_code, r.text[:400])
    #         return ok
    #     except Exception as e:
    #         logger.debug("[response->OS] requests failed: %r -> fallback to urllib", e)

    #     # 2) urllib 폴백
    #     try:
    #         from urllib.request import Request, urlopen
    #         headers = {"Content-Type": "application/json"}
    #         if v.os_user:
    #             token = base64.b64encode(f"{v.os_user}:{v.os_pass or ''}".encode()).decode()
    #             headers["Authorization"] = f"Basic {token}"

    #         req = Request(url, data=payload, headers=headers, method="POST")
    #         ctx = None
    #         if url.startswith("https://") and v.os_insecure:
    #             ctx = ssl._create_unverified_context()

    #         with urlopen(req, timeout=v.os_timeout, context=ctx) as resp:
    #             status = getattr(resp, "status", 200)
    #             ok = status in (200, 201)
    #             if not ok:
    #                 body = resp.read(512).decode("utf-8", "ignore")
    #                 logger.warning("[response->OS] urllib bad status=%s body=%s", status, body)
    #             return ok
    #     except Exception as e:
    #         logger.warning("[response->OS] urllib failed: %r", e)
    #         return False
    
    
    # ---------------------------
    # 어시스턴트 텍스트 추출
    # ---------------------------
    # def _extract_assistant_text(self, body: Dict[str, Any]) -> Optional[str]:
    #     # 1) messages[*].role in ("assistant", "model")
        
    #     # msgs = body.get("messages")
    #     lstMessage:list = body.get(ApiParameterDefine.MESSAGES)
        
    #     strContent:str = ""
        
    #     #message, 처음 메시지.
    #     if 0 < len(lstMessage):
            
    #         strContent = lstMessage[0].get("content")
            
    #     return strContent
        
        
        #TODO: 단순화, 들어가지 않는 데이터는 버린다.
        # if isinstance(msgs, list) and msgs:
            
        #     for m in reversed(msgs):
        #         role = (m or {}).get("role")
                
        #         if role in ("assistant", "model"):
        #             txt = (m or {}).get("content")
        #             if isinstance(txt, str) and txt:
        #                 return txt
        
        # # 2) choices[0].message.content (OpenAI 호환)
        # choices = body.get("choices")
        # if isinstance(choices, list) and choices:
        #     ch0 = choices[0] or {}
        #     msg = ch0.get("message") or {}
        #     if isinstance(msg, dict):
        #         txt = msg.get("content")
        #         if isinstance(txt, str) and txt:
        #             return txt
        #     txt = ch0.get("text")
        #     if isinstance(txt, str) and txt:
        #         return txt

        # # 3) 기타 관용 키
        # for key in ("response", "output", "result", "assistant"):
        #     val = body.get(key)
        #     if isinstance(val, str) and val:
        #         return val
        #     if isinstance(val, dict):
        #         txt = val.get("content")
        #         if isinstance(txt, str) and txt:
        #             return txt
        # return None
