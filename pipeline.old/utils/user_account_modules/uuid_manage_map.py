
import uuid

from lib_include import *

from type_hint import *

'''
사용자 계정, UUID 관리
키별 UUID를 관리하고, UUID를 생성한다.
import uuid
str(uuid.uuid4())

'''

class UUIDManageMap:
    
    def __init__(self):
        
        #uuid 관리 Map, DB에서 ID로 가져온다. (TODO: 향후 게정의 수집이 되면 이름 변경 필요)
        self.__dictUUID:dict = None
        
        # pass
        
    # 초기화
    def Initialize(self, ):
        
        '''
        
        '''
        
        self.__dictUUID:dict = {}
        
        return ERR_OK
    
    # DB에서 UUID 정보 갱신
    def UpdateUUIDFromDB(self, listDBUserInfo:list):
        '''
        최초 계정관리 정보를 수집하면, 해당 id를 토대로 uuid를 완성한다.
        상황에 따라, 주기적으로 업데이트 할수 있다.
        '''
        
        #TODO: 기존 uuid는 유지하고, DB에서 교체만 하는, merge기능도 고려
        #최초 개발은 기존 uuid를 모두 삭제후 다시 업데이트 한다.
        self.__dictUUID.clear()
        
        for dictUserInfo in listDBUserInfo:
            
            user_key:str = dictUserInfo.get("user_key")
            
            uuid:str = dictUserInfo.get("id")
            
            #나머지는 그대로 저장            
            self.__dictUUID[user_key] = uuid
            
            #테스트 로그 
            LOG().debug(f"update user uuid from db, key = {user_key}, uuid = {uuid}")
            # pass
        
        return ERR_OK
    
    # 생성과 제공을 같이 만든다. 사용 구조상 생성후 바로 저장이 되는 구조여야 한다.
    def GenerateNewUUID(self, strUserKey:str) -> str:
        
        '''
        기존에 없는 userKey이면 uuid를 생성한다.
        uuid 생성, uuid는 uuid4를 사용
        
        생성된 uuid를 반환한다.
        '''
        
        strUUID:str = self.__dictUUID.get(strUserKey)
        
        if None == strUUID:
            
            strUUID = str(uuid.uuid4())
            
            LOG().info(f"generate new uuid, key = {strUserKey}, uuid = {strUUID}")
            
            #TODO: 생성시점에 저장이 안되어도, 동일한 키가 다시 들어오면 재사용 하면 된다.
            self.__dictUUID[strUserKey] = strUUID
            #pass
        
        return strUUID
    
    ###################################### private