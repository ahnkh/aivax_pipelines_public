
from lib_include import *

'''
AI 서비스등 사용자 계정 데이터 관리
구조상 스레드 와 Queue가 존재할수 있다.
TODO: Db에 저장하는 기능을 백그라운드로 수행하며, DB I/O를 최소화 해야 한다.
global 데이터 관리는 pipelineMainApp에서 수행한다. (중간 규모의 관리)
'''

class UserAccountDataHandler:
    
    def __init__(self):
        
        #최초에 가지고 있고, 저장할 데이터
        self.__dictCurrentUserInfo:dict = {}
        
        #받아올 데이터, insert 시점에 저장된 계정정보와 다른것만 추가
        self.__dictNewUserInfo:dict = {}
        pass
    
    #계정 정보의 추가
    def AddData(self, strUserID:str, dictUserInfo:dict):
        
        '''
        계정정보, id, email, ai 서비스 유형등 계속 확장 가능
        dictionary로 감싸서 전달 받는다.
        이건 생산자/소비가 Queue를 고려한다. => insert 성능은 조금 떨어뜨리고, dictionary, 중복 제거
        '''
        
        self.__dictNewUserInfo[strUserID] = dictUserInfo
        
        return ERR_OK
    
    # 사용자 계정관리, 초기화
    def Initialize(self, dictUserAccountDataLocalConfig:dict):
        
        '''
        '''
        
        LOG().info("initialize user account handler")
        
        #별도의 스레드를 호출한다. 계정 정보는, mainapp 외 백그라운드로 수집된다.
        
        #스레드 호출, 가급적 인스턴스를 전역으로 관리
        thread = threading.Thread(name="user account data thread", target=self.ThreadHandlerProc, daemon=True, args=(dictUserAccountDataLocalConfig,))
        thread.start()
        
        return ERR_OK
    
    
    # 스레드 생성.
    def ThreadHandlerProc(self, dictUserAccountDataLocalConfig):

        '''
        데몬이 종료될때까지는 계속 수행된다. 
        TODO: 스레드가 종료되었으면, 재기동등 예외처리 로직을 추가한다.
        TODO: try/catch 예외처리 필수.
        '''
        
        # nMaxWaitTimeout:int = LogWriteHandler.MAX_WAIT_TIME_OUT
        # nThreadSleep:int = 1
        
        # 최초 기동후 조회, 데이터 구조를 생성한다. 과거 데이터를 가지고 있는다.
        self.__readUserInfoFromDB()
        
        thread_handler:dict = dictUserAccountDataLocalConfig.get("thread_handler")
        
        #스레드 sleep, 기본 15초로 지정
        sleep:int = thread_handler.get("sleep")

        while True:
            
            #에러 발생시 무한 loop, try로 예외 처리
            try:
                
                self.__doInsertUserAccount()
                
                # 정상적인 케이스의 sleep
                time.sleep(sleep)
                
            except Exception as err:         
                LOG().error(traceback.format_exc())
                
                # 오류 발생시의 대기 (잡아야 하는 오류이나, 운영시 문제가 되기에 sleep으로 대기)
                time.sleep(sleep)
                
            
            # 처음부터 queue를 dictionary로 고려.

            #Queue에 쌓인 데이터, 1번에 처리한다. 많이 쌓이면 곤란하므로 10초정도를 고려 (앞단에서 체크가 필요할수도.)
            #데이터 구조는 다시 고려
            time.sleep(sleep) #시작후 대기한다. (바로 저장이 되지는 않을 것으로 예상)

        #TOOD: 호출될 수 없는 구문.
        return ERR_OK
    
    
    ########################################### private
    
    # 사용자 계정의 등록 (계정의 경우 중복 데이터가 많고, 실제 추가되는 계정은 100개 남짓으로 예상된다)
    def __doInsertUserAccount(self, ):
        
        '''
        '''
        
        return ERR_OK
    
    #DB에서 계정 정보를 가져온다.
    def __readUserInfoFromDB(self, ):
        
        '''
        '''
        
        dictDBResult = {}
        sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_ai_user_account", {"where" : "", "limit":1}, dictDBResult)
        
        self.__dictCurrentUserInfo
        
        return ERR_OK