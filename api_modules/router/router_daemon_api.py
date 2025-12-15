
# router_user.py
from fastapi import APIRouter

#동기, 비동기 선택 옵션 추가 (향후를 위해서 구현)
import inspect

from lib_include import *

from type_hint import *

#TODO: 매번 호출해야 하는 문제. 이건 static으로 하자...
# from api_modules.helper.router_custom_helper import RouterCustomHelper

from api_modules.router.sub_modules.api_router_impl_command import ApiRouterImplCommand

app = ApiRouterEx(
    # prefix="/",
    # tags=["pipline"],
)

command = ApiRouterImplCommand()

#신규 추가, 다중 필터 TODO: 기본 구성은 inlet을 참고한다.
@app.post("/v1/filter/multiple_filter")
async def filter_prompt_from_engine(modelItem: VariantFilterForm, request: Request):
    
    '''
    기존 inlet의 확장, 필터를 직접 지정하며, 다수의 필터를 호출하는 기능을 제공한다.
    
    **filter_list** : 차단 필터 리스트    
    - _secret_filter_ : <u>API 차단 필터</u>
    
    - llm_filter : AI 필터 (신규 llm/slm 필터로 변경)
    - input_filter : opensearch 저장 (프롬프트)
    
    - file_block_filter : file에 대한 필터 기능 제공
    
    - <del>output_filter : opensearch 저장 (LLM 응답, 별도 API로 개발)</del>
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
            "secret_filter",
            "input_filter",
            "file_block_filter"
        ],
        "prompt": "내 API key는 API_key=sk-1234567-0000-abdcdef 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요",
        "user_id": "ghahn",
        "email": "ghahn@wins21.co.kr",
        "ai_service":1,
        "client_host":"127.0.0.1",
        "session_id":"",
        "attach_files": [
            "/home1/aivax/data_resource/attach_file/sample.docx"
        ]
        
    }'    
    ```
    
    응답 메시지 참고 사항
    - action: 0: allow, 1: block, 2: masking
    '''
    
    return await doRouterFunction("doFilterApiRouter", modelItem, request)

#output filter에 대한 api 제공
@app.post("/v1/filter/output_filter")
async def write_ouput_response(modelItem: OutputFilterItem, request: Request):
    
    '''
    '''
        
    return await doRouterFunction("doOutputApiRouter", modelItem, request)
    
    
#정책 추가 인터페이스, 정책에 대한 테스트와 응답 결과만 반환한다.
@app.post("/v1/filter/testrule")
async def testFilterRule(modelItem: FilterRuleTestItem, request: Request) -> dict:
    '''
    '''
    
    return await doRouterFunction("doTestFilterRule", modelItem, request)

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
    
    byteRawBody = await request.body()
        
    dictItemModel = {}
    JsonHelper.LoadToDictionary(byteRawBody, dictItemModel)
    
    return await doRouterFunction("doLogApiRouter", dictItemModel)
    
    # try:
    
    #     byteRawBody = await request.body()
        
    #     dictItemModel = {}
    #     JsonHelper.LoadToDictionary(byteRawBody, dictItemModel)

    #     return await doLogApiRouter(dictItemModel)

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
   
    
# filter api 모듈 본체, TODO: 내부 함수가 async로 구현되어 있어, async로 선언될수 밖에 없는 구조이다.    
async def doFilterApiRouter(modelItem: VariantFilterForm, request: Request) -> dict:
    
    '''
    '''
    
    #TODO: 기존 함수는 그대로 유지하고, 신규 API를 추가한다.
    #TODO: pipeline과 pipeline_module이 같은 개념같다. 다시 확인 필요.
    #TODO: 이시점에 업데이트가 안되어 있다. 방안 => mainApp를 활용.
    # dictPipelineMap:dict = app.GetState(ApiRouterEx.STATE_KEY_PIPELINE_MAP)
    
    #순환참조 주의 => 향후 리펙토링시 구조 변경
    # from mainapp.pipeline_main_app import PipeLineMainApp

    # mainApp:PipeLineMainApp = app.GetState(ApiRouterEx.STATE_KEY_MAINAPP)
    mainApp:Any = app.GetState(ApiRouterEx.STATE_KEY_MAINAPP)
    
    return await command.doFilterApiRouter(mainApp, modelItem, request)

#output 응답 전달 본체
async def doOutputApiRouter(modelItem: OutputFilterItem, request: Request) -> dict:
    
    #mainApp 연결은 필요할 것으로 예상
    mainApp:Any = app.GetState(ApiRouterEx.STATE_KEY_MAINAPP)
    
    return await command.doOutputApiRouter(mainApp, modelItem, request)
    

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
    
    
    
    
    
    