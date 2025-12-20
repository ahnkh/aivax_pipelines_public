
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
    
    
    # 사용자 메시지 처리, 우선 하드코딩
    def CustomBlockMessages(self, strPolicyCategory) -> str:
        
        '''
        '''
        
        strBlockMessage:str = f'''[AIVAX] 프롬프트 차단
AIVAX 정책에 의해 민감정보가 프롬프트에 포함된 것으로 탐지되었습니다.
❌탐지 유형은 '{strPolicyCategory}' 입니다.
민감 정보를 전송할 경우, 기밀 정보 또는 개인 정보 유출등의 피해가 발생할 수 있으니 각별한 주의를 부탁드려요
요청하신 프롬프트는 AIVAX에 의해서 요청이 차단되었습니다.
세부 지침 사항은 관리자에게 문의해주세요
        '''
        
        return strBlockMessage
    
    # SLM 시점의 사용자 masking 문자열, 우선 하드코딩, 향후 UI에서 제어
    def CustomMaskMessageOfSLM(self, strPolicyCategory) -> str:
        
        '''
        '''
        
        strMaskedMessage:str = f'''[AIVAX] 프롬프트 마스킹
AIVAX 정책에 의해 민감정보가 프롬프트에 포함된 것으로 탐지되었습니다.
❌탐지 유형은 '{strPolicyCategory}' 입니다.
세부 지침 사항은 관리자에게 문의해주세요
        '''
        
        return strMaskedMessage