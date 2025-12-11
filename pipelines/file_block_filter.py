

from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.filter_pattern_manager import FilterPatternManager
from block_filter_modules.filter_pattern.helper.file_filter_pattern import FileFilterPattern

'''
file 분석 filter, pipeline 신규 추가
'''

class Pipeline(PipelineBase):
    
    def __init__(self):
        
        '''
        '''
        
        super().__init__()
        
        pass
    
    
    #pipeline, inlet, outlet 중 inlet 만 가져간다.
    async def inlet(self, body: Dict[str, Any], __user__: Optional[dict] = None, dictExtParameter:dict = None, dictOuputResponse:dict = None, __request__: Optional[Request] = None) : #-> Dict[str, Any]:
        
        '''
        TODO: 이 기능은 multiple filter 에서만 호출한다.
        TODO: prompt 대신, filer 정보가 넘어온다., 데이터는 body 에서 수집되어서 broad cast하도록 구성한다.
        '''
        
        # 응답처리
        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
        
        # TODO: 접속정보, 그대로 가져간다. 공통 코드는 공통화.
        
        
        fileBlockFilterPattern:FileFilterPattern = self.GetFilterPatternModule(FilterPatternManager.PATTERN_FILTER_FILE)
        
        #file 분석, 정보 수집 + 정책 차단 여부
        #file은 다수일수 있다. 다만 pipeline의 regex filter에서는 제외하고, file filter에서만 제공한다.
        #TODO: opensearch의 filter 분리는 향후 고민.
        attach_file:list = body.get(ApiParameterDefine.ATTACH_FILE)
        
        fileBlockFilterPattern.DetectPattern()
        
        #TODO: 컨텐츠, 다시 고려
        
        # 응답 결과의 전달
        # 저장 => 별도 file_filter가 무난하기는 하다.
        
        return ERR_OK
    
    