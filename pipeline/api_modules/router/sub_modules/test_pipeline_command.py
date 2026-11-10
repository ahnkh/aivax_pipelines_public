
from lib_include import *

from type_hint import *

from api_modules.helper.router_custom_helper import RouterCustomHelper

'''
pipeline, test filter command
'''

class TestPipelineCommand:
    
    # typemask, 3개이다.
    MASK_REGEX = 1 #0b0001
    MASK_SLM = 2 #0b0010
    # MASK_ALL = 3
    
    def __init__(self):
        
        pass
    
    
    # test수행, regex, slm filter에 대한 호출, typeMask로 분기
    async def doTestApiRouter(self, _mainApp:Any, modelItem: FilterRuleTestItem, request: Request, routerCustomHelper:RouterCustomHelper) -> dict:
        
        '''
        '''
        
        from mainapp.pipeline_main_app import PipeLineMainApp
        mainApp:PipeLineMainApp = _mainApp
        
        dictPipelineMap:dict = mainApp.GetMainAppLinkedPipelineModules()
        
        # test filter 수행
        # typeMask에 따라서 분기되며 1:Regex, 2:SLM, 3: Regex+SLM 이다.
        
        nTypeMask:int = modelItem.typeMask
        
        
        dictRegexOutputResponse = {}
        dictSLMOutputResponse = {}
        
        # regex filter 수행
        if (TestPipelineCommand.MASK_REGEX & nTypeMask):
            
            await self.__doRegexFilter(dictPipelineMap, modelItem, dictRegexOutputResponse)
            
            # testrule, 응답값, regex_result, slm_result로 분기, 하드코딩은 나중에 한번에 같이 개선
            # dictOutResponse["regex_result"] = dictRegexOutputResponse
            # pass
            
        # slm filter 수행
        if (TestPipelineCommand.MASK_SLM & nTypeMask):
            
            await self.__doSLMFilter(dictPipelineMap, modelItem, dictSLMOutputResponse)
            # dictOutResponse["slm_result"] = dictRegexOutputResponse
            # dictOutResponse.update(dictSLMOutputResponse)
            # pass
        
        
        # 응답 코드 관리 => 코드 분리
        
        apiResponseHandler:ApiResponseHandlerEX = ApiResponseHandlerEX()
    
        # 기본상태코드, 성공으로 할당, 기존 키값은 유지, output만 교체
        apiResponseHandler.attachSuccessCode()    
        apiResponseHandler.attachApiCommandCode("rule filter test")
        
        # 일단 Regex
        # TODO: 둘다 수행하는 경우를 고려, output을 합쳐야 한다.
        
        # test
        
        # dictOutResponse.update(dictSLMOutputResponse)
        
        # apiResponseHandler.attachResponse("filter_result", dictOutResponse)
        
        apiResponseHandler.attachResponse("regex_result", dictRegexOutputResponse)
        apiResponseHandler.attachResponse("slm_result", dictSLMOutputResponse)
        
        return apiResponseHandler.outResponse()
    
    
    ################################### private
    
    # regex Filter 수행
    async def __doRegexFilter(self, dictPipelineMap:dict, modelItem: FilterRuleTestItem, dictRegexOutputResponse:dict):
        
        '''
        '''
        
        # regex filter
        strPipelineFilterName:str = "secret_filter"
        
        #일단, 무조건 있다는 가정
        from pipelines.detect_secrets import Pipeline
        pipeline:Pipeline = dictPipelineMap.get(strPipelineFilterName, None)
        
        strRule:str = modelItem.rule
        strAction:str = modelItem.action
        strPrompt:str = modelItem.prompt
        
        await pipeline.testRule(strPrompt, strRule, strAction, dictRegexOutputResponse)
        # pass
        
        
    # slm filter 수행
    async def __doSLMFilter(self, dictPipelineMap:dict, modelItem: FilterRuleTestItem, dictSLMOutputResponse:dict):
        
        '''
        '''
        
        from pipelines.slm_filter_v2 import Pipeline
        
        strPipelineFilterName:str = "slm_filter"
        
        pipeline:Pipeline = dictPipelineMap.get(strPipelineFilterName, None)
        
        strAction:str = modelItem.action
        strPrompt:str = modelItem.prompt
        
        await pipeline.testRule(strPrompt, strAction, dictSLMOutputResponse)
        
        pass
    
        
        
        