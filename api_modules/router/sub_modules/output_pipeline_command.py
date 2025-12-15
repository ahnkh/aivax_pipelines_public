

from lib_include import *

from type_hint import *

from api_modules.helper.router_custom_helper import RouterCustomHelper

'''
pipeline, output filter 전달 command
'''

class OuputPipelineCommand:
    
    def __init__(self):
        pass
    
    # 데이터 bridge, output filter로 연결을 위한 모듈을 전달한다.
    async def doOutputApiRouter(self, _mainApp:Any, modelItem: OutputFilterItem, request: Request, routerCustomHelper:RouterCustomHelper) -> dict:
        
        '''
        TODO: 코드가 거의 같다. 향후 리펙토링.
        '''
        
        from mainapp.pipeline_main_app import PipeLineMainApp
        mainApp:PipeLineMainApp = _mainApp
        
        dictPipelineMap:dict = mainApp.GetMainAppLinkedPipelineModules()
        
        #TODO: ouput은 한개. 이름을 고정한다.
        strOuputFilterPipeline = "output_filter"
        
        apiResponseHandler:ApiResponseHandlerEX = ApiResponseHandlerEX()
        
        # 기본상태코드, 성공으로 할당.
        apiResponseHandler.attachSuccessCode()
        apiResponseHandler.attachApiCommandCode("output api router")
        
        pipeline = dictPipelineMap.get(strOuputFilterPipeline, None)
        
        # 없을때는 예외처리.
        if None == pipeline:
        
            strErrorMessage:str = f"invalid pipeline, not exist pipeline, id = {strOuputFilterPipeline}"            
            routerCustomHelper.GenerateHttpException(ApiErrorDefine.HTTP_404_NOT_FOUND, ApiErrorDefine.HTTP_404_NOT_FOUND_MSG, strErrorMessage, apiResponseHandler)
            
            return {} #TODO: 호출될 수 없는 구문
        
        '''
        거의 동일하게
        body : {
            "metadata" :{
                "session_id" : "",
                "message_id" : "",
                "response_id" : "",
                "channel" : "web"
            },
            
            "messages": [
                {"role":"user", "content":strPromptMessage}
            ]
        }
        user : {
            "user_id": "",
            "user_role" : "",
            "user_email" : ""
            
        }
        
        # 모델, 사용량, latency 등은 우선 제외
        # output, 최대 20만자까지 제외 (200_000)
        '''
        
        #사용자 프롬프트, 프롬프트는 필수로 잡고, 나머지는 부가정보로 전달한다.       
        dictBodyParameter:dict = routerCustomHelper.GenerateOutletBodyParameter(modelItem)
        
        #  직접 호출
        # body:dict = {}
        
        # 사용자 정보, 있으면 전달, 저장은 하지 않는다.
        user:dict = {
            ApiParameterDefine.NAME : modelItem.user_id,
            ApiParameterDefine.EMAIL : modelItem.email,
            ApiParameterDefine.AI_SERVICE : modelItem.ai_service,
            ApiParameterDefine.CLIENT_HOST : modelItem.client_host,   
        }
        
        dictOutputResponse = {}
        await pipeline.outlet(dictBodyParameter, user, dictOutputResponse)
        
        apiResponseHandler.attachResponse(f"out_result", dictOutputResponse)
        
        #output의 응답은, 에러 처리 정도로만 활용한다.
        return apiResponseHandler.outResponse()
            