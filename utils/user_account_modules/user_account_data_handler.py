# import copy
import threading

from lib_include import *

from type_hint import *

from utils.user_account_modules.uuid_manage_map import UUIDManageMap

'''
AI 서비스등 사용자 계정 데이터 관리
구조상 스레드 와 Queue가 존재할수 있다.
TODO: Db에 저장하는 기능을 백그라운드로 수행하며, DB I/O를 최소화 해야 한다.
global 데이터 관리는 pipelineMainApp에서 수행한다. (중간 규모의 관리)

TODO: RDB에 추가시 uuid를 추가해야 한다.
'''

class UserAccountDataHandler:
    
    USER_QUEUE_LIMIT = 500 #Queue 제한값 지정, 최대 500명이 쌓여있으면 장애이다.
    USER_QUEUE_DELETE_COUNT = 100
    
    def __init__(self):
        
        #최초에 가지고 있고, 저장할 데이터
        self.__dictCurrentUserInfo:dict = {}
        
        #uuid기반의 계정정보, filter를 사용시 id를 찾기 위한 용도로 미리 생성
        #TODO: 이름으로 찾으면, 매칭이 안된다. 엔진에서 id,email이 넘어온다는 가정하에 사용 => 기존 dictCurrentUserInfo로 사용 가능.
        # self.__dictUUIDHashUserNameInfo:dict = {}
        
        self.__lock = threading.Lock()
        
        #받아올 데이터, insert 시점에 저장된 계정정보와 다른것만 추가
        self.__dictNewUserInfo:dict = {}
        
        #uuid 관리 map
        self.__uuidMap:UUIDManageMap = None
        pass
    
    #계정 정보의 추가
    def AddData(self, strUserKey:str, dictUserInfo:dict):
        
        '''
        계정정보, id, email, ai 서비스 유형등 계속 확장 가능
        dictionary로 감싸서 전달 받는다.
        이건 생산자/소비가 Queue를 고려한다. => insert 성능은 조금 떨어뜨리고, dictionary, 중복 제거
        
        InfoKey = user_email + "_" + service id 조합 (향후 변경 가능)
        user:dict = {
            ApiParameterDefine.NAME : modelItem.user_id,
            ApiParameterDefine.EMAIL : modelItem.email,
            ApiParameterDefine.AI_SERVICE : modelItem.ai_service
        }
        '''
        
        #dctionary의 제한값 설정, 제한값 이상으로 쌓이면, 과거 데이터를 삭제후 업데이트 한다.
        #삭제 개수는 random, 일정량을 덜어낸다.
        if UserAccountDataHandler.USER_QUEUE_LIMIT < len(self.__dictNewUserInfo):
            
            #과거 dictionary 삭제, 검증은 필요
            
            LOG().error(f"user queue is full, delete old user queue")
            
            dictNewUserInfo = self.__dictNewUserInfo
            
            keys = list(dictNewUserInfo.keys())[UserAccountDataHandler.USER_QUEUE_DELETE_COUNT:]
            dictErasedUserInfo = {k: dictNewUserInfo[k] for k in keys}

            dictNewUserInfo.clear()
            dictNewUserInfo.update(dictErasedUserInfo)
        
        self.__dictNewUserInfo[strUserKey] = dictUserInfo
        
        return ERR_OK
    
    #계정정보, 최신 데이터 전달후 pipeline에서 참조
    
    
    #UUID를 발급한다.
    def GenerateUUID(self, strUserKey:str)-> str:
        
        '''
        '''
        
        return self.__uuidMap.GenerateNewUUID(strUserKey)
    
    # 사용자 계정관리, 초기화
    def Initialize(self, dictUserAccountDataLocalConfig:dict):
        
        '''
        '''
        
        LOG().info("initialize user account handler")
        
        self.__uuidMap:UUIDManageMap = UUIDManageMap()
        self.__uuidMap.Initialize()
        
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
                    
                    #if 문, 불필요한 insert 자제.
                    if 0 < len(self.__dictNewUserInfo):
                        self.__doInsertUserAccount()
                
                # 정상적인 케이스의 sleep, lock 구문의 밖에서 sleep, lock이 오래 잡히는 것을 방지한다.
                time.sleep(sleep) 
                
            except Exception as err:         
                LOG().error(traceback.format_exc())
                
                # 오류 발생시의 대기 (잡아야 하는 오류이나, 운영시 문제가 되기에 sleep으로 대기)
                time.sleep(sleep)

        #TOOD: 호출될 수 없는 구문.
        # return ERR_OK
    
    ########################################### private
    
    # 사용자 계정의 등록 (계정의 경우 중복 데이터가 많고, 실제 추가되는 계정은 100개 남짓으로 예상된다)
    def __doInsertUserAccount(self, ):
        
        '''
        신규로 등록된 계정, 과거 계정과 비교하여
        존재하지 않는 신규 계정이면 db에 업데이트
        db가 많지 않을듯 하여, 단건으로 insert 한다.
        '''
        
        #수집 시간 공통으로 사용, 모두 같은 시간에 등록한다. => MariaDB의 Trigger를 활용한다.
        # strRegDate:str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        for strUserKey in self.__dictNewUserInfo.keys():
            
            dictExistUserAccount:dict = self.__dictCurrentUserInfo.get(strUserKey)
            
            # 없는 계정 정보이면, Db에 추가 (bulk는 고려하지 않는다.)
            if None == dictExistUserAccount:
                
                LOG().info(f"new user account exist, insert {strUserKey}")
                
                dictNewUserAccount:dict = self.__dictNewUserInfo.get(strUserKey)      
                                                    
                nError = self.__insertNewUserAccount(dictNewUserAccount)
                
                #TODO: 수집후 오류가 발생하면 exception이 발생한다. 오류가 없으면, 원본에도 저장한다.
                #TODO: 키만 존재하면 되고, 실제 데이터는 기존과 동일하지 않아도 무방하기는 하다.
                
                #TODO: 오류 발생시, 무한 반복되는 문제, N개 이상이면 초기화 되는 로직이 필요하다. => 날짜? 
                #아니면, 여기서는 실패시 버리는 로직도 고려한다. => 1차적으로 개수 제한, dictionary의 개수가 제한값 이상이면, 과거 데이터 삭제.
                if ERR_OK == nError:
                    self.__dictCurrentUserInfo[strUserKey] = dictNewUserAccount
                                        
                #pass
                
            # pass
            
            #TODO: 계정 정보는 I/O가 크지 않다. 그냥 insert
            #활동시간, 다시 업데이트를 해야 하며, 별도 모듈에서 관리, 이건 update 구문.
            #별도의 스레드로 관리해야 할 수도 있다. (대기 시간이 길어서, 스레드 쪽이 유리)
            #우선 최대 제한 시간을 신규 계정과 등록과 맞춰서 하나의 스레드와 동일하게 유지
            
        #저장후, 신규 수집한 map을 초기화 한다. (lock 필요, 호출시점에 lock이 걸린 상태이다.)
        self.__dictNewUserInfo.clear()
        
        return ERR_OK
    
    #DB에서 계정 정보를 가져온다.
    def __readUserInfoFromDB(self, ):
        
        '''
        '''
        
        '''
        SELECT 
            CONCAT(email, '_', ai_service_id) AS user_key, id, email, user_group_id, ai_service_id, created_at, updated_at
        FROM app.users {where} limit {limit}
        '''
        dictDBResult = {}
        sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_ai_user_account", {"where" : ""}, dictDBResult)
        
        lstQueryData:list = dictDBResult.get(DBSQLDefine.QUERY_DATA)
        
        #같은 구조를 유지, 그대로 복사한다. (혹시 몰라 deep copy)  
        #TODO: 키구조 주의, 그대로 넣으면 안된다. 사양 파악후 정리.      
        # 키를 생성해서, 다시 만든다. => DB에서 만든다. 그래도, 바로 복사는 안된다.
        # self.__dictCurrentUserInfo:dict = copy.deepcopy(dictQueryData)
        
        #일단 하나로 처리하자.
        for dictUserInfo in lstQueryData:
            
            user_key:str = dictUserInfo.get("user_key")
            
            #나머지는 그대로 저장            
            self.__dictCurrentUserInfo[user_key] = dictUserInfo
            
        #수집데이터, 다시 넘겨서 uuid를 업데이트 한다.
        self.__uuidMap.UpdateUUIDFromDB(lstQueryData)
        
        return ERR_OK
    
    #DB로 신규 계정을 추가한다.
    def __insertNewUserAccount(self, dictNewUserAccount:dict):
        
        '''
        이름은  아래 항목으로 통일, 등록시간은 맞춘다. => 15초 단위인데.. 일단 불필요한 부분에 자원 낭비를 없애는 차원.
        dictNewUserAccount = {            
        }
        
        '''        
        
        #TODO: id는 uuid로 생성한다. 좀더 고려 필요.
        uuid:str = dictNewUserAccount.get(ApiParameterDefine.UUID)
        # user_id:str = dictNewUserAccount.get("user_id")
        email:str = dictNewUserAccount.get(ApiParameterDefine.EMAIL)
        
        ai_service:int = dictNewUserAccount.get(ApiParameterDefine.AI_SERVICE)
        
        #ai server 명, 문자로 변환하여 저장 => 숫자를 직접 추가한다.
        # strAIServerName:str = AI_SERVICE_NAME_MAP.get(ai_service, "")
        
        # TIODO: comment는 우선 공백, 수집 하지 않는다.
        # etc_comment:str = dictNewUserAccount.get("etc_comment")\
        # etc_comment:str = ""
        # use_flag:int = CONFIG_OPT_ENABLE #사용여부, 수집되지 않는다. 기본 활성
        
        dictDBInfo = {
            "uuid" : uuid,
            # "reg_date" : datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            # "reg_date" : strRegDate, #TODO: 최초 등록 시점에는, DB를 활용하자.
            "email" : email,
            "ai_service" : ai_service, #순차적으로 GPT, claude, gemini, copilot, ..
            # "etc_comment" : "", #comment
            # "use_flag" : use_flag, #1:활성, 0:비활성
        }
        
        dictDBResult:dict = {}
        sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_insert_update_ai_user_account", dictDBInfo, dictDBResult)
        
        return ERR_OK
        
        # pass
    