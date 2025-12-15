
from lib_include import *

from type_hint import *

from api_modules.helper.router_custom_helper import RouterCustomHelper

from api_modules.router.sub_modules.filter_pipeline_command import FilterPipelineCommand
from api_modules.router.sub_modules.output_pipeline_command import OuputPipelineCommand

'''
fast api, ipc 각각 사용, 재활용이 필요하여 클래스, 모듈화
'''

class ApiRouterImplCommand:
    
    def __init__(self):
        
        #helper, static 최소화
        self.__routerCustomHelper = RouterCustomHelper()
        
        self.__filterPipelineCommand = FilterPipelineCommand()
        self.__outputPipelineCommand = OuputPipelineCommand()
        pass
    
    
    #api, FastApi Router - 재사용
    #TODO: 비동기 코드, fast api에서는 비동기로 호출하고, ipc등 동기 상황은 ayncio로 호출 필요
    async def doFilterApiRouter(self, _mainApp:Any, modelItem: VariantFilterForm, request: Request = None) -> dict:
    
        '''
        '''
        
        # #TODO: 기존 함수는 그대로 유지하고, 신규 API를 추가한다.
        # #TODO: pipeline과 pipeline_module이 같은 개념같다. 다시 확인 필요.
        # #TODO: 이시점에 업데이트가 안되어 있다. 방안 => mainApp를 활용.
        # # dictPipelineMap:dict = app.GetState(ApiRouterEx.STATE_KEY_PIPELINE_MAP)
        
        # #순환참조 주의 => 향후 리펙토링시 구조 변경
        # from mainapp.pipeline_main_app import PipeLineMainApp
        # mainApp:PipeLineMainApp = _mainApp
        
        # dictPipelineMap:dict = mainApp.GetMainAppLinkedPipelineModules()
        
        # #테스트, 디버그
        # # LOG().debug(f"call pipeline map info = {dictPipelineMap}")
        
        # #시나리오, pipeline 리스트를 여러개 가져온다.
        # #호출시 pipeline 전달은 크게 문제가 안되며, pipeline으로 전달되는 filter 자체를 고치는 부분과
        # #호출후 결과를 모아서 전달하는 응답이 중요하다.
        
        # #가상의 pipelineid를 가져온다고 가정 => pipeline은 유지하고, 안의 로직을 모듈화 하는 방향으로 접근 + 코드 리펙토링
        
        # #TODO: 기본 Output : ApiResponseHandler를 사용하는 방안.
        # #TODO: api 응답시, 차단 메시지, masked메시지, 이런 값을 전달하는 방안의 검토
        # #차단 메시지와, API 포맷의 메시지를 전달하는 방안으로 고려한다.
        # #http 헤더도 필요하며, 데이터의 크기문제로 allow 시점에는 전달하지 않는다.
        # #helper 모듈의 개발을 검토한다.
            
        # apiResponseHandler:ApiResponseHandlerEX = ApiResponseHandlerEX()
        
        # # 기본상태코드, 성공으로 할당.
        # apiResponseHandler.attachSuccessCode()
        # apiResponseHandler.attachApiCommandCode("pipeline multiple filter")
        
        # #사용자 프롬프트, 프롬프트는 필수로 잡고, 나머지는 부가정보로 전달한다.       
        # dictBodyParameter:dict = self.__routerCustomHelper.GenerateInletBodyParameter(modelItem)
        
        # #이건 어쩔수 없다. 매 요청마다 사용자 키를 생성, 이메일과 서비스 조합
        # strUserKey:str = f"{modelItem.email}_{modelItem.ai_service}"
        
        # #TODO: uuid는 생성해야 한다. userKey로 관리된다. 자료 구조 필요, 계정관리자에서 관리해서, mainApp를 통해서 공유 받자.
        # strUUID:str = mainApp.GenerateUUID(strUserKey)
        
        # user:dict = {
        #     ApiParameterDefine.UUID : strUUID,
        #     ApiParameterDefine.NAME : modelItem.user_id,
        #     ApiParameterDefine.EMAIL : modelItem.email,
        #     ApiParameterDefine.AI_SERVICE : modelItem.ai_service,
        #     ApiParameterDefine.CLIENT_HOST : modelItem.client_host            
        # }
        
        # dictExtParameter:dict = None #부가정보 확장 parameter, 우선 무시
        
        # # #부가정보에 대해서는 향후 formdata를 model_dict로 변환하거나, dictionary로 직접 변환한다.
        # # #우선 parameter만 만든다. => 요청 데이터와 응답 데이터는 따로 만들자. 동시 접근의 문제, 사용자의 부가 옵션은 modelItem에서 가져온다.
        
        # #TODO: 메소드 이름, 향후 config로 관리, 지금은 하드코딩
        # strFilterMethodName = "inlet"
        
        # #TODO: 가공을 위해서, 전체를 모아서 저장하자.
        # dictFilterResult = {}
        
        # #TODO: regex 패턴의 inlet 범위는 사실상 한개로 압축되며, input filter 포함 2개이다.
        # lstPipeFilterName:list = modelItem.filter_list
        
        # for strPipelineFilterName in lstPipeFilterName:
            
        #     pipeline = dictPipelineMap.get(strPipelineFilterName, None)
            
        #     if None == pipeline:
        #         #TODO: 에외처리로 가는 방향, raise 처리 공통화는 2차 리펙토링때 개선            
        #         strErrorMessage:str = f"invalid pipeline, not exist pipeline, id = {strPipelineFilterName}"            
        #         self.__routerCustomHelper.GenerateHttpException(ApiErrorDefine.HTTP_404_NOT_FOUND, ApiErrorDefine.HTTP_404_NOT_FOUND_MSG, strErrorMessage, apiResponseHandler)            
        #         continue #TODO: 호출될수 없는 구문
                    
        #     #예외처리, 다시 작성 필요 => 더 적절항 방법 존재.
        #     #TODO: async 처리에 대한 대응, await의 적절한 시점 또는 concurrent.future 처리도 고려        
        #     #TODO: 신규 함수로 추가하는 방안으로 검토, 우선 regex_filter, async도 제외, 대신 병렬처리 (향후)
            
        #     dictEachFilterOutput = {}
            
        #     if hasattr(pipeline, strFilterMethodName):
                
        #         #TODO: 메소드 동적 호출 처리, no async
                
        #         methodFunction = getattr(pipeline, strFilterMethodName)
                
        #         #TODO: 내부 메소드에서 async 처리 되어 있어서, async, await 구조는 유지.
        #         #TODO: request 객체의 전달 추가, 선언쪽에서 __request__ 로 선언되어 있어, 우선 이름을 맞춘다 (장기적으로 리펙토링은 필요)
        #         # asyncio.run(methodFunction(dictBodyParameter, user, dictExtParameter, dictEachFilterOutput, __request__ = request))
        #         await methodFunction(dictBodyParameter, user, dictExtParameter, dictEachFilterOutput, __request__ = request)                
                
        #     else: #TODO: 예외 강화, 존재하지 않는 filter이면 에러 처리 => 로깅만 처리, 예외는 미발생
                
        #         strErrorMessage:str = f"invalid filter, not exist filter, fileter = {strFilterMethodName}"
        #         # RouterCustomHelper.GenerateHttpException(ApiErrorDefine.HTTP_404_NOT_FOUND, ApiErrorDefine.HTTP_404_NOT_FOUND_MSG, strErrorMessage, apiResponseHandler)
        #         LOG().error(strErrorMessage)
        #         continue
                
        #     #TODO: 응답 처리, TODO: 메시지를 만드는 부분은 재고려 필요, 우선 각 함수의 응답 결과를 저장한다.
        #     #함수의 존재 여부와 상관없이, 응답은 만든다. error 또는 응답
        #     # apiResponseHandler.attachResponse(f"filter_result/{strPipelineFilterName}", dictEachFilterOutput)
        #     # dictApiOutResponse["filter_result"][strPipelineFilterName] = dictEachFilterOutput
            
        #     dictFilterResult[strPipelineFilterName] = dictEachFilterOutput
            
        # #최종 메시지.
        # #응답 데이터 가공 좀더 개선 필요     
        # #message 형태 데이터, 데몬과 협의 대상, 아직 정리가 되지는 않았다.
        # #TODO: 우선 생성한다.
        # dictOutMessage = {            
        #     # "action" : 0, #allow = 0, block = 1, masking = 2
        #     # "masked_contents" : "",
        #     # "block_message" : "",    
            
        #     #기본값을 먼저 채운다.
        #     ApiParameterDefine.OUT_ACTION : PipelineFilterDefine.ACTION_ALLOW,
        #     ApiParameterDefine.OUT_MASKED_CONTENTS : "",
        #     ApiParameterDefine.OUT_BLOCK_MESSAGE : "",
        # }
        # apiResponseHandler.attachResponse(f"final_decision", dictOutMessage)    
            
        # #Filer별 요청후, 마지막에 취합
        # self.__routerCustomHelper.GenerateOutputFinalDecision(dictOutMessage, dictFilterResult)
        
        # #개별 pipeline 결과
        # apiResponseHandler.attachResponse(f"filter_result", dictFilterResult)
        
        # #TODO: 응답 데이터의 저장, filter 결과의 분석 vs pipeline 호출
        
        # # 사용자 정보의 저장, user 정보를 전달한다.
        # mainApp.AddUserAccount(strUserKey, user)

        # return apiResponseHandler.outResponse()
        # # return dictApiOutResponse
        
        routerCustomHelper:RouterCustomHelper = self.__routerCustomHelper
        
        return await self.__filterPipelineCommand.doFilterApiRouter(_mainApp, modelItem, request, routerCustomHelper)
    
    
    #api, FastApi Router - 재사용
    #TODO: 비동기 코드, fast api에서는 비동기로 호출하고, ipc등 동기 상황은 ayncio로 호출 필요
    async def doOutputApiRouter(self, _mainApp:Any, modelItem: OutputFilterItem, request: Request = None) -> dict:
        
        '''
        '''
        
        routerCustomHelper:RouterCustomHelper = self.__routerCustomHelper
        
        return await self.__outputPipelineCommand.doOutputApiRouter(_mainApp, modelItem, request, routerCustomHelper)
    
    
    