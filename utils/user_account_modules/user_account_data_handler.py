import copy
import threading

from lib_include import *

# from type_hint import *

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
        
        self.__lock = threading.Lock()
        
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
                
                # 처음부터 queue를 dictionary로 고려.
                with self.__lock:
                    self.__doInsertUserAccount()
                
                # 정상적인 케이스의 sleep, lock 구문의 밖에서 sleep, lock이 오래 잡히는 것을 방지한다.
                time.sleep(sleep) 
                
            except Exception as err:         
                LOG().error(traceback.format_exc())
                
                # 오류 발생시의 대기 (잡아야 하는 오류이나, 운영시 문제가 되기에 sleep으로 대기)
                time.sleep(sleep)

        #TOOD: 호출될 수 없는 구문.
        return ERR_OK
    
    
    ########################################### private
    
    # 사용자 계정의 등록 (계정의 경우 중복 데이터가 많고, 실제 추가되는 계정은 100개 남짓으로 예상된다)
    def __doInsertUserAccount(self, ):
        
        '''
        신규로 등록된 계정, 과거 계정과 비교하여
        존재하지 않는 신규 계정이면 db에 업데이트
        db가 많지 않을듯 하여, 단건으로 insert 한다.
        '''
        
        #수집 시간 공통으로 사용, 모두 같은 시간에 등록한다.
        strRegDate:str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        for strUserID in self.__dictNewUserInfo.keys():
            
            dictExistUserAccount:dict = self.__dictCurrentUserInfo.get(strUserID)
            
            # 없는 계정 정보이면, Db에 추가 (bulk는 고려하지 않는다.)
            if None == dictExistUserAccount:
                
                LOG().info(f"new user account exist, insert {strUserID}")
                
                dictNewUserAccount:dict = self.__dictNewUserInfo.get(strUserID)      
                          
                self.__insertNewUserAccount(dictNewUserAccount, strRegDate)
                
            # pass
        
        return ERR_OK
    
    #DB에서 계정 정보를 가져온다.
    def __readUserInfoFromDB(self, ):
        
        '''
        '''
        
        dictDBResult = {}
        sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_ai_user_account", {"where" : "", "limit":1}, dictDBResult)
        
        dictQueryData:dict = dictDBResult.get(DBSQLDefine.QUERY_DATA)
        
        #같은 구조를 유지, 그대로 복사한다. (혹시 몰라 deep copy)        
        self.__dictCurrentUserInfo:dict = copy.deepcopy(dictQueryData)
        
        return ERR_OK
    
    #DB로 신규 계정을 추가한다.
    def __insertNewUserAccount(self, dictNewUserAccount:dict, strRegDate:str):
        
        '''
        이름은  아래 항목으로 통일, 등록시간은 맞춘다. => 15초 단위인데.. 일단 불필요한 부분에 자원 낭비를 없애는 차원.
        dictNewUserAccount = {            
        }
        
        '''        
        
        user_id:str = dictNewUserAccount.get("user_id")
        email:str = dictNewUserAccount.get("email")
        ai_service:int = dictNewUserAccount.get("ai_service")
        # TIODO: comment는 우선 공백, 수집 하지 않는다.
        # etc_comment:str = dictNewUserAccount.get("etc_comment")\
        # etc_comment:str = ""
        use_flag:int = CONFIG_OPT_ENABLE #사용여부, 수집되지 않는다. 기본 활성
        
        dictDBInfo = {
            "user_id" : user_id,
            # "reg_date" : datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reg_date" : strRegDate,
            "email" : email,
            "ai_service" : ai_service, #순차적으로 GPT, claude, gemini, copilot, ..
            "etc_comment" : "", #comment
            "use_flag" : use_flag, #1:활성, 0:비활성
        }
        
        dictDBResult:dict = {}
        sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_insert_update_ai_user_account", dictDBInfo, dictDBResult)
        
        # pass
    