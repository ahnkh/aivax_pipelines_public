
import copy

from lib_include import *

from type_hint import *

from block_filter_modules.etc_utils.filter_custom_utils import FilterCustomUtils

'''
slm filter, 필요한 것 중심으로 다시 리펙토링
'''

class Pipeline(PipelineBase):
    
    def __init__(self):
        
        '''
        '''
        
        super().__init__()
        
        self.type = "filter"
        self.id = "slm_filter"
        self.name = "slm_filter"
        
        # 공용 helper
        self.__filterCustomUtil:FilterCustomUtils = FilterCustomUtils()
        
        pass
    
    async def inlet(self, body: Dict[str, Any], __user__: Optional[dict] = None, dictExtParameter:dict = None, dictOuputResponse:dict = None, __request__: Optional[Request] = None) : #-> Dict[str, Any]:
        '''
        '''
        
        # 응답 처리
        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
        
        # 패턴 관리, 별도 패턴이되, 스마트 엠투엠 서버로 요청, 응답을 받는다.
        
        #body, 구조는 동일하다.
        metadata:dict = body.get(ApiParameterDefine.META_DATA)
        
        message_id:str = metadata.get(ApiParameterDefine.MESSAGE_ID)
        session_id:str = metadata.get(ApiParameterDefine.SESSION_ID)
        
        #message 수집
        messages = body.get(ApiParameterDefine.MESSAGES)
        
        last:dict = messages[-1]
        content = last.get("content")
        
        strLocalContents:str = copy.deepcopy(content)
        
        #TODO: 약간의 중복코드, 일단 그대로 사용 (향후 tuple 정도로 정리)
        #사용자 정보의 수집        
        user_id:str = ""
        user_email:str = ""
        ai_service_type:int = AI_SERVICE_DEFINE.SERVICE_UNDEFINE #없으면, 기본 GPT
        uuid:str = ""
        client_host:str = ""
        (user_id, user_email, ai_service_type, uuid, client_host) = self.__filterCustomUtil.GetUserData(__user__)
        
        
        # 응답 데이터 처리
        # 탐지 결과 (block/allow), 원본 메시지를 추출한다. 향후 확장을 위한 응답 구조는 가져간다.
        dictSLMDetectResult:dict = {
            ApiParameterDefine.OUT_ACTION : PipelineFilterDefine.ACTION_ALLOW,
            ApiParameterDefine.OUT_CONTENT : ""
            } #버퍼 한개만 추가.
        
        # 탐지, 우선은 별도 모듈 대한 private 함수로, 개발후 분리 필요. 설계 미흡으로 향후 추가 개발 필요
        self.__detectPatternFromSLM(strLocalContents, dictSLMDetectResult)
        
        #반환값 할당, 중복이지만, 개별로 관리
        strSLMAction:str = dictSLMDetectResult.get(ApiParameterDefine.OUT_ACTION)
        strSLMContent:str = dictSLMDetectResult.get(ApiParameterDefine.OUT_CONTENT)
        
        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = strSLMAction
        
        # 데이터 생성
        # 정책 : slm의 action, 정책ID, 정책명, DB의 action값, 카테고리
        strAction:str = strSLMAction
        strMasked:str = ""
        
        #정책관련, 우선 빈값
        strPolicyID:str = ""
        strPolicyName:str = ""
        
        #block 또는 alllow => slm action과 동일하게.
        strPolicyAction:str = strSLMAction
        strTarget:str = ""
        
        # opensearch 저장
        #opensearch 저장 변수, TODO: 리펙토링 필요            
        dictOpensearchDocument:dict = {
            "@timestamp": ts_isoz(),
            
            "filter" : PipelineFilterDefine.FILTER_STAGE_REGEX,
            "filter_name": PipelineFilterDefine.FILTER_STAGE_REGEX,
            "content": strLocalContents,
            "message":{},
            
            "request": {"id": message_id},
            "session": {"id": session_id},
            
            "user": {"id": user_id, "email": user_email, "uuid" : uuid},

            # "event":   {"id": msg_id, "type": "detect"},
            # "request": {"id": msg_id},
            # "session": {"id": sess_id},
            # "user":    {"id": user_id},
            
            # stage, regex로 통일
            # "stage":   "detect_secrets",
            "stage":   PipelineFilterDefine.FILTER_STAGE_SLM,
            # "detection": detection_status,
            "should_block": (strAction == "block"),
            "mode": strPolicyAction, #DB상의 action으로 교체 (should_block과 값이 다르다.)
            
            #정책탐지시 정책 id, 이름 추가 (TODO: 25.12.02 정책 구조 변경에 따라 수정 필요, 진행중)
            "policy_id" : strPolicyID,
            "policy_name" : strPolicyName,
            "src":     {"ip": client_host},
            
            "pii": {
                # type: 정책명 추가
                "types": strTarget, # 카테고리
                # 잘못된 하드코딩, 제거
                # "samples": "reasons: API 키의 탐지, 기밀 정보, 민감정보, 세부 지침 사항, 이모지 금지",
                "confidence": 1.0
            },
            
            #25.12.02 ai 서비스 유형 추가                
            "ai_service" : AI_SERVICE_NAME_MAP.get(ai_service_type, ""),
            
            #masked contents 추가
            "masked_contents" : strMasked,
            
            #slm 원본 데이터 추가
            "slm_content" : strSLMContent
            
            # "final_action": fa_internal,
        }

        # self._index_opensearch(os_doc_final)
        self.AddLogData(LOG_INDEX_DEFINE.KEY_REGEX_FILTER, dictOpensearchDocument)
        
        return ERR_OK
    
    ###################################### private
    
    # slm을 통한 탐지, 우선 분리하지 않는다.
    def __detectPatternFromSLM(self, strContent:str, dictSLMDetectResult:dict):
        
        '''
        '''
        
        #TODO: 일단 하드코딩, 다시 만들어야 하며, 만드는 시점에 변경
        strURL:str = "http://127.0.0.1:1200/v1/chat/completions"
        timeout:int = 60 # 오래 걸릴수 있다. 우선 60초 timeout
        
        # 요청 패턴, 일단 개발, 향후 개선 (이정도로는 부족)
        post = {
            "model" : "cipherguard01",
            "messages" : [
                {
                    "role" : "user",
                    "content" : strContent    
                }                
            ],
            "temperature" : 0.0,
            "max_tokens" : 2048
        }
        
        header:dict = {
            "Content-Type" : "application/json"
        }
        
        resp = requests.post(strURL, json=post, timeout=timeout, headers=header)
        resp.raise_for_status()
        
        dictSLMHttpResponse:dict = resp.json()

        #응답 문자열 파싱, 결과 데이터 저장        
        self.__parseSLMReponse(dictSLMHttpResponse, dictSLMDetectResult)
        
        return ERR_OK
    
    #데이터 추출, 우선 여기 개발후 리펙토링
    def __parseSLMReponse(self, dictSLMHttpResponse:dict, dictSLMDetectResult:dict):
        
        '''
        데이터 오류, 또는 반환값이 없으면 allow
        choices/message/content 를 수집한다. 나머지는 아직 불필요
        content 안에 Safe 이면 allow, Unsafe 이면 block
        카테고리 정보는 우선 수집하지 않고, 별도 파싱도 하지 않는다.
        
        {
            "choices": [
                {
                "finish_reason": "stop",
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Safety: Safe\nCategories: None"
                }
                # 민감정보이면
                Safety: Unsafe\nCategories: PII
                }
            ],
            "created": 1765936456,
            "model": "cipher-guard-current.gguf",
            "system_fingerprint": "b7361-a81a56957",
            "object": "chat.completion",
            "usage": {
                "completion_tokens": 8,
                "prompt_tokens": 312,
                "total_tokens": 320
            },
            "id": "chatcmpl-85yqyzUyBhN4oqEd65wBCbxrT2XHGiqr",
            "timings": {
                "cache_n": 311,
                "prompt_n": 1,
                "prompt_ms": 109.252,
                "prompt_per_token_ms": 109.252,
                "prompt_per_second": 9.15315051440706,
                "predicted_n": 8,
                "predicted_ms": 782.808,
                "predicted_per_token_ms": 97.851,
                "predicted_per_second": 10.21961962575753
            }
            }
        '''
        
        # 기본 데이터 초기화 => 별도 응답 대신, 최종 응답에 추가
        # dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
        
        
        # 기본 예외처리, 응답이 없으면 allow, content는 공백
        if None == dictSLMHttpResponse or 0 == len(dictSLMHttpResponse):
            LOG().error("invalid slm response, skip, pass allow")
            return ERR_OK
        
        choices:list = dictSLMHttpResponse.get(PipelineFilterDefine.SLM_RESONSE_CHOICE, [])
        
        if 0 == len(choices):
            return ERR_OK
       
        #choice 안에, message 안에, contents
        dictChoice:dict = choices[0]
        
        message:dict = dictChoice.get(PipelineFilterDefine.SLM_RESONSE_MESSAGE)
        content:str = message.get(PipelineFilterDefine.SLM_RESONSE_CONTENT)
        
        dictSLMDetectResult[ApiParameterDefine.OUT_CONTENT] = content
        
        # 차단여부
        
        if PipelineFilterDefine.SLM_RESPONSE_UNSAFE in content:
            
            dictSLMDetectResult[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
            # pass
            
        # else: #차단이 아니면 일단 모두 safe
        
        return ERR_OK
    
    
    