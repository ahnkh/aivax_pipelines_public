
import threading

#외부 라이브러리
from lib_include import *

from type_hint import *

#공통 모듈, TODO: 개발 검토중, http 요청, 기타 공용 모듈

from block_filter_modules.filter_pattern.filter_pattern_manager import FilterPatternManager

#http 요청
from block_filter_modules.filter_policy.helper.filter_dbpolicy_request_helper import FilterDBPolicyRequestHelper

'''
차단등 정책 관리 모듈, db를 조회하여 dictionary등 
활용이 편한 정보로 정규화 한다.
'''

class FilterPolicyManager:
    
    def __init__(self):        
        pass
    
    #초기화, db에 대한 조회 모듈이 필요하다. (웹서비스, http_request 활용 여부)
    def Initialize(self, dictJsonLocalConfigRoot:dict, filterPatternManager:FilterPatternManager):
        
        '''
        간단한 조회, 최소화된 모듈로 검토
        접속 정보, 서버, db이든 필요하다. 로컬 db 또는 config 정보가 필요
        우선 json config에 위임한다.
        
        기본 스레드, 초기화 로직 존재
        정책의 갱신과 정책의 조회는 각각 동작한다. 주기가 길어서, 스레드 동기화까지는 고려할 필요가 없다.
        '''
        
        # self.__detectSecretPolicy = DetectSecretPolicy()
        
        # self.__filterDBPolicyRequestHelper:FilterDBPolicyRequestHelper = FilterDBPolicyRequestHelper()
        
        #db 기본 정보, config에서 받아온다. 향후 로직이 만들어지면, 그때 처리
        #config에서 필요한 정보, dctionary로 수집후 스레드로 전달, 상수 주의, config가 없는 경우의 대비, 기본 값도 상수로 정의 http://127.0.0.1:3000
        dictPolicyLocalConfig = {
            LOCAL_CONFIG_DEFINE.KEY_DB_SERVER_DEFAULT_IP : LOCAL_CONFIG_DEFINE.VAL_DB_SERVER_DEFAULT_IP,
            LOCAL_CONFIG_DEFINE.KEY_DB_SERVER_DEFAULT_PORT : LOCAL_CONFIG_DEFINE.VAL_DB_SERVER_DEFAULT_PORT,
            LOCAL_CONFIG_DEFINE.KEY_DB_SERVER_DEFAULT_SCHEME : LOCAL_CONFIG_DEFINE.VAL_DB_SERVER_DEFAULT_SCHEME,
            
            LOCAL_CONFIG_DEFINE.KEY_DB_POLL_CYCLE_SECOND : LOCAL_CONFIG_DEFINE.VAL_DB_POLL_CYCLE_SECOND,
        }
        
        self.__initializeLocalPolicyConfig(dictJsonLocalConfigRoot, dictPolicyLocalConfig)
        
        LOG().info(f"initialize thread, initial value = {dictPolicyLocalConfig}")
        
        thread = threading.Thread(name="db policy thread", target=self.ThreadHandlerProc, daemon=True, args=(dictPolicyLocalConfig, filterPatternManager))
        thread.start()
        
        return ERR_OK
    
    def ThreadHandlerProc(self, dictPolicyLocalConfig:dict, filterPatternManager:FilterPatternManager):
        
        '''
        기본 스레드 인터페이스만 개발
        '''
        
        #주기적으로 기동한다, 우선 주기는 1분으로 지정한다.
        nThreadCycle:int = dictPolicyLocalConfig.get(LOCAL_CONFIG_DEFINE.KEY_DB_POLL_CYCLE_SECOND, LOCAL_CONFIG_DEFINE.VAL_DB_POLL_CYCLE_SECOND)
        # nThreadCycle:int = 5 #시연용 임시 테스트, 제외
        
        filterDBPolicyRequestHelper:FilterDBPolicyRequestHelper = FilterDBPolicyRequestHelper()
        
        # 정책 데이터, 유지후 계속 보유한다.
        from block_filter_modules.filter_policy.groupfilter.filter_policy_group_data import FilterPolicyGroupData
        
        #TODO: 순환 참조 이슈, 번거로워도 개별로 저장한다.
        filterPolicyGroupData:FilterPolicyGroupData = FilterPolicyGroupData()
        filterPolicyGroupData.Initialize()
        
        while True:
            
            try:
                
                
                # 최초 단순 조회
                # filterDBPolicyRequestHelper.RequestToDBPolicy(dictFilterPolicy, dictPolicyLocalConfig)
                
                # 2차, 그룹별 2 depth조회
                filterDBPolicyRequestHelper.RequestFilterDBPolicyGroup(filterPolicyGroupData)
                
                # 파일 차단정보, 이것도 전달 필요
                dictFileBlockPolicy:dict = {}
                filterDBPolicyRequestHelper.RequestFileBlockPolicy(dictFileBlockPolicy)
                
                #정책의 가공이 필요하면, 이시점에서 가공 (미구현 상태에서 인수인계)
                # self.__generateFilterPolicy()
                
                #패턴 관리자로 업데이트된 정책을 전달, 2차 filterPolicyGroupData 전달 구조로 변경
                # filterPatternManager.notifyDBPolicyUpdateSignal(dictFilterPolicy)
                filterPatternManager.notifyDBPolicyUpdateSignal(filterPolicyGroupData)
                
                #복사 없이, notify
                filterPatternManager.notifyCustomUpdateFileBlockInfo(dictFileBlockPolicy)
                
                time.sleep(nThreadCycle)
                
            except Exception as err:         
                LOG().error(traceback.format_exc())
                
                #TODO: 예외가 발생하면 무한 대기가 된다. 
                #개발자 레벨에서 확인할 오류는 확인하되, sleep을 주고, 문제 발생시 어쨌든 계속 시도는 해야 한다.
                time.sleep(nThreadCycle)
                
        
        return ERR_OK
    
    
    ################################################# private
    
    #local 정책, config에서 가져온다. TODO: 이름이 모호하다. 우선 개발.
    def __initializeLocalPolicyConfig(self, dictJsonLocalConfigRoot:dict, dictPolicyLocalConfig:dict):
        '''
        다음 구조를 읽는다. config.json을 참고, 주석은 최신화 불필요
        예외처리는 하지 않는다. 문제 발생시 예외 발생, 종료
        "global_env":
        {
            "db_api_server":
            {
                "default_server_ip" : "127.0.0.1",
                "default_server_port" : 3000,
                "default_schema" : "http"
            }
        },
        '''
        
        global_env:dict = dictJsonLocalConfigRoot.get("global_env")
        
        db_api_server:dict = global_env.get("db_api_server")
                
        strDBServerIP:str = db_api_server.get(LOCAL_CONFIG_DEFINE.KEY_DB_SERVER_DEFAULT_IP, LOCAL_CONFIG_DEFINE.VAL_DB_SERVER_DEFAULT_IP)
        nDBServerPort:int = int(db_api_server.get(LOCAL_CONFIG_DEFINE.KEY_DB_SERVER_DEFAULT_PORT, LOCAL_CONFIG_DEFINE.VAL_DB_SERVER_DEFAULT_PORT))
        strDBServerScheme:str = db_api_server.get(LOCAL_CONFIG_DEFINE.KEY_DB_SERVER_DEFAULT_SCHEME, LOCAL_CONFIG_DEFINE.VAL_DB_SERVER_DEFAULT_SCHEME)
        
        #입력값 예외처리는 나중, 우선 설정값을 우선으로 업데이트 한다.
        dictPolicyLocalConfig[LOCAL_CONFIG_DEFINE.KEY_DB_SERVER_DEFAULT_IP] = strDBServerIP
        dictPolicyLocalConfig[LOCAL_CONFIG_DEFINE.KEY_DB_SERVER_DEFAULT_PORT] = nDBServerPort
        dictPolicyLocalConfig[LOCAL_CONFIG_DEFINE.KEY_DB_SERVER_DEFAULT_SCHEME] = strDBServerScheme
        
        return ERR_OK
    
    # #Filter 정책의 생성, 각 Policy 관리 모듈을 가진다. 향후 고려
    # def __generateFilterPolicy(self, ):
        
    #     '''
    #     HTTP 요청으로 DB에서 데이터를 가져온다.
    #     가져온 데이터를 각 policy로 이관한다. 이때 구분 기준이 필요하다.
    #     우선, 한화 시스템 기준으로 detect secret policy를 할당하자.
    #     개별 모듈별 관리의 기준은 개별 정책 모듈에서 관리한다.
    #     '''
        
    #     # self.__detectSecretPolicy.GeneratePolicy()
        
    #     return ERR_OK