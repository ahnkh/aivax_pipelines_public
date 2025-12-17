
import copy

from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.filter_pattern_manager import FilterPatternManager
from block_filter_modules.filter_pattern.helper.slm_filter_pattern import SLMFilterPattern

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
        dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_ALLOW
        
        # 패턴 관리, 별도 패턴이되, 스마트 엠투엠 서버로 요청, 응답을 받는다.
        
        #body, 구조는 동일하다.
        metadata:dict = body.get(ApiParameterDefine.META_DATA)
        
        dictUser:dict = __user__
        
        #message 수집
        messages = body.get(ApiParameterDefine.MESSAGES)
        strLocalContents:str = self.__gatherContents(messages)
        
        # 여기서 탐지
        # 탐지 결과 (block/allow), 원본 메시지를 추출한다. 향후 확장을 위한 응답 구조는 가져간다.
        dictSLMDetectResult:dict = {
            #TODO: action 값, DB와 중복이다..
            ApiParameterDefine.OUT_ACTION : PipelineFilterDefine.ACTION_ACCEPT,
            ApiParameterDefine.OUT_SLM_CONTENT : ""
            } #버퍼 한개만 추가.
        
        #TODO: 정책과 결과는 분리해야 한다.
        dictSLMPolicyResult:dict = {
            
            DBDefine.DB_FIELD_RULE_ID : "",
            DBDefine.DB_FIELD_RULE_NAME : "",
            DBDefine.DB_FIELD_RULE_ACTION : "",
            DBDefine.DB_FIELD_RULE_TARGET : "",
            
        }
        
        # 탐지, 우선은 별도 모듈 대한 private 함수로, 개발후 분리 필요. 설계 미흡으로 향후 추가 개발 필요
        slmFilterPattern:SLMFilterPattern = self.GetFilterPatternModule(FilterPatternManager.PATTERN_FILTER_SLM)
        
        slmFilterPattern.DetectPattern(strLocalContents, dictSLMDetectResult, dictSLMPolicyResult)
        
        #반환값 할당, 중복이지만, 개별로 관리 TODO: 같이 사용하면 안되는데.. 일단 accept => allow로 변환.
        #Block, DB의 정책을 사용한다.
        strSLMAction:str = dictSLMPolicyResult.get(ApiParameterDefine.OUT_ACTION)
        # strSLMContent:str = dictSLMDetectResult.get(ApiParameterDefine.OUT_CONTENT)
        
        #TODO: 정책 데이터를 받아온다.
        # strPolicyID:str = dictSLMDetectResult.get(DBDefine.DB_FIELD_RULE_ID, "")
        strPolicyName:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_NAME, "")
        # strPolicyAction:str = dictSLMDetectResult.get(DBDefine.DB_FIELD_RULE_ACTION, "")
        # strPolicyTarget:str = dictSLMDetectResult.get(DBDefine.DB_FIELD_RULE_TARGET, "")
        
        # dictOuputResponse[ApiParameterDefine.OUT_ACTION] = strSLMAction
        
        # 응답 데이터 처리
        self.__updateApiOutResponse(strSLMAction, strPolicyName, dictOuputResponse)
        
        # 로그의 저장
        self.__addLogData(dictSLMDetectResult, dictSLMPolicyResult, metadata, dictUser, strLocalContents)
        
        return ERR_OK
    
    ###################################### private
    
    # contents의 수집, 모듈 분리, 예외처리, 성능은 일부 포기
    def __gatherContents(self, lstMessage:str) -> str:
        
        '''
        '''
        
        if None == lstMessage or 0 == len(lstMessage):
            return ""
        
        # 마지막 메시지.
        last:dict = lstMessage[-1]
        
        if None != last:
            
            content = last.get("content", "")
        
            strLocalContents:str = copy.deepcopy(content)
            return strLocalContents
        
        return ""
    
    # opensearch로의 저장, 분리해본다.
    def __addLogData(self, dictSLMDetectResult:dict, dictSLMPolicyResult:dict, dictMetaData:dict, dictUser:dict, strContents:str):
        
        '''
        '''
        
        #반환값 할당, 중복이지만, 개별로 관리
        strSLMAction:str = dictSLMDetectResult.get(ApiParameterDefine.OUT_ACTION)
        strSLMContent:str = dictSLMDetectResult.get(ApiParameterDefine.OUT_SLM_CONTENT)
        
        #TODO: 정책 데이터를 받아온다.
        strPolicyID:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_ID, "")
        strPolicyName:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_NAME, "")
        
        #TODO: 정책의 Action을 바라본다.
        strPolicyAction:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_ACTION, "")
        strPolicyTarget:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_TARGET, "")
        
        # 데이터 생성
        # 정책 : slm의 action, 정책ID, 정책명, DB의 action값, 카테고리
        strAction:str = strPolicyAction
        strMasked:str = "" # 현재 수집이 안되는 값, 공백 처리.
        
        # 로그의 저장
        #TODO: 약간의 중복코드, 일단 그대로 사용 (향후 tuple 정도로 정리)
        #사용자 정보의 수집        
        user_id:str = ""
        user_email:str = ""
        ai_service_type:int = AI_SERVICE_DEFINE.SERVICE_UNDEFINE #없으면, 기본 GPT
        uuid:str = ""
        client_host:str = ""
        (user_id, user_email, ai_service_type, uuid, client_host) = self.__filterCustomUtil.GetUserData(dictUser)
        
        message_id:str = dictMetaData.get(ApiParameterDefine.MESSAGE_ID)
        session_id:str = dictMetaData.get(ApiParameterDefine.SESSION_ID)
        
        # opensearch 저장
        #opensearch 저장 변수, TODO: 리펙토링 필요            
        dictOpensearchDocument:dict = {
            "@timestamp": ts_isoz(),
            
            "filter" : PipelineFilterDefine.FILTER_STAGE_SLM,
            "filter_name": PipelineFilterDefine.FILTER_STAGE_SLM,
            "content": strContents,
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
            "stage":   [PipelineFilterDefine.FILTER_STAGE_SLM],
            # "detection": detection_status,
            "should_block": (strAction == "block"),
            "mode": strPolicyAction, #DB상의 action으로 교체 (should_block과 값이 다르다.)
            
            #정책탐지시 정책 id, 이름 추가 (TODO: 25.12.02 정책 구조 변경에 따라 수정 필요, 진행중)
            "policy_id" : strPolicyID,
            "policy_name" : strPolicyName,
            "src": {"ip": client_host},
            
            "pii": {
                # type: 정책명 추가
                "types": strPolicyTarget, # 카테고리
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
    
    # API 응답 결과 업데이트
    def __updateApiOutResponse(self, strSLMAction, strPolicyName, dictOuputResponse:dict):
        
        '''
        block, allow에 따른 분기
        차단 메시지, 다른 패턴과 동일하게 분기
        '''
        
        # strSLMAction:str = dictSLMDetectResult.get(ApiParameterDefine.OUT_ACTION)
        
        #한번더, 명확하게.
        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
        dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_ALLOW
        
        #accept로 점진적으로 통일.
        if PipelineFilterDefine.ACTION_BLOCK == strSLMAction:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
            dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_BLOCK
            
            #정책 카테고리, name만 표기                
            strBlockMessage:str = self.__filterCustomUtil.CustomBlockMessages(strPolicyName)
            
            #message
            dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = strBlockMessage
            #pass
            
        elif PipelineFilterDefine.ACTION_MASKING == strSLMAction:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_MASKING
            dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_MASKING
            
            # 모호하여 하드코딩
            dictOuputResponse[ApiParameterDefine.OUT_MASKED_CONTENTS] = f'''[AIVAX] 프롬프트 마스킹
AIVAX 정책에 의해 민감정보가 프롬프트에 포함된 것으로 탐지되었습니다.
❌탐지 유형은 '{strPolicyName}' 입니다.
세부 지침 사항은 관리자에게 문의해주세요
        '''
        
        return ERR_OK
    
    
    
    
    
    