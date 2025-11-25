
import os

#외부 라이브러리
from lib_include import *

from type_hint import *

#helper 추가
from mainapp.helper.pipeline_app_helper import PipelineAppHelper

#환경설정, config등 관리
from mainapp.helper.main_app_env_loader import MainAppEnvLoader

#정책관리 모듈 추가
from block_filter_modules.filter_policy.filter_policy_manager import FilterPolicyManager

#Filter 별 패턴 탐지 기능 관리
from block_filter_modules.filter_pattern.filter_pattern_manager import FilterPatternManager

#TODO: type_hint 개념 적용.
from utils.log_write_modules.log_write_handler import LogWriteHandler

# 계정 데이터 관리 모듈 추가
from utils.user_account_modules.user_account_data_handler import UserAccountDataHandler

#ipc 통신, 서버 추가, main에서 실행하는 것으로 하자. 
#ipc 통신으로 mainapp를 전달한다. (통신과 실행체 분리)
from ipc_modules.ipc_pipeline_server import IPCPipelineServer


from common_modules.global_common_module import GlobalCommonModule

'''
관리 모듈, MainApp 실행
입력 인터페이스, FastApi와는 State로 연결한다.
MainApp가 GlobalResourceManager 기능도 내장한다. (중간정도의 확장성)
'''

class PipeLineMainApp:
    
    def __init__(self):
        
        #설정 config, 기본으로 가지고 있는다.
        self.__dictJsonLocalConfigRoot:dict = None
        
        #log 처리 모듈, Thread
        self.__logWriteHandler:LogWriteHandler = None
        
        #pipeline 정보, 복사
        self.__dictPipelineModulesRef:dict = None
        
        #app helper 추가
        self.__appHelper:PipelineAppHelper = None
        
        #환경 변수 관리
        self.__mainAppEnvLoader:MainAppEnvLoader = None
        
        #정책관리 모듈 추가
        self.__filterPolicyManager:FilterPolicyManager = None
        
        #filter, 패턴 탐지 관리
        self.__filterPatternManager:FilterPatternManager = None
        
        #사용자 계정 데이터 관리
        self.__userAccountDataHandler:UserAccountDataHandler = None
        
        # ipc 통신 서버
        self.__ipcPipelineServer:IPCPipelineServer = None
        pass
    
    
    #초기화 함수, TODO: config는 우선 dictionary로 관리한다.
    def Initialize(self, dictOpt:dict):
        
        '''
        초기 설정, config, 환경 정보 관리
        Logger 설정은 기본 모듈 활용
        부가 모듈의 실행 및 관리, mainapp에서 관리해서, FactApi 모듈로 전달 통로를 제공한다.
        '''
        
        #TODO: LogWriter - 관리 모듈로 관리 또는 LogWriteHandler를 Main으로 사용
        #config 및 초기화는 MainApp 및 Helper에서 관리
        
        #local 설정 config, 기본으로 관리, 우선 하나로 관리
        self.__dictJsonLocalConfigRoot:dict = {}
        
        #이름 변경.
        dictJsonLocalConfigRoot:dict = self.__dictJsonLocalConfigRoot
        
        #환경설정, 가장 먼저 로딩한다., TODO: config 위치는 고정하자.
        self.__mainAppEnvLoader = MainAppEnvLoader()
        self.__mainAppEnvLoader.Initalize(dictOpt, dictJsonLocalConfigRoot)
        
        self.__logWriteHandler:LogWriteHandler = LogWriteHandler()        
        self.__initializeLogWriter(self.__logWriteHandler, dictJsonLocalConfigRoot)
        
        self.__initializeFactoryInstance(dictJsonLocalConfigRoot)
        
        self.__initializeDBModule(dictOpt, dictJsonLocalConfigRoot)
        
        #TODO: 초기화, 환경 설정 관리는 env loader 로 관리
        #TODO: 설정 정보의 관리, 사양 관리 필요.
        # self.__logWriteHandler.Initialize(dictLogWriteHandlerLocalConfig)
        
        self.__appHelper:PipelineAppHelper = PipelineAppHelper()
        
        #filter, 패턴 탐지 관리, TODO: policy manager와 연결되어 있다.
        self.__filterPatternManager:FilterPatternManager = FilterPatternManager()
        self.__filterPatternManager.Initialize(dictJsonLocalConfigRoot)
        
        # 정책 업데이트, TODO: pipeline의 filter 접근은 mainapp를 통해서 접근
        # 정책이 업데이트 되면, filterPatternManager로 정책을 전달한다. (브로드 캐스팅)
        self.__filterPolicyManager:FilterPolicyManager = FilterPolicyManager()
        self.__filterPolicyManager.Initialize(dictJsonLocalConfigRoot, self.__filterPatternManager)
        
        #사용자 계정 데이터 관리
        self.__userAccountDataHandler:UserAccountDataHandler = UserAccountDataHandler()
        self.__initializeUserAccountDataHandler(self.__userAccountDataHandler, dictJsonLocalConfigRoot)
        
        # ipc 통신 서버, mainapp를 전달하는 구조.
        self.__ipcPipelineServer:IPCPipelineServer = IPCPipelineServer()
        mainApp:PipeLineMainApp = self
        
        self.__initializeIPCServer(self.__ipcPipelineServer, mainApp, dictJsonLocalConfigRoot)
        
        return ERR_OK
    
    ######################################## public
    
    #여기서 직접적으로 filter 호출이 되도록, wrapping 하고, 
    #fastapi의 모듈이 여러개를 호출할수 있도록 한다. 
    #mainapp에 호출되는 함수는 동기식으로 제공하고, 호출측에서 async, await 처리를 한다.
    #가능한 모듈단위로 분리한다.
    
    #pipeline 상태의 공유 변수 추가, 내부에서 pipeline으로 (자동으로) 상태 변경 (정책등)하는 기능도 필요하다.
    #TODO: PipelineModule의 초기화 시점 애매, 모듈 분리
    
    def AttachPipelineModules(self, dictPipelineModules:dict):
        '''
        '''
        #연결은 여기서
        self.__dictPipelineModulesRef = dictPipelineModules
        return ERR_OK
        
    #pipeline module, 접근시 MainApp를 연결하여 반환
    def GetMainAppLinkedPipelineModules(self) -> dict:
        
        '''
        변경된 참조의 복사, TODO: PIPELINE_MODULES가 변경되면, mainapp로 전달을 위해서
        이 함수가 호출이 되어야 한다.
        '''
        
        mainApp = self
        
        #각 pipeline 모듈마다, 연결한다. helper로 분리
        self.__appHelper.LinkPipelineModules(mainApp, self.__dictPipelineModulesRef)
        return self.__dictPipelineModulesRef
        
    #LogHandler, OpenSearch Log 로그 데이터를 추가한다.
    def AddLogData(self, strDataType:str, dictOuptut:dict):
        
        '''
        TODO: MainApp는 인터페이스를 제공하고, 실제 구현은 Helper 또는 관리 모듈에서 수행한다.
        AppHelper는 최대한 상태를 가지지 않는 패턴
        '''
        
        self.__appHelper.AddLogData(self.__logWriteHandler, strDataType, dictOuptut)        
        return ERR_OK
    
    #사용자 계정의 추가, TODO: 사이즈가 커지면, 한단계 더 모듈 관리자를 추가한다. (항상 동작해야 하는 기능으로, 직접 호출 구조를 선택한다)
    def AddUserAccount(self, ):
        
        '''
        TODO: 구조상 스레드, 백그라운드 I/O
        '''
        
        return ERR_OK
    
    #패턴 모듈, 중계 기능만 제공하고, 실제 구현은 하지 않는다.
    def GetFilterPatternModule(self, strFilterPatternKey:str) -> Any:
        
        '''
        filter내 helper를 반환한다. 오류는 exception으로 처리
        객체의 식별은 호출측에서 import 구문으로 처리
        '''
        
        return self.__filterPatternManager.GetFilterPattern(strFilterPatternKey)
    
    
    #테스트 함수, 추가
    def Test(self, dictTestOpt:dict):
        
        pass
        
        
    ########################################## private
    
    #log writer 초기화
    def __initializeLogWriter(self, logWriteHandler:LogWriteHandler, dictJsonLocalConfigRoot:dict):
        
        '''
        json 설정에 대한 초기화
        TODO: 기타 상수 config는, local define으로 정의한다.
        '''
        
        log_write_module:dict = dictJsonLocalConfigRoot.get("log_write_module")
        
        logWriteHandler.Initialize(log_write_module)        
        return ERR_OK
        
    #Factory 모듈, Db 모듈 추가
    def __initializeFactoryInstance(self, dictJsonLocalConfigRoot:dict):
        
        '''        
        '''
        
        LOG().debug("initialize factory instance")
        
        # nErrInitializeInstanceFactory = GlobalInstanceFactory.createFactoryInstance(self.__dictJsonLocalConfigRoot)
        GlobalInstanceFactory.createFactoryInstance(dictJsonLocalConfigRoot)
        
        return ERR_OK
    
    # RDB 모듈 초기화    
    def __initializeDBModule(self, dictOpt:dict, dictJsonLocalConfigRoot:dict) -> int:

        '''
        DB 생성 모듈의 초기화, SQLMap, DBHelper, SQLInterface를 관리하는 모듈의 생성
        TODO: SQLMap 모듈도, 해당 Instance에서 생성하도록 이관.
        '''
        
        sqlClientInterface:SQLClientInterface = GlobalCommonModule.SingletonFactoryInstance(FactoryInstanceDefine.CLASS_SQL_CLIENT_INTERFACE)
        
        #모듈 초기화, 실패시 exception
        sqlClientInterface.Initialize(dictOpt, dictJsonLocalConfigRoot)

        return ERR_OK
    
    # 사용자 계정 정보 저장 관리 모듈 추가
    def __initializeUserAccountDataHandler(self, userAccountDataHandler:UserAccountDataHandler, dictJsonLocalConfigRoot:dict):
        
        '''
        '''
        
        #TODO: 초기 설정값, config 필요
        
        user_account_data_module:dict = dictJsonLocalConfigRoot.get("user_account_data_module")
        
        userAccountDataHandler.Initialize(user_account_data_module)
        
        return ERR_OK
    
    # ipc 통신 서버 실행, 초기화
    def __initializeIPCServer(self, ipcPipelineServer:IPCPipelineServer, mainApp:Any, dictJsonLocalConfigRoot:dict):
        
        '''
        별도의 IPC 서버와, 통신후 mainapp를 통해서 Filter, 계정관리 모듈등으로 데이터를 전달한다.
        '''
        
        ipcPipelineServer.Initialize(mainApp, dictJsonLocalConfigRoot)
        
        return ERR_OK
       
    