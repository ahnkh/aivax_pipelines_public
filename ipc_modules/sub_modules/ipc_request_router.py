
#비동기 처리, 대응
import asyncio

from lib_include import *

from type_hint import *


from api_modules.router.api_router_impl_command import ApiRouterImplCommand


#이 시점에는 MainApp 참조
# from mainapp.pipeline_main_app import PipeLineMainApp

'''
IPC 요청에 대해서, 각 처리 모듈로 요청을 route
fastapi와 유사한 포맷으로, URL을 대체 데이터 분기 기능 제공
'''

class IPCRequestRouter:
    
    def __init__(self):
        
        #mainApp, 참조로 복사
        from mainapp.pipeline_main_app import PipeLineMainApp
        self.__mainAppRef:PipeLineMainApp = None
        
        self.__routerCommand:ApiRouterImplCommand = None
        pass
    
    def Initialize(self, mainApp:Any, dictIPCPipelineServerLocalConfig:dict):
        
        '''
        '''
        
        LOG().info("initialize ipc router")
        
        self.__mainAppRef = mainApp
        
        self.__routerCommand:ApiRouterImplCommand = ApiRouterImplCommand()
        
        #TOOD: config 관련, 우선 전달 필요성은 향후 검토
        
        return ERR_OK
    
    #요청에 대한 Route, TOOD: 병렬처리, 비동기, 반환값은 향후 고려.
    # def RouteRequest(self, dictRequest:dict) -> dict:
    def RouteRequest(self, dictRequest:dict) -> dict:
        
        '''
        메시지 수신후의 처리
        - 예외처리를 이 함수에서 담당한다. (오류 발생 포함)
        - router.point 필드가 기존 fastapi의 endpoint 이다.
        - 나머지 값은 fastapi와 동일값을 추가하며, json 레벨을 1차원 depth로, 필드명을 최소화 하여 관리한다.
        - 기존에 사용하는 필드명은 유지하고, 신규 필드를 기준으로 변경한다.
        - sslproxy 엔진은 기존 multiple_filter만 사용하며, router.point 이름은 상수로 관리한다.
        '''
        
        #결과 데이터는 반환하는 것으로, fast api router, 그대로 구현, ApiResponseHandler
        
        try:
            
            router_point:str = dictRequest.get(IPC_ROUTER_DEFINE.REQUEST_ROUTER_POINT)
            
            #TODO: 여기는 reflection X, 분기하자.
            # 짧은 depth, 여기서 분기
            
            if None == router_point:
                #error
                raise #TODO: 예외처리는 2차 고민
            
            if IPC_ROUTER_DEFINE.ROUTER_PIPELINE_FILTER == router_point:                
                return self.__routeFilterRequest(dictRequest)
        
        except Exception as e:
            # logger.error(f"Message processing error: {e}")
            LOG().error(traceback.format_exc())
            
            #TODO: error 메시지 생성 기능 검토.
            return {"error": f"{e}"}


    ###################################################### private
    
    # 요청에 대한 처리, dictionary 반환
    def __routeFilterRequest(self, dictRequest:dict) -> dict:
        
        '''
        기능 최소화, 필요 기능만 전달
        '''
        
        # filter_list:list = dictRequest.get("filter_list")
        
        #이정도 자원은 무시.
        # dictPipelineMap:dict = self.__mainAppRef.GetMainAppLinkedPipelineModules()
                
        # userItem:VariantFilterUserItem = VariantFilterUserItem(
        #     id = dictRequest.get("id"),
        #     email = dictRequest.get("email"),
        #     client_host = dictRequest.get("client_host"),
        #     session_id = dictRequest.get("session_id"),
        # )
        
        modelItem = VariantFilterForm(
            filter_list = dictRequest.get("filter_list"),
            prompt = dictRequest.get("prompt"),
            # user_role = userItem       
            
            user_id = dictRequest.get("id", ""),
            email = dictRequest.get("email", ""),
            client_host = dictRequest.get("client_host", "127.0.0.1"),
            session_id = dictRequest.get("session_id", ""),
            ai_service = dictRequest.get("ai_service", 0),
        )
        
        #TODO: 계정 정보는 mainApp를 통해서 User관리 객체로 전달, MariaDB로 저장한다.
        #TODO: 세션정보를 생성하는 방법도 향후 알아볼것. (sessionid)
        
        return asyncio.run(self.__routerCommand.doFilterApiRouter(self.__mainAppRef, modelItem, None))
        
        
        
        
        
    
    
        