
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
    
    
    # 사용자 메시지 처리, 우선 하드코딩, 단순 패턴일때는 category 1개로 표기
    def CustomBlockMessages(self, strPolicyCategory:str) -> str:
        
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
    
    # 사용자 메시지 처리, SLM, 문구 변경
    def CustomSLMBlockMessage(self, lstEvidence:list):
        
        '''
        TODO: Evidence, Wins 모델에서만 지원한다.
        [{\"type\": \"PHONE\", \"value\": \"010-1234-5678\"}, {\"type\": \"EMAIL\", \"value\": \"test@example.com\"}]
        '''
        
        strDetectType:str = ""        
        strDetectValue:str = ""
        
        if 1 == len(lstEvidence):
            
            dictEvidence:dict = lstEvidence[0]
            
            strDetectType = dictEvidence.get("type")
            strDetectValue = dictEvidence.get("value")
            
        elif 2 <= len(lstEvidence):
            
            strDetectType = ",".join(item["type"] for item in lstEvidence)
            strDetectValue = ",".join(item["value"] for item in lstEvidence)
            
        else: #탐지 사유가 없는 경우
            
            strDetectType = "nodetect"
            strDetectValue = "unknown"            
            # pass
                
        
        strBlockMessage:str = f'''[AIVAX] 프롬프트 차단
SLM 필터 정책에 의해 민감정보가 프롬프트 문맥에 포함된 것으로 탐지되었습니다.
❌탐지 유형은 '{strDetectType}' 입니다.
❌탐지된 내용은 '{strDetectValue}' 입니다.
민감 정보를 전송할 경우, 기밀 정보 또는 개인 정보 유출등의 피해가 발생할 수 있으니 각별한 주의를 부탁드려요
요청하신 프롬프트는 AIVAX에 의해서 요청이 차단되었습니다.
세부 지침 사항은 관리자에게 문의해주세요
        '''
        
        return strBlockMessage
        
        
    
    # SLM 시점의 사용자 masking 문자열, 우선 하드코딩, 향후 UI에서 제어
    def CustomMaskMessageOfSLM(self, lstEvidence:list) -> str:
        
        '''
        '''
        
        # TODO: 중복된 코드, 향후 정규화 + 개선
        strDetectType:str = ""        
        strDetectValue:str = ""
        
        if 1 == len(lstEvidence):
            
            dictEvidence:dict = lstEvidence[0]
            
            strDetectType = dictEvidence.get("type")
            strDetectValue = dictEvidence.get("value")
            
        elif 2 <= len(lstEvidence):
            
            strDetectType = ",".join(item["type"] for item in lstEvidence)
            strDetectValue = ",".join(item["value"] for item in lstEvidence)
            
        else: #탐지 사유가 없는 경우
            
            strDetectType = "nodetect"
            strDetectValue = "unknown"            
            # pass
        
        strMaskedMessage:str = f'''[AIVAX] 프롬프트 마스킹
SLM 필터 정책에 의해 민감정보가 프롬프트 문맥에 포함된 것으로 탐지되었습니다.
❌탐지 유형은 '{strDetectType}' 입니다.
❌탐지된 내용은 '{strDetectValue}' 입니다.

세부 지침 사항은 관리자에게 문의해주세요
        '''
        
        return strMaskedMessage
    
    # 차단된 정책 카테고리명의 반환
    def ConvertBlockPolicyCategory(self, lstDetectEvidence:list, strPolicyName:str) -> str:
        
        '''
        2개 이상 탐지가 되면, category를 N개로 표기한다.
        '''
        
        # 탐지 개수가 1개이면, 그대로 반환
        if 1 == len(lstDetectEvidence):
            
            return strPolicyName
        
        # 2개로 제한
        strFirstRuleName:str = ""
        
        for dictDetectResult in lstDetectEvidence:
            
            strRuleName:str = dictDetectResult.get(DBDefine.DB_FIELD_RULE_NAME, "")
            
            if 0 == len(strFirstRuleName):
                strFirstRuleName = strRuleName
                
            else: # 2번째면, 종료
                
                if strFirstRuleName != strRuleName:
                    
                    strFirstRuleName += "," + strRuleName
                    
                    return strFirstRuleName
        
        
        # 오면 안되는 구문, 이때는 기본값 반환
        return strPolicyName