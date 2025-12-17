# filters/regex_and_entropy_secret_filter.py
# OpenWebUI Pipelines - Filter
# 목적: 유저 프롬프트(content)에서
#   1) 알려진 시크릿 패턴(AWS/Azure/Base64HighEntropy/BasicAuth/Cloudant/Discord/GitHub/JWT/Keyword/Mailchimp/PrivateKey/Slack/Stripe/Twilio)
#   2) 엔트로피 높은 토큰(완화 임계치)
# 를 탐지하여 해당 "토큰/값"만 [MASKING]으로 치환

import copy

from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.filter_pattern_manager import FilterPatternManager
from block_filter_modules.filter_pattern.helper.detect_secret_filter_pattern import DetectSecretFilterPattern

from block_filter_modules.etc_utils.filter_custom_utils import FilterCustomUtils
'''
2025.10.21 pipeline과 pipeliemainapp간 공유
mainapp와는 양방향 구조로 가져간다.
webapp는 mainapp를 통해서 pipeline에 접근한다.
'''

# MASK_DEFAULT = "[MASKING]"
MASK_DEFAULT = "[AIVAX MASKING]"

class Pipeline(PipelineBase):
    
    def __init__(self):
        
        '''
        '''
        
        super().__init__()
        
        self.type = "filter"
        self.id = "secret_filter"
        self.name = "secret_filter"
        
        self.valves = self.Valves()
        
        #TODO: 사용하지 않는 필드, 향후 제거
        self.toggle = True
        
        # 공용 helper
        self.__filterCustomUtil:FilterCustomUtils = FilterCustomUtils()
        pass
    
    class Valves(BaseModel):
        pipelines: List[str] = Field(default_factory=lambda: ["*"], description="적용 파이프라인('*'=전체)")
        priority: int = Field(default=0, description="필터 실행 우선순위(높을수록 먼저)")
        enabled: bool = Field(default=True, description="필터 ON/OFF")
        log_to_console: bool = Field(default=True, description="콘솔 로그 출력")
        mask_char: str = Field(default=MASK_DEFAULT, description="치환 문자열")
        # 엔트로피/길이 임계치(완화값: 너무 높다는 피드백 반영)
        min_len_b64: int = Field(default=20, description="Base64 모양 최소 길이(기본 20)")
        min_len_hex: int = Field(default=28, description="Hex 모양 최소 길이(기본 28)")
        min_len_mixed: int = Field(default=20, description="혼합 문자군 최소 길이(기본 20)")
        thr_b64: float = Field(default=4.0, description="Base64 모양 엔트로피 임계치(기본 4.0)")
        thr_hex: float = Field(default=3.0, description="Hex 모양 엔트로피 임계치(기본 3.0)")
        thr_mixed: float = Field(default=3.8, description="혼합 모양 엔트로피 임계치(기본 3.8)")
        # 프리픽스 완화(사내 토큰 접두 등)
        prefix_relax: bool = Field(default=True, description="특정 접두 토큰(ak-, tk-, ghp-/_) 완화 룰 적용")
        
        # 저장 옵션 => TODO: 미사용 옵션으로 보이며, 사용 출처 불분명
        store_response_text: bool = True          # 응답 전문 저장 여부
        response_max_bytes: int = 200_000         # 응답 텍스트 최대 바이트(UTF-8 기준)
        hash_only: bool = False                   # 전문 대신 해시만 저장
        include_filters_meta: bool = True         # body["_filters"] 저장
        include_usage: bool = True                # 토큰/지연 등 사용량 저장
        pass
        
    ########################################### public
    
    # ---------- 파이프라인 엔트리 ----------
    async def inlet(self, body: Dict[str, Any], __user__: Optional[dict] = None, dictExtParameter:dict = None, dictOuputResponse:dict = None, __request__: Optional[Request] = None) : #-> Dict[str, Any]:
        
        '''
        TODO: 기존 형상은 가급적 유지
        
        "body":
        {
            "metadata": {
                "session_id": "",
                "message_id": ""
            },
            
            "messages": [
                {"role":"user", "content":""}
            ],
            
            "attach_file": [
                "/home1/aivax/resource_data/attach_file/test.docs"
            ]
        }
        
        "user:
        {
            "name" : "khan",
            "email" : "ghahn@wins21.co.kr"
        }
        
        TODO: 예외처리는 raise 로 대체.
        
        TODO: 2단계 모델이 보류되어, body전달은 불필요한 자원 낭비, 제거
        '''
        
        #chat completion을 통해 호출시, 예외처리
        if None == dictOuputResponse:
            dictOuputResponse = {}  
        
        #기본적인 응답 처리, action필드를 기본값으로 설정, TODO: 공통화
        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
        
        #설명 문자열, 각 filter마다 추가. 크게 의미는 없다.
        # dictOuputResponse[ApiParameterDefine.OUT_DESRIPTION] = f"{self.name} filter 차단을 수행합니다."
        
        # 2단계 기능, 제거.
        # if not self.valves.enabled:
        #     LOG().info("action disabled")
            
        #     # raise Exception(f"action disabled, id = {self.id}")
        #     # raise HTTPException(
        #     #     status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        #     #     detail=f"action disabled, id = {self.id}")
            
        #     #body의 전달은, 사이드 이펙트가 우려되어 유지.
        #     return body

        # messages = body.get(ApiParameterDefine.MESSAGES) or []
        messages = body.get(ApiParameterDefine.MESSAGES)
        
        last:dict = messages[-1]
        content = last.get("content")

        #사용자 정보의 수집        
        user_id:str = ""
        user_email:str = ""
        ai_service_type:int = AI_SERVICE_DEFINE.SERVICE_UNDEFINE #없으면, 기본 GPT
        uuid:str = ""
        client_host:str = ""
        
        # dictUserInfo:dict = __user__
        
        # if None != dictUserInfo:
            
        #     user_id = dictUserInfo.get(ApiParameterDefine.NAME, "")
        #     user_email = dictUserInfo.get(ApiParameterDefine.EMAIL, "")
        #     ai_service_type = dictUserInfo.get(ApiParameterDefine.AI_SERVICE, AI_SERVICE_DEFINE.SERVICE_UNDEFINE)
            
        #     client_host = dictUserInfo.get(ApiParameterDefine.CLIENT_HOST, "") #TODO: 2단계만 수집 가능
            
        #     uuid = dictUserInfo.get(ApiParameterDefine.UUID, "")
            
        (user_id, user_email, ai_service_type, uuid, client_host) = self.__filterCustomUtil.GetUserData(__user__)
        
        #테스트용 로그 추가
        # LOG().debug(f"run detect secret inlet, prompt = {content}")
        
        #detect_secret, 다수 실행되는 현상, 마지막만 읽어들인다.
        # last:dict = messages[-1]
        # content = last.get("content")
        
        # TODO: 2단계 미사용, 불필요 기능, 2단계 사용시에도 더 적절하게 예외처리.
        # if not isinstance(messages, list):
        #     # LOG().error(f"invalid messages, {messages}")
        #     raise Exception(f"invalid messages format, id = {self.id}, message = {messages}")
        #     # raise HTTPException(
        #     #     status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        #     #     detail=f"invalid messages format, id = {self.id}, message = {messages}")
            
        #     # return body
        
        detectSecretFilterPattern:DetectSecretFilterPattern = self.GetFilterPatternModule(FilterPatternManager.PATTERN_FILTER_DETECT_SECRET)

        #우선 아래와 같이 수정한다. 기존 구조 유지
        messages = messages[-1:]

        #message, 다수 구조를 고려한다.=> 이렇게 되면 문제가 된다.
        for msg in messages:
            
            if msg.get("role") != "user":
                continue
            
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                # LOG().error(f"invalid content, {content}")
                
                raise Exception(f"invalid content format, id = {self.id}, content = {content}")                
                # continue
                
                
            #TODO: content, 재사용하면 안된다.
            strLocalContents:str = copy.deepcopy(content)
            
            masked:str = ""
            

            #TODO: detect span 기능, 통째로 이관
            # spans, counts = self.__detect_spans(content)
            
            #TODO: 구조 변경 필요, valve 클래스, 참조가 어려운 문제
            valves = self.valves
            (spans, counts, dictDetectedRule) = detectSecretFilterPattern.DetectPattern(strLocalContents, valves, user_id, uuid, ai_service_type)
                        
            #정책ID, 정책명을 차단 메시지에 추가 (너무 길다, 리펙토링 필요)
            strPolicyID:str = dictDetectedRule.get("id", "")
            strPolicyName:str = dictDetectedRule.get("name", "")
            strPolicyAction:str = dictDetectedRule.get(DBDefine.DB_FIELD_RULE_ACTION, "")
            strTarget:str = dictDetectedRule.get(DBDefine.DB_FIELD_RULE_TARGET, "") #카테고리, TODO: define 처리 필수
            
            # LOG().info(f"Masked: {counts}, len = {len(spans)}")
            
            # 이제는 span 과 action을 같이 본다.
            #action, block 과 masking 만 차단이고, 나머지는 아니다.
            
            if spans and (strPolicyAction in (PipelineFilterDefine.ACTION_BLOCK, PipelineFilterDefine.ACTION_MASKING)):
                
                #TODO: 우선 개발, counts의 필드에 따른 분기, 우선 수정후 2차 리펙토링시 개선한다.                
                # nAcceptCount = counts.get("accept")
                nBlockCount = counts.get("block")
                nMaskingCount = counts.get("masking")
                
                #정책 카테고리, name만 표기
                
                # strBlockMessage:str = self.__customBlockMessage(strPolicyName)                
                strBlockMessage:str = self.__filterCustomUtil.CustomBlockMessages(strPolicyName)
                
                #block 먼저 체크
                if 0 < nBlockCount:
                
                    # masked = self.__maskSpans(content, spans)
                    # msg["content"] = masked
                    
                    #TODO: action 값, 정수로 바꾼다. testRule은 문자를 유지한다. (백엔드 에서 사용이 우려)
                    dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
                    dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_BLOCK
                    
                    #TODO: maskinig 이든, block 이든 masking 처리 한다.
                    masked = self.__maskSpans(strLocalContents, spans)
                    
                    #TODO: 이건 변경하지 않도록 설정한다. (2단계 모델만 지원)
                    # msg["content"] = masked
                    dictOuputResponse[ApiParameterDefine.OUT_MASKED_CONTENTS] = masked
                    
                    #strBlockMessage:str = self.__customBlockMessage()
                    dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = strBlockMessage
                    
                elif 0 < nMaskingCount:
                    
                    dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_MASKING
                    dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_MASKING
                    
                    masked = self.__maskSpans(content, spans)
                    # msg["content"] = masked
                    dictOuputResponse[ApiParameterDefine.OUT_MASKED_CONTENTS] = masked

                    #TODO: 여기서부터는 협의 필요                    
                    dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = strBlockMessage
                    
                    
                #테스트, LLM으로 변조된 메시지를 보내는게 주요 기능이다.
                # ★ LLM에게 안내문을 '그대로 출력'하도록 지시
                # block_notice = "[AIVAX] 민감정보의 유출이 감지되어 차단되었습니다. 개인정보를 제외하고 다시 시도해주세요."
                # 2차 모델이 활성화 되는 시점에 주석 해제.
                # last = (body.get("messages") or [])[-1]
                # last["content"] = (
                #     "다음 문장을 사용자에게 그대로 출력하세요(추가 설명/수정/확장/사과문/이모지 금지):\n"
                #     f"{strBlockMessage}"
                # )
                
                
            else:
                # LOG().info("No secrets detected (regex+entropy).")
                # self.logger.info("No secrets detected (regex+entropy).")
                
                # dictOuputResponse["action"] = "allow"
                dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
                dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_ALLOW
                
                dictOuputResponse[ApiParameterDefine.OUT_MASKED_CONTENTS] = ""

                #TODO: 여기서부터는 협의 필요                    
                dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = ""
                
            # TODO: helper 생성 필요
            # 우선 테스트.
            strFinalAction = dictOuputResponse.get(ApiParameterDefine.OUT_ACTION)
            
            # meta = body.get("metadata") or {}
            metadata:dict = body.get(ApiParameterDefine.META_DATA)
                            
            # user_id = (__user__ or {}).get(ApiParameterDefine.NAME) if isinstance(__user__, dict) else None
            # user_email = (__user__ or {}).get(ApiParameterDefine.EMAIL) if isinstance(__user__, dict) else None            
            # ai_service_type = (__user__ or {}).get(ApiParameterDefine.AI_SERVICE) if isinstance(__user__, dict) else None
            
            message_id = metadata.get(ApiParameterDefine.MESSAGE_ID)
            session_id = metadata.get(ApiParameterDefine.SESSION_ID)
            
            #위험한 코드, 다른 형태로 향후 개발.
            # client_ip = __request__.client.host
            # client_ip = ""
            
            #ai service 명 추가
            # strAIServiceName:str = AI_SERVICE_NAME_MAP.get(ai_service_type, "")   

            #opensearch 저장 변수, TODO: 리펙토링 필요            
            dictOpensearchDocument:dict = {
                "@timestamp": ts_isoz(),
                
                "filter" : PipelineFilterDefine.FILTER_STAGE_REGEX,
                "filter_name": PipelineFilterDefine.FILTER_STAGE_REGEX,
                "content": strLocalContents,
                "message":msg,
                
                "request": {"id": message_id},
                "session": {"id": session_id},
                
                "user": {"id": user_id, "email": user_email, "uuid" : uuid},

                # "event":   {"id": msg_id, "type": "detect"},
                # "request": {"id": msg_id},
                # "session": {"id": sess_id},
                # "user":    {"id": user_id},
                
                # stage, regex로 통일
                # "stage":   "detect_secrets",
                "stage":   PipelineFilterDefine.FILTER_STAGE_REGEX,
                # "detection": detection_status,
                "should_block": (strFinalAction == "block"),
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
                "masked_contents" : masked
                
                # "final_action": fa_internal,
            }

            # self._index_opensearch(os_doc_final)
            self.AddLogData(LOG_INDEX_DEFINE.KEY_REGEX_FILTER, dictOpensearchDocument)

            '''
            #2025.11.15 2단계 모델에 반영되었으나, 3단계 모델에서는 ssl proxy로 전달되지 않아 주석 처리
            if std_action == "block":
                block_message = f"🚫 보안 정책에 의해 차단되었습니다. 메시지에 민감정보가 포함되어 있으니 해당 정보를 제거한 후 다시 시도해 주세요." 
                raise Exception(block_message)
            '''

        # 2단계 모델에서만 필요, 불필요, 제거
        # return body
        return ERR_OK
    
    #룰 테스트 메소드 추가
    async def testRule(self, strPrompt:str, strRule:str, strAction:str, dictOuputResponse:dict, request:Request):
        
        '''
        TODO: 우선 개발후, 2차 리펙토링 필수
        TODO: 사용자 정보는 현재는 사용하지 않는다. (향후 사용자에 대한 식별 정리후 사용)
        '''
        
        #TODO: 순환참조 우려로, 함수내 import (import 구문의 singleton 패턴 방식에 의지)
        from block_filter_modules.filter_pattern.filter_pattern_manager import FilterPatternManager
        from block_filter_modules.filter_pattern.helper.detect_secret_filter_pattern import DetectSecretFilterPattern
        detectSecretFilterPattern:DetectSecretFilterPattern = self.GetFilterPatternModule(FilterPatternManager.PATTERN_FILTER_DETECT_SECRET)
        
        (spans, counts, dictDetectedRule) = detectSecretFilterPattern.TestRulePattern(strPrompt, strRule, strAction)
        
        #TODO: 여기는 차단명이 없다. 테스트로 통일
        strPolicyName:str = "정책 테스트"
        
        #TODO: 반드시 다시 개발
        #사양변경, content 필드를 활용, masking일때는 masking 된 데이터를 보여주고
        #차단일때는 임의의 해당 문구를 추가
        #[AIVAX] 요청하신 문의 사항은 정책을 위반한 사항으로 차단 되었습니다.
        
        if spans:
                
            #TODO: 우선 개발, counts의 필드에 따른 분기, 우선 수정후 2차 리펙토링시 개선한다.                
            # nAcceptCount = counts.get("accept")
            nBlockCount = counts.get("block")
            nMaskingCount = counts.get("masking")
            
            #block 먼저 체크
            if 0 < nBlockCount:
            
                # masked = self.__maskSpans(content, spans)
                # msg["content"] = masked
                
                dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
                
                #TODO: maskinig 이든, block 이든 masking 처리 한다.
                masked = self.__maskSpans(strPrompt, spans)     
                
                strCustomContent:str = '''
                허용되지 않은 프롬프트가 포함되어 요청이 차단되었습니다. 
                '''
                           
                dictOuputResponse[ApiParameterDefine.OUT_CONTENT] = strCustomContent
                
                strBlockMessage:str = self.__customBlockMessage(strPolicyName)
                dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = strBlockMessage
                
            elif 0 < nMaskingCount:
                
                dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_MASKING
                
                masked = self.__maskSpans(strPrompt, spans)                
                dictOuputResponse[ApiParameterDefine.OUT_CONTENT] = masked

                #TODO: 여기서부터는 협의 필요                    
                strBlockMessage:str = self.__customBlockMessage(strPolicyName)
                dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = strBlockMessage
            
        else:
            LOG().info("No secrets detected (regex+entropy).")
            # self.logger.info("No secrets detected (regex+entropy).")
            
            # dictOuputResponse["action"] = "allow"
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
        
        return ERR_OK 

    async def outlet(self, body: Dict[str, Any], __event_emitter__=None, __user__: Optional[dict] = None) -> Dict[str, Any]:
        return body
    
    ############################################################ private
    
    #TODO: 이 함수는 detect secret으로 유지한다. 이름만 변경
    def __maskSpans(self, text: str, spans: List[Tuple[int, int]]) -> str:
        
        '''
        '''
        
        if not spans:
            return text
        
        spans = sorted(spans, key=lambda x: x[0])
        merged: List[Tuple[int, int]] = []
        
        for s, e in spans:
            if not merged or s > merged[-1][1]:
                merged.append((s, e))
            else:
                ps, pe = merged[-1]
                merged[-1] = (ps, max(pe, e))
        
        out = []
        last = 0
        
        mask = self.valves.mask_char or MASK_DEFAULT
        
        for s, e in merged:
            out.append(text[last:s])
            out.append(mask)
            last = e
            
        out.append(text[last:])
        
        return "".join(out)
    
    #차단 메시지, 우선 하드코딩, 향후 ouput 데이터의 처리 모듈을 개발한다.
    def __customBlockMessage(self, strBlockCategory:str) -> str:
        
        '''
        시연용 하드코딩
        '''
        
        strBlockMessage:str = f'''[AIVAX] 프롬프트 차단
AIVAX 정책에 의해 민감정보가 프롬프트에 포함된 것으로 탐지되었습니다.
❌탐지 유형은 '{strBlockCategory}' 입니다.
민감 정보를 전송할 경우, 기밀 정보 또는 개인 정보 유출등의 피해가 발생할 수 있으니 각별한 주의를 부탁드려요
요청하신 프롬프트는 AIVAX에 의해서 요청이 차단되었습니다.
세부 지침 사항은 관리자에게 문의해주세요
        '''
        
        return strBlockMessage
    
    