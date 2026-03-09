
from lib_include import *

from type_hint import *

from api_modules.helper.router_custom_helper import RouterCustomHelper

'''
pipeline을 통한 filter 요청 기능 관리
모듈 분리
'''

class FilterPipelineCommand:
    
    def __init__(self):
        pass
    
    
    async def doFilterApiRouter(self, _mainApp:Any, modelItem: VariantFilterForm, request: Request, routerCustomHelper:RouterCustomHelper) -> dict:
    
        '''
        '''
        
        from mainapp.pipeline_main_app import PipeLineMainApp
        mainApp:PipeLineMainApp = _mainApp
        
        dictPipelineMap:dict = mainApp.GetMainAppLinkedPipelineModules()
        
        from block_filter_modules.filter_policy.helper.filter_custom_config import FilterCustomConfig
        filterCustomConfig:FilterCustomConfig = mainApp.GetFilterCustomConfig()
        
        customFilterConfigItem: PipelineCustomFilterConfigItem = filterCustomConfig.filterConfig
        
        #sslproxy, 응답결과 제어
        nSSLProxyBypassBitMask:int = filterCustomConfig.GetSSLProxyBypassMask()
        
        # pipeline 호출 제어, true 설정되면 차단이 발생해도 무시하고 계속 수행한다.
        # bAllPipelineFilterDetect:bool = filterCustomConfig.IsAllPipelineFilterDetect()
        
        #pipeline 호출 제어, true 설정되면 차단이 발생해도 무시하고 계속 수행한다.
        bNextDetectAfterBlock:bool = customFilterConfigItem.next_detect_after_block
        
        #시나리오, pipeline 리스트를 여러개 가져온다.
        #호출시 pipeline 전달은 크게 문제가 안되며, pipeline으로 전달되는 filter 자체를 고치는 부분과
        #호출후 결과를 모아서 전달하는 응답이 중요하다.
        
        #가상의 pipelineid를 가져온다고 가정 => pipeline은 유지하고, 안의 로직을 모듈화 하는 방향으로 접근 + 코드 리펙토링
        
        apiResponseHandler:ApiResponseHandlerEX = ApiResponseHandlerEX()
        
        # 기본상태코드, 성공으로 할당.
        apiResponseHandler.attachSuccessCode()
        apiResponseHandler.attachApiCommandCode("pipeline multiple filter")
        
        #사용자 프롬프트, 프롬프트는 필수로 잡고, 나머지는 부가정보로 전달한다.       
        dictBodyParameter:dict = routerCustomHelper.GenerateInletBodyParameter(modelItem)
        
        #TODO: 사용자 정보 생성기능 보강
        # file등 사용자 정보가 수집되지 않을때, email, service가 없을때는 세션id 정보를 통해서 과거 데이터를 수집하도록 개선.
        #조건, 분기 필요, session_id는 body.metadata로 전달된다.
        # strSessionID:str = modelItem.session_id
        
        strUserKey:str = f"{modelItem.email}_{modelItem.ai_service}"
        
        #TODO: uuid는 생성해야 한다. userKey로 관리된다. 자료 구조 필요, 계정관리자에서 관리해서, mainApp를 통해서 공유 받자.
        strUUID:str = mainApp.GenerateUUID(strUserKey)
        
        #사용자 프롬프트의 최신 데이터 저장, 사양 변경으로 중복이지만 추가한다.
        lstMessage:list = dictBodyParameter.get(ApiParameterDefine.MESSAGES)
        last:dict = lstMessage[-1]
        prompt:str = last.get(ApiParameterDefine.MESSAGE_PROMPT)
        
        user:dict = {
            ApiParameterDefine.UUID : strUUID,
            ApiParameterDefine.NAME : modelItem.user_id,
            ApiParameterDefine.EMAIL : modelItem.email,
            ApiParameterDefine.AI_SERVICE : modelItem.ai_service,
            ApiParameterDefine.CLIENT_HOST : modelItem.client_host,   
            
            #프롬프트, user에도 담아서 전달한다.  
            ApiParameterDefine.MESSAGE_PROMPT : prompt    
        }
        
        #customFilterConfigItem 를 전달하도록 사양 변경
        # dictExtParameter:dict = None #부가정보 확장 parameter, 우선 무시
        
        # #부가정보에 대해서는 향후 formdata를 model_dict로 변환하거나, dictionary로 직접 변환한다.
        # #우선 parameter만 만든다. => 요청 데이터와 응답 데이터는 따로 만들자. 동시 접근의 문제, 사용자의 부가 옵션은 modelItem에서 가져온다.
        
        #TODO: 메소드 이름, 향후 config로 관리, 지금은 하드코딩
        strFilterMethodName = "inlet"
        
        #TODO: 가공을 위해서, 전체를 모아서 저장하자.
        dictFilterResult = {}
        
        #TODO: regex 패턴의 inlet 범위는 사실상 한개로 압축되며, input filter 포함 2개이다.
        lstPipeFilterName:list = modelItem.filter_list
        
        #모든 pipeline에 대한 순회
        for strPipelineFilterName in lstPipeFilterName:
            
            pipeline = dictPipelineMap.get(strPipelineFilterName, None)
            
            if None == pipeline:
                #TODO: 에외처리로 가는 방향, raise 처리 공통화는 2차 리펙토링때 개선            
                strErrorMessage:str = f"invalid pipeline, not exist pipeline, id = {strPipelineFilterName}"            
                routerCustomHelper.GenerateHttpException(ApiErrorDefine.HTTP_404_NOT_FOUND, ApiErrorDefine.HTTP_404_NOT_FOUND_MSG, strErrorMessage, apiResponseHandler)            
                continue #TODO: 호출될수 없는 구문
                                
            #TODO: async 처리에 대한 대응, await의 적절한 시점 또는 concurrent.future 처리도 고려        
            #TODO: 신규 함수로 추가하는 방안으로 검토, 우선 regex_filter, async도 제외, 대신 병렬처리 (향후)
            
            #개별 filter에 대한 ouput 응답
            dictEachFilterOutput = {}
            
            # 이전 코드
            '''
            if hasattr(pipeline, strFilterMethodName):
                
                #TODO: 메소드 동적 호출 처리, no async
                
                methodFunction = getattr(pipeline, strFilterMethodName)
                
                #TODO: 내부 메소드에서 async 처리 되어 있어서, async, await 구조는 유지.
                #TODO: request 객체의 전달 추가, 선언쪽에서 __request__ 로 선언되어 있어, 우선 이름을 맞춘다 (장기적으로 리펙토링은 필요)
                # asyncio.run(methodFunction(dictBodyParameter, user, dictExtParameter, dictEachFilterOutput, __request__ = request))
                
                #TODO: 너무 많은 인자. 최초 pipeline 구조로 인한 어쩔수 없는.. next 버전에서는 리펙토링 필요.
                await methodFunction(dictBodyParameter, user, dictExtParameter, dictEachFilterOutput, __request__ = request)   
                
                #TODO: 26.03.03 분기 옵션화, 제어, 차단이 발생했을때, 흐름 제어 필요 
                #모든 패턴에 대해서 탐지/차단 수행옵션을 제공하고, 기본은 차단후 종료이다.
                
                #응답의 처리, 차단, block이 발생했으면, 종료 처리
                #masking, accept는 더 수행이 되어야 한다. (25.12 기준 최대 3개의 inlet 제공)
                if True == self.__isBlockFilter(dictEachFilterOutput):
                    # 테스트용 로그
                    # LOG().info(f"block inlet filter {strPipelineFilterName}, finish")
                    #추가후 종료
                    dictFilterResult[strPipelineFilterName] = dictEachFilterOutput
                    break
                    #pass if
                #pass if
                
            else: #TODO: 예외 강화, 존재하지 않는 filter이면 에러 처리 => 로깅만 처리, 예외는 미발생
                
                strErrorMessage:str = f"invalid filter, not exist filter, filter = {strFilterMethodName}"
                # RouterCustomHelper.GenerateHttpException(ApiErrorDefine.HTTP_404_NOT_FOUND, ApiErrorDefine.HTTP_404_NOT_FOUND_MSG, strErrorMessage, apiResponseHandler)
                LOG().error(strErrorMessage)
                continue
            '''
            
            #예외처리 => 있어서는 안되는 구문, 종료시킨다.
            if False == hasattr(pipeline, strFilterMethodName):
                
                strErrorMessage:str = f"invalid filter, not exist filter, filter = {strFilterMethodName}"
                RouterCustomHelper.GenerateHttpException(ApiErrorDefine.HTTP_404_NOT_FOUND, ApiErrorDefine.HTTP_404_NOT_FOUND_MSG, strErrorMessage, apiResponseHandler)
                # LOG().error(strErrorMessage)
            
            methodFunction = getattr(pipeline, strFilterMethodName)
                
            #TODO: request 객체의 전달 추가, 선언쪽에서 __request__ 로 선언되어 있어, 우선 이름을 맞춘다 (장기적으로 리펙토링은 필요)
            # asyncio.run(methodFunction(dictBodyParameter, user, dictExtParameter, dictEachFilterOutput, __request__ = request))
            
            #TODO: 너무 많은 인자. 최초 pipeline 구조로 인한 어쩔수 없는.. next 버전에서는 리펙토링 필요. 
            # await methodFunction(dictBodyParameter, user, dictExtParameter, dictEachFilterOutput, __request__ = request)   
            await methodFunction(dictBodyParameter, user, customFilterConfigItem, dictEachFilterOutput, __request__ = request)
            
            # 조건문 좀더 정확하게 검증.
            if False == bNextDetectAfterBlock:
                
                if True == self.__isBlockFilter(dictEachFilterOutput):
                    dictFilterResult[strPipelineFilterName] = dictEachFilterOutput
                    
                    #차단 수행후, loop 종료
                    # LOG().info(f"block inlet filter {strPipelineFilterName}, finish")
                    break
                
            #TODO: 응답 처리, TODO: 메시지를 만드는 부분은 재고려 필요, 우선 각 함수의 응답 결과를 저장한다.
            #함수의 존재 여부와 상관없이, 응답은 만든다. error 또는 응답
            # apiResponseHandler.attachResponse(f"filter_result/{strPipelineFilterName}", dictEachFilterOutput)
            # dictApiOutResponse["filter_result"][strPipelineFilterName] = dictEachFilterOutput
            
            dictFilterResult[strPipelineFilterName] = dictEachFilterOutput
            #pass for
            
        #최종 메시지.
        #응답 데이터 가공 좀더 개선 필요     
        #message 형태 데이터, 데몬과 협의 대상, 아직 정리가 되지는 않았다.
        #TODO: 우선 생성한다.
        dictFinalOutMessage = {            
            # "action" : 0, #allow = 0, block = 1, masking = 2
            # "masked_contents" : "",
            # "block_message" : "",    
            
            #기본값을 먼저 채운다.
            ApiParameterDefine.OUT_ACTION : PipelineFilterDefine.ACTION_ALLOW,
            ApiParameterDefine.OUT_ACTION_CODE : PipelineFilterDefine.CODE_ALLOW,
            ApiParameterDefine.OUT_MASKED_CONTENTS : "",
            ApiParameterDefine.OUT_BLOCK_MESSAGE : "",
        }
            
        #Filter별 요청후, 마지막에 취합
        
        
        routerCustomHelper.GenerateOutputFinalDecision(dictFinalOutMessage, dictFilterResult, nSSLProxyBypassBitMask)
        
        apiResponseHandler.attachResponse(f"final_decision", dictFinalOutMessage)
        
        #개별 pipeline 결과, 취합된 결과의 저장
        apiResponseHandler.attachResponse(f"filter_result", dictFilterResult)
        
        #TODO: 응답 데이터의 저장, filter 결과의 분석 vs pipeline 호출
        
        # 사용자 정보의 저장, user 정보를 전달한다.
        mainApp.AddUserAccount(strUserKey, user)
        
        # TODO: sslproxy 전달 프롬프트
        dictApiOutResponse:dict = apiResponseHandler.outResponse()

        # return apiResponseHandler.outResponse()
        return dictApiOutResponse
        
    ########################################################## private
    
    #차단결과, 차단이 발생했으면, 다음 inlet은 동작하지 않고 skip
    def __isBlockFilter(self, dictEachFilterOutput:dict) -> bool:
        
        '''
        '''
        
        action:str = dictEachFilterOutput.get(ApiParameterDefine.OUT_ACTION, PipelineFilterDefine.ACTION_ALLOW)
        action_code:int = dictEachFilterOutput.get(ApiParameterDefine.OUT_ACTION_CODE, PipelineFilterDefine.CODE_ALLOW)
        
        if PipelineFilterDefine.CODE_BLOCK == action_code or PipelineFilterDefine.ACTION_BLOCK == action:            
            return True
        
        return False
        