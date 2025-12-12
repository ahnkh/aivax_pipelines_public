

from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.filter_pattern_manager import FilterPatternManager
from block_filter_modules.filter_pattern.helper.file_block_filter_pattern import FileBlockFilterPattern

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
        
        # TODO: 접속정보, 그대로 가져간다. 공통 코드는 공통화.
        
        fileBlockFilterPattern:FileBlockFilterPattern = self.GetFilterPatternModule(FilterPatternManager.PATTERN_FILTER_FILE_BLOCK)
        
        #file 분석, 정보 수집 + 정책 차단 여부
        #file은 다수일수 있다. 다만 pipeline의 regex filter에서는 제외하고, file filter에서만 제공한다.
        #TODO: opensearch의 filter 분리는 향후 고민.
        attach_file:list = body.get(ApiParameterDefine.ATTACH_FILE)
        
        # metadata, session, message_id가 존재하며, 관련해서 추가정보를 수집한다.
        metadata:dict = body.get(ApiParameterDefine.META_DATA)
        
        message_id:str = metadata.get(ApiParameterDefine.MESSAGE_ID)
        session_id:str = metadata.get(ApiParameterDefine.SESSION_ID)
        
        fileBlockFilterPattern.DetectPattern(attach_file, dictOuputResponse)
        
        # 응답 데이터 처리, 우선 개발후 정리
        strAction:str = dictOuputResponse.get(ApiParameterDefine.OUT_ACTION)
        
        # 응답 결과의 전달
        # 차단일때의 응답 정리, file 타입은 우선 차단 메시지를 만들지 않는다. (향후 공통화 + UI 설정 필요)
        if PipelineFilterDefine.ACTION_BLOCK == strAction:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_BLOCK
            
            # 불필요, 최종 메시지 생성 시점에 예외처리
            # dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = ""
            
        else:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_ALLOW
            # dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = ""
            
            
        #opensearch 저장, file list 중심의 저장
        dictOpensearchDocument = {
            "@timestamp": ts_isoz(),
            "filter" : self.id,
            "filter_name": self.name,
            
            "request": {"id": message_id},
            "session": {"id": session_id},
            
            "stage":   PipelineFilterDefine.FILTER_STAGE_FILE_BLOCK,
            
            # 일단 이 값은 유지, input, output 점검 시점에 다시 정리
            "should_block": (strAction == "block"),
            
            "mode": strAction,
            
            #regex pattern에 맞춰서.. => 각 파일별 정책, 파일 별로 추가한다.
            # "policy_id" : strPolicyID,
            # "policy_name" : strPolicyName,
            
        }
        
        #file 요약, 그대로 저장
        dictOpensearchDocument.update(dictOuputResponse)
        
        self.AddLogData(LOG_INDEX_DEFINE.KEY_REGEX_FILTER, dictOpensearchDocument)
        
        #TODO: 여기 다시 정리
        # dictOuputResponse[]
        
        #TODO: 컨텐츠, 다시 고려
        
        
        # 저장 => 별도 file_filter가 무난하기는 하다.
        
        return ERR_OK
    
    