
from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.filter_pattern_manager import FilterPatternManager
from block_filter_modules.filter_pattern.helper.file_block_filter_pattern import FileBlockFilterPattern

from block_filter_modules.etc_utils.filter_custom_utils import FilterCustomUtils

'''
file 분석 filter, pipeline 신규 추가
'''

class Pipeline(PipelineBase):
    
    def __init__(self):
        
        '''
        '''
        
        super().__init__()
        
        # TODO: 이 구조는 그대로 가져간다.
        self.type = "filter"
        self.id = "file_block_filter"
        self.name = "file_block_filter"
        
        #TODO: values는 필요하다고 판단되면 추가, 우선 추가하지 않는다.
        
        # 공용 helper
        self.__filterCustomUtil:FilterCustomUtils = FilterCustomUtils()        
        pass
    
    
    #pipeline, inlet, outlet 중 inlet 만 가져간다.
    async def inlet(self, body: Dict[str, Any], __user__: Optional[dict] = None, dictExtParameter:dict = None, dictOuputResponse:dict = None, __request__: Optional[Request] = None) : #-> Dict[str, Any]:
        
        '''
        TODO: 이 기능은 multiple filter 에서만 호출한다.
        TODO: prompt 대신, filer 정보가 넘어온다., 데이터는 body 에서 수집되어서 broad cast하도록 구성한다.
        '''
        
        # 응답처리
        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
        
        dictOuputResponse[ApiParameterDefine.FILE_SUMMARY] = []        
        
        fileBlockFilterPattern:FileBlockFilterPattern = self.GetFilterPatternModule(FilterPatternManager.PATTERN_FILTER_FILE_BLOCK)
        
        #file 분석, 정보 수집 + 정책 차단 여부
        #file은 다수일수 있다. 다만 pipeline의 regex filter에서는 제외하고, file filter에서만 제공한다.
        #TODO: list안의 dictionary로 변환되어 전달된다.
        attach_file:list = body.get(ApiParameterDefine.ATTACH_FILE)
        
        # metadata, session, message_id가 존재하며, 관련해서 추가정보를 수집한다.
        metadata:dict = body.get(ApiParameterDefine.META_DATA)
        
        message_id:str = metadata.get(ApiParameterDefine.MESSAGE_ID)
        session_id:str = metadata.get(ApiParameterDefine.SESSION_ID)
        
        #TODO: 정책상의 policy action등, 별도로 분리한다.
        #TODO: file은 여러개라는 가정으로, 각 파일 list별 정책이 들어간다.
        # dictSLMPolicyResult:dict = {
            
        #     DBDefine.DB_FIELD_RULE_ID : "",
        #     DBDefine.DB_FIELD_RULE_NAME : "",
        #     DBDefine.DB_FIELD_RULE_ACTION : "",
        #     DBDefine.DB_FIELD_RULE_TARGET : "",            
        # }
        
        fileBlockFilterPattern.DetectPattern(attach_file, dictOuputResponse)
        
        # 응답 데이터 처리, 우선 개발후 정리
        strAction:str = dictOuputResponse.get(ApiParameterDefine.OUT_ACTION)
        
        #TODO: opensearch 저장은 모델 분리.
        #TODO: 약간의 중복코드, 일단 그대로 사용 (향후 tuple 정도로 정리)
        #사용자 정보의 수집        
        user_id:str = ""
        user_email:str = ""
        ai_service_type:int = AI_SERVICE_DEFINE.SERVICE_UNDEFINE #없으면, 기본 GPT
        uuid:str = ""
        client_host:str = ""
        (user_id, user_email, ai_service_type, uuid, client_host) = self.__filterCustomUtil.GetUserData(__user__)
        
        # 응답 결과의 전달
        # 차단일때의 응답 정리, file 타입은 우선 차단 메시지를 만들지 않는다. (향후 공통화 + UI 설정 필요)
        if PipelineFilterDefine.ACTION_BLOCK == strAction:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_BLOCK
            
            # 불필요, 최종 메시지 생성 시점에 예외처리
            # dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = ""
            
        else:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_ALLOW
            # dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = ""
            
            
        # strPolicyID:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_ID, "")                    
        # strPolicyName:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_NAME, "")
        #TODO: 정책의 Action을 바라본다.
        # strPolicyAction:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_ACTION, "")
        # strPolicyTarget:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_TARGET, "")
        
        #opensearch 저장, file list 중심의 저장
        #TODO: 없는데이터라도 기존과 동일하게 공백으로.
        dictOpensearchDocument = {
            "@timestamp": ts_isoz(),
            "filter" : PipelineFilterDefine.FILTER_STAGE_FILE_BLOCK,
            "filter_name": PipelineFilterDefine.FILTER_STAGE_FILE_BLOCK,
            
            # "content": "", #TODOL 없는 데이터
            # "message":"",
            
            "request": {"id": message_id},
            "session": {"id": session_id},
            
            "user": {"id": user_id, "email": user_email, "uuid" : uuid},
            
            "stage": [PipelineFilterDefine.FILTER_STAGE_FILE_BLOCK],
            
            # 일단 이 값은 유지, input, output 점검 시점에 다시 정리
            "should_block": (strAction == "block"),
            
            # 최종 Action
            "mode": strAction,
            
            # TODO: file이 구조상 여러개이다. 각 파일별 정책이 들어간다.
            # #정책탐지시 정책 id, 이름 추가 (TODO: 25.12.02 정책 구조 변경에 따라 수정 필요, 진행중)
            # "policy_id" : strPolicyID,
            # "policy_name" : strPolicyName,
                
            "src":     {"ip": client_host},
            
            # "pii": {
            #     # type: 정책명 추가
            #     "types": strPolicyTarget, # 카테고리
            #     # 잘못된 하드코딩, 제거
            #     # "samples": "reasons: API 키의 탐지, 기밀 정보, 민감정보, 세부 지침 사항, 이모지 금지",
            #     "confidence": 1.0
            # },
            
            "ai_service" : AI_SERVICE_NAME_MAP.get(ai_service_type, ""),
            
            #regex pattern에 맞춰서.. => 각 파일별 정책, 파일 별로 추가한다.
            # "policy_id" : strPolicyID,
            # "policy_name" : strPolicyName,
            
        }
        
        #file 요약, 그대로 저장
        dictOpensearchDocument.update(dictOuputResponse)
        
        self.AddLogData(LOG_INDEX_DEFINE.KEY_REGEX_FILTER, dictOpensearchDocument)
        
        return ERR_OK
    
    