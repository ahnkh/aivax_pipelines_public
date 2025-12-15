
from lib_include import *

from type_hint import *

'''
pipeline filter, local customize utils
'''

class FilterCustomUtils:
    
    def __init__(self):
        pass
    
    
    #사용자 정보를 반환한다. tuple로 전달
    def GetUserData(self, dictUserInfo:dict) -> tuple:
        
        '''
        '''
        
        user_id:str = ""
        user_email:str = ""
        ai_service_type:int = AI_SERVICE_DEFINE.SERVICE_UNDEFINE #없으면, 기본 GPT
        uuid:str = ""
        client_host:str = ""
        
        if None != dictUserInfo:
            
            user_id = dictUserInfo.get(ApiParameterDefine.NAME, "")
            user_email = dictUserInfo.get(ApiParameterDefine.EMAIL, "")
            ai_service_type = dictUserInfo.get(ApiParameterDefine.AI_SERVICE, AI_SERVICE_DEFINE.SERVICE_UNDEFINE)
            
            client_host = dictUserInfo.get(ApiParameterDefine.CLIENT_HOST, "") #TODO: 2단계만 수집 가능
            
            uuid = dictUserInfo.get(ApiParameterDefine.UUID, "")
            # pass
            
            
        return (user_id, user_email, ai_service_type, uuid, client_host)