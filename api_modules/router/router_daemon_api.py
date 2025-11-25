
# router_user.py
from fastapi import APIRouter

#동기, 비동기 선택 옵션 추가 (향후를 위해서 구현)
import inspect

from lib_include import *

from type_hint import *

#TODO: 매번 호출해야 하는 문제. 이건 static으로 하자...
from api_modules.helper.router_custom_helper import RouterCustomHelper

app = ApiRouterEx(
    # prefix="/",
    # tags=["pipline"],
)

#신규 추가, 다중 필터 TODO: 기본 구성은 inlet을 참고한다.
@app.post("/v1/filter/multiple_filter")
async def filter_prompt_from_engine(modelItem: VariantFilterForm, request: Request):
    
    '''
    기존 inlet의 확장, 필터를 직접 지정하며, 다수의 필터를 호출하는 기능을 제공한다.
    
    **filter_list** : 차단 필터 리스트    
    - _secret_filter_ : <u>API 차단 필터</u>
    
    - llm_filter : AI 필터 (불완전 버전, 검증 필요)
    - input_filter : opensearch 저장 (프롬프트)
    - output_filter : opensearch 저장 (LLM 응답)    
    
    - <del>load_detect_secrets : AI 차단 필터 (불완전 버전, 검증 필요)</del>
    - <del>inlet_raw_logger : 테스트용, 미사용</del>
    - <del>regex_filter : 정규표현식 기반 필터 (정규식 불완전, 탐지안되는 기능)</del>
    
    **prompt** : 프롬프트 메시지 (기존 message의 contents)    
    - 다음의 user, contents 구조에서 프롬프트만 사용하며, 차기 버전에서 user 정보가 필요시 추가 예정
    - 변경전 : {"role": "user", "content": "안녕하세요"}
    - _변경후_ : <u>"prompt": "프롬프트를 입력해주세요"</u>
    
    **encoding** : 프롬프트 인코딩 여부 (기본값 비활성화)    
    - _true_ : <u>encoding (기본값 미사용, 평문 처리)</u>
    
    _**etc_flag** : 예외된 플래그 옵션 (차기 버전에서 추가 계획)_
    
    **user_role** :
    - id : 사용자 ID
    - email : email
    - client_host : client host ip
    - session_id : session id
    
    예제   
    ```bash 
    curl -X 'POST' 
        'http://127.0.0.1:7000/v1/filter/multiple_filter' 
        -H 'accept: application/json' 
        -H 'Content-Type: application/json'
        -d '{
        "filter_list": [
            "secret_filter"
        ],
        "prompt": "내 API key는 API_key=sk-1234567-0000-abdcdef 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요",
        "encoding": false,
        "etc_flag": {            
        }
    }'    
    ```
    
    응답 메시지 참고 사항
    - action: 0: allow, 1: block, 2: masking
    '''
    
    return await doRouterFunction("doFilterApiRouter", modelItem, request)
    
    # try:

    #     return await doFilterApiRouter(modelItem, request)

    # except HTTPException as err:

    #     LOG().error(traceback.format_exc()) 

    #     dictOutput:dict = err.detail    
    #     return dictOutput

    # except Exception as err:
        
    #     # LOG().error(str(err))        
    #     LOG().error(traceback.format_exc())  
        
    #     apiResponseHandler = ApiResponseHandlerEX()
    #     apiResponseHandler.attachFailCode(ApiErrorDefine.API_UNKNOWN_ERROR, ApiErrorDefine.API_UNKNOWN_ERROR_MSG, str(err))

    #     return apiResponseHandler.outResponse()
    
    
#정책 추가 인터페이스, 정책에 대한 테스트와 응답 결과만 반환한다.
@app.post("/v1/filter/testrule")
async def testFilterRule(modelItem: FilterRuleTestItem, request: Request) -> dict:
    '''
    '''
    
    return await doRouterFunction("doTestFilterRule", modelItem, request)
    
    # try:

    #     return await doTestFilterRule(modelItem, request)

    # except HTTPException as err:

    #     LOG().error(traceback.format_exc()) 

    #     dictOutput:dict = err.detail    
    #     return dictOutput

    # except Exception as err:
        
    #     # LOG().error(str(err))        
    #     LOG().error(traceback.format_exc())  
        
    #     apiResponseHandler = ApiResponseHandlerEX()
    #     apiResponseHandler.attachFailCode(ApiErrorDefine.API_UNKNOWN_ERROR, ApiErrorDefine.API_UNKNOWN_ERROR_MSG, str(err))

    #     return apiResponseHandler.outResponse()
    
    
#api router 실행, 공통화
# async def doRouterFuction(strMethodName:str, modelItem: Any, request: Request) -> dict:
async def doRouterFunction(strMethodName:str, *args, **kwargs) -> dict:
    '''
    '''
    
    try:
        
        # 현재 모듈 참조
        module = sys.modules[__name__]  
        
        #TODO: 인자 주의
        methodFunction = getattr(module, strMethodName)
        
        if inspect.iscoroutinefunction(methodFunction):
            return await methodFunction(*args, **kwargs)
        else:
            return methodFunction(*args, **kwargs)

    except HTTPException as err:

        LOG().error(traceback.format_exc()) 

        dictOutput:dict = err.detail    
        return dictOutput

    except Exception as err:
        
        # LOG().error(str(err))        
        LOG().error(traceback.format_exc())  
        
        apiResponseHandler = ApiResponseHandlerEX()
        apiResponseHandler.attachFailCode(ApiErrorDefine.API_UNKNOWN_ERROR, ApiErrorDefine.API_UNKNOWN_ERROR_MSG, str(err))

        return apiResponseHandler.outResponse()

    
#저장 인터페이스, TODO: API가 많을것 같지는 않다. 만약에 많아지면, 호출 기능 공통화, 이 API 까지는 각각 개발
@app.post("/v1/log/add-log")
async def addLogToOpenSearch(request: Request):
    
    '''
    opensearch 에 직접 로그를 저장하는 기능을 제공한다.
    
    - 주의) 서식은 json 형태의 로그 저장 서식이며, 다음의 예시대로 제공한다.
    - 정형화되지 않은 json 서식으로, swagger에서는 테스트 UI를 제공하지 않는다.
    
    **index**
    
    - input_filter : 프롬프트 입력 데이터의 저장
    - regex_filter : filter 결과의 저장
    - output_filter : LLM 응답 데이터의 저장
    
    예제   
    ```bash 
    curl -X 'POST' \
        'http://127.0.0.1:7000/v1/log/add-log' \\
        -H 'accept: application/json' \\
        -H 'Content-Type: application/json'\\
        -d '{
        "index": "input_filter",
        
        "log_list" : [
            {
                "@timestamp": "2025-10-21T22:52:10.094136Z",
                
                "event": {
                    "id": null,
                    "type": "query"
                },
                
                 "request": {
                    "id": null
                },
                "session": {
                    "id": null
                },
                "user": {
                    "id": null,
                    "role": null
                },
                
                 "src": {
                    "ip": "127.0.0.1"
                },
                "channel": "web",
                "query": {
                    "text": "저장 테스트 로그"
                }
            }
        ]        
    }'  
    '''
    
    #TODO: 여기까지만 개별로 생성, 이후 API가 추가되면 공통화
    
    try:
    
        byteRawBody = await request.body()
        
        dictItemModel = {}
        JsonHelper.LoadToDictionary(byteRawBody, dictItemModel)

        return await doLogApiRouter(dictItemModel)

    except HTTPException as err:

        LOG().error(traceback.format_exc()) 

        dictOutput:dict = err.detail    
        return dictOutput

    except Exception as err:
        
        # LOG().error(str(err))        
        LOG().error(traceback.format_exc())  
        
        apiResponseHandler = ApiResponseHandlerEX()
        apiResponseHandler.attachFailCode(ApiErrorDefine.API_UNKNOWN_ERROR, ApiErrorDefine.API_UNKNOWN_ERROR_MSG, str(err))

        return apiResponseHandler.outResponse()
   
    
# filter api 모듈 본체, TODO: 내부 함수가 async로 구현되어 있어, async로 선언될수 밖에 없는 구조이다.    
async def doFilterApiRouter(modelItem: VariantFilterForm, request: Request) -> dict:
    
    '''
    '''
    
    #TODO: 기존 함수는 그대로 유지하고, 신규 API를 추가한다.
    #TODO: pipeline과 pipeline_module이 같은 개념같다. 다시 확인 필요.
    #TODO: 이시점에 업데이트가 안되어 있다. 방안 => mainApp를 활용.
    # dictPipelineMap:dict = app.GetState(ApiRouterEx.STATE_KEY_PIPELINE_MAP)
    
    #순환참조 주의 => 향후 리펙토링시 구조 변경
    from mainapp.pipeline_main_app import PipeLineMainApp

    mainApp:PipeLineMainApp = app.GetState(ApiRouterEx.STATE_KEY_MAINAPP)
    
    dictPipelineMap:dict = mainApp.GetMainAppLinkedPipelineModules()
    
    #테스트, 디버그
    LOG().debug(f"call pipeline map info = {dictPipelineMap}")
    
    #시나리오, pipeline 리스트를 여러개 가져온다.
    #호출시 pipeline 전달은 크게 문제가 안되며, pipeline으로 전달되는 filter 자체를 고치는 부분과
    #호출후 결과를 모아서 전달하는 응답이 중요하다.
    
    #가상의 pipelineid를 가져온다고 가정 => pipeline은 유지하고, 안의 로직을 모듈화 하는 방향으로 접근 + 코드 리펙토링
    
    #TODO: 기본 Output : ApiResponseHandler를 사용하는 방안.
    #TODO: api 응답시, 차단 메시지, masked메시지, 이런 값을 전달하는 방안의 검토
    #차단 메시지와, API 포맷의 메시지를 전달하는 방안으로 고려한다.
    #http 헤더도 필요하며, 데이터의 크기문제로 allow 시점에는 전달하지 않는다.
    #helper 모듈의 개발을 검토한다.
        
    apiResponseHandler:ApiResponseHandlerEX = ApiResponseHandlerEX()
    
    # 기본상태코드, 성공으로 할당.
    apiResponseHandler.attachSuccessCode()
    apiResponseHandler.attachApiCommandCode("pipeline multiple filter")
    
    #사용자 프롬프트, 프롬프트는 필수로 잡고, 나머지는 부가정보로 전달한다.
    # strPromptMessage:str = modelItem.prompt    
    # strPromptMessage:str = RouterCustomHelper.ConvertPromptMessage(modelItem)
    
    dictBodyParameter:dict = RouterCustomHelper.GenerateInletBodyParameter(modelItem)
    
    user:dict = {
        ApiParameterDefine.NAME : modelItem.user_role.id,
        ApiParameterDefine.EMAIL : modelItem.user_role.email
    }
    
    dictExtParameter:dict = None #부가정보 확장 parameter, 우선 무시
    
    # #부가정보에 대해서는 향후 formdata를 model_dict로 변환하거나, dictionary로 직접 변환한다.
    # #우선 parameter만 만든다. => 요청 데이터와 응답 데이터는 따로 만들자. 동시 접근의 문제, 사용자의 부가 옵션은 modelItem에서 가져온다.
    # dictExtParameter = {
    #     #부가정보, 추가적인 파라미터는 우선 여기에 추가 
    #     #만일 modelItem의 항목이 늘어나면, 여기에 추가해서 전달
    #     # ApiParameterDefine.PARAM_MAIN_APP : mainApp
    # }
    
    #inlet을 호출하는 것은 유지해보자.
    
    #TODO: 메소드 이름, 향후 config로 관리, 지금은 하드코딩
    strFilterMethodName = "inlet"
    
    #TODO: 가공을 위해서, 전체를 모아서 저장하자.
    dictFilterResult = {}
    
    #일단 작성후 refactoring
    lstPipeFilterName:list = modelItem.filter_list
    for strPipelineFilterName in lstPipeFilterName:
        
        pipeline = dictPipelineMap.get(strPipelineFilterName, None)
        
        if None == pipeline:
            #TODO: 에외처리로 가는 방향, raise 처리 공통화는 2차 리펙토링때 개선            
            strErrorMessage:str = f"invalid pipeline, not exist pipeline, id = {strPipelineFilterName}"            
            RouterCustomHelper.GenerateHttpException(ApiErrorDefine.HTTP_404_NOT_FOUND, ApiErrorDefine.HTTP_404_NOT_FOUND_MSG, strErrorMessage, apiResponseHandler)            
            continue #TODO: 호출될수 없는 구문
                
        #예외처리, 다시 작성 필요 => 더 적절항 방법 존재.
        #TODO: async 처리에 대한 대응, await의 적절한 시점 또는 concurrent.future 처리도 고려        
        #TODO: 신규 함수로 추가하는 방안으로 검토, 우선 regex_filter, async도 제외, 대신 병렬처리 (향후)
        
        dictEachFilterOutput = {}
        
        if hasattr(pipeline, strFilterMethodName):
            
            #TODO: 메소드 동적 호출 처리, no async
            
            methodFunction = getattr(pipeline, strFilterMethodName)
            
            #TODO: 내부 메소드에서 async 처리 되어 있어서, async, await 구조는 유지.
            #TODO: request 객체의 전달 추가, 선언쪽에서 __request__ 로 선언되어 있어, 우선 이름을 맞춘다 (장기적으로 리펙토링은 필요)
            await methodFunction(dictBodyParameter, user, dictExtParameter, dictEachFilterOutput, __request__ = request)
            
            #테스트, 응답 메시지 처리의 예를 위한 임의 추가
            # dictEachFilterOutput["description"] = f"{strPipelineFilterName} 차단의 결과입니다."
            
        else: #TODO: 예외 강화, 존재하지 않는 filter이면 에러 처리 => 로깅만 처리, 예외는 미발생
            
            strErrorMessage:str = f"invalid filter, not exist filter, fileter = {strFilterMethodName}"
            # RouterCustomHelper.GenerateHttpException(ApiErrorDefine.HTTP_404_NOT_FOUND, ApiErrorDefine.HTTP_404_NOT_FOUND_MSG, strErrorMessage, apiResponseHandler)
            LOG().error(strErrorMessage)
            continue
            
        #TODO: 응답 처리, TODO: 메시지를 만드는 부분은 재고려 필요, 우선 각 함수의 응답 결과를 저장한다.
        #함수의 존재 여부와 상관없이, 응답은 만든다. error 또는 응답
        # apiResponseHandler.attachResponse(f"filter_result/{strPipelineFilterName}", dictEachFilterOutput)
        # dictApiOutResponse["filter_result"][strPipelineFilterName] = dictEachFilterOutput
        
        dictFilterResult[strPipelineFilterName] = dictEachFilterOutput
        
        
    #최종 메시지.
    #응답 데이터 가공 좀더 개선 필요     
    #message 형태 데이터, 데몬과 협의 대상, 아직 정리가 되지는 않았다.
    #TODO: 우선 생성한다.
    dictOutMessage = {            
        # "action" : 0, #allow = 0, block = 1, masking = 2
        # "masked_contents" : "",
        # "block_message" : "",    
        
        #기본값을 먼저 채운다.
        ApiParameterDefine.OUT_ACTION : PipelineFilterDefine.ACTION_ALLOW,
        ApiParameterDefine.OUT_MASKED_CONTENTS : "",
        ApiParameterDefine.OUT_BLOCK_MESSAGE : "",
    }
    apiResponseHandler.attachResponse(f"final_decision", dictOutMessage)    
        
    #Filer별 요청후, 마지막에 취합
    RouterCustomHelper.GenerateOutputFinalDecision(dictOutMessage, dictFilterResult)
    
    #개별 pipeline 결과
    apiResponseHandler.attachResponse(f"filter_result", dictFilterResult)
    
    #TODO: 응답 데이터의 저장, filter 결과의 분석 vs pipeline 호출

    return apiResponseHandler.outResponse()
    # return dictApiOutResponse
    

#로그 저장 기능, 여기까지만 별도로 작성하고, 신규 API가 필요하면 정규화 + 리펙토링
async def doLogApiRouter(dictItemModel: dict) -> dict:
    
    '''
    '''
    
    #일단, 개발후 리펙토링
    index:str = dictItemModel.get("index")
    
    log_list:list = dictItemModel.get("log_list")
    
    #순환참조 주의 => 향후 리펙토링시 구조 변경
    from mainapp.pipeline_main_app import PipeLineMainApp

    mainApp:PipeLineMainApp = app.GetState(ApiRouterEx.STATE_KEY_MAINAPP)
    
    for dictLogData in log_list:
        
        mainApp.AddLogData(index, dictLogData)
        
    #TODO: 오류가 발생하지 않으면, 일단 success로 전송한다.
    
    # apiResponseHandler:ApiResponseHandlerEX = ApiResponseHandlerEX()
    
    # # 기본상태코드, 성공으로 할당.
    # apiResponseHandler.attachSuccessCode()    
    # apiResponseHandler.attachApiCommandCode("log add api")
    
    strOutputData:str = f"insert log, index = {index}, count = {len(log_list)}"
    
    # apiResponseHandler.attachResponse(f"out_message_data", strOutputData) 
    
    # return apiResponseHandler.outResponse()

    return GetApiOutResponse("log add api", "out_message_data", strOutputData)


#filter 정책 테스트, 우선 만들고, 공통화, 리펙토링
async def doTestFilterRule(modelItem: FilterRuleTestItem, request: Request) -> dict:
    
    '''
    TODO: test 함수는, 우선 request 객체를 사용하지 않는다.
    향후 사용자별 정책등을 고려할때 재검토. 전달 인터페이스만 제공한다.
    '''
    
    from mainapp.pipeline_main_app import PipeLineMainApp

    mainApp:PipeLineMainApp = app.GetState(ApiRouterEx.STATE_KEY_MAINAPP)
    
    dictPipelineMap:dict = mainApp.GetMainAppLinkedPipelineModules()
    
    #시나리오, pipeline 리스트를 여러개 가져온다.
    #호출시 pipeline 전달은 크게 문제가 안되며, pipeline으로 전달되는 filter 자체를 고치는 부분과
    #호출후 결과를 모아서 전달하는 응답이 중요하다.
    
    #가상의 pipelineid를 가져온다고 가정 => pipeline은 유지하고, 안의 로직을 모듈화 하는 방향으로 접근 + 코드 리펙토링
    
    #TODO: 기본 Output : ApiResponseHandler를 사용하는 방안.
    #TODO: api 응답시, 차단 메시지, masked메시지, 이런 값을 전달하는 방안의 검토
    #차단 메시지와, API 포맷의 메시지를 전달하는 방안으로 고려한다.
    #http 헤더도 필요하며, 데이터의 크기문제로 allow 시점에는 전달하지 않는다.
    #helper 모듈의 개발을 검토한다.
    
    #일단, detect secret으로 통일
    strPipelineFilterName:str = "secret_filter"
    
    #일단, 무조건 있다는 가정
    pipeline = dictPipelineMap.get(strPipelineFilterName, None)
    
    strRule:str = modelItem.rule
    strAction:str = modelItem.action
    strPrompt:str = modelItem.prompt
    
    #일단 테스트
    dictOutputResponse = {}
    await pipeline.testRule(strPrompt, strRule, strAction, dictOutputResponse, request = request)
    
    return GetApiOutResponse("rule filter test", "filter_result", dictOutputResponse)


#응답 데이터 반환, 우선 이 안에서 공통화
def GetApiOutResponse(strApiCommandCode:str, strResponseKey:str, dictOutResponse:dict) -> dict:
    
    '''
    '''
    
    apiResponseHandler:ApiResponseHandlerEX = ApiResponseHandlerEX()
    
    # 기본상태코드, 성공으로 할당.
    apiResponseHandler.attachSuccessCode()    
    apiResponseHandler.attachApiCommandCode(strApiCommandCode)
    
    apiResponseHandler.attachResponse(strResponseKey, dictOutResponse)
    
    return apiResponseHandler.outResponse()
    
    
    
    
    
    