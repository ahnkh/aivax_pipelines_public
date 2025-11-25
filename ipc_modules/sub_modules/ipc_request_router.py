
from lib_include import *

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
        pass
    
    def Initialize(self, mainApp:Any, dictIPCPipelineServerLocalConfig:dict):
        
        '''
        '''
        
        LOG().info("initialize ipc router")
        
        self.__mainAppRef = mainApp
        
        #TOOD: config 관련, 우선 전달 필요성은 향후 검토
        
        return ERR_OK
    
    #요청에 대한 Route, TOOD: 병렬처리, 비동기, 반환값은 향후 고려.
    def RouteRequest(self, dictRequest:dict):
        
        '''
        '''
        
        #결과 데이터는 반환하는 것으로, fast api router, 그대로 구현, ApiResponseHandler
        
        return {}