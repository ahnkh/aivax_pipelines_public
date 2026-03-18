
from lib_include import *

'''
병합이 실패한 로그에 대한 처리
output로그가 들어왔을때, input로그를 못찾으면, MergeFailOuputLog에서 적절한 로그를 찾는다.
ouput로그가 들어오면, 반드시 저장이 되어야 한다.
단, 기존 input로그는 일단 남겨둔다. => 향후 일괄로 정리
'''

class MergeFailOutputLogHandler:
    
    def __init__(self):
        pass
    
    #저장이 실패한 로그, 다시 만든다.
    def HandleMergeFailLog(self, strLogMessageKey:str, dictOutputLog:dict, dictPromptLogMap:dict) -> dict:
        
        '''
        TODO: output 로그에 실패시 키가 될만한 필드를 충분히 추가한다.
        uptdate구조라, 같은 필드를 추가한다.
        '''
        
        #1차, prompt 로그가 없으면 실패이다. 이때는 output로그만 저장한다.
        if 0 == len(dictPromptLogMap):
            return {}
        
        
        #TODO: 알고리즘 고안 필요
        #promptLogMap을 전체 순회, 계정(메일), 서비스ID가 같으면 그 프롬프트를 사용한다.
        #오탐 가능성이 크기는 하다.
        
        #실패시점의 프롬프트 추출 기준
        # outputlog에 email, ai서비스 유형, 타임스탬프가 존재한다.
        # email, 서비스 유형이 같은 프롬프트이고, 타임스탬프가 기준값, 상수 (= 5분이내)인 가장 먼저 찾아지는 프롬프트를 선정한다.
        # user:dict = dictOutputLog.get("user")
        
        strOutputID:str = dictOutputLog.get("user_id")
        strOutputEmail:str = dictOutputLog.get("user_email")
        strOutputClientHost:str = dictOutputLog.get("client_host")
        strOutputAIService:str = dictOutputLog.get("ai_service")
        
        #일단 성능 최우선, 함수로 나누지 말고 내부에서 찾는다.
        for dictPrompt in dictPromptLogMap.values():
            
            input_user:dict = dictPrompt.get("user")
            
            strPromptID:str = input_user.get("id")
            strPromptEmail:str = input_user.get("email")
            
            strPromptClientHost:str = dictPrompt.get("client_host")
            strPromptAIService:str = dictPrompt.get("ai_service")
            
            # email, ID, IP가 같은지 확인
            if (strPromptEmail == strOutputEmail) or (strOutputID == strPromptID):
                
                if (strOutputAIService == strPromptAIService) and (strOutputClientHost == strPromptClientHost):
                    
                    #3번째 검증, 시간이 5분 이내여야 한다.
                    
                    strOutputTimeStamp:str = dictOutputLog.get("@timestamp")
                    strPromptStamp:str = dictPrompt.get("@timestamp")
                    
                    tOutput = datetime.datetime.fromisoformat(strOutputTimeStamp.replace("Z", "+00:00"))
                    tPrompt = datetime.datetime.fromisoformat(strPromptStamp.replace("Z", "+00:00"))

                    diff = abs(tOutput - tPrompt)

                    if (diff <= datetime.timedelta(minutes=5)):
                        
                        LOG().info(f"find matched prompt log, email = {strPromptEmail}, service = {strPromptAIService}, timestamp = {strOutputTimeStamp}")
                        
                        return dictPrompt
                        
        
        
        # 모두 loop를 돌았는데, email, 서비스 유형이 없으면, 다음 로직으로 반환
        # 제일 마지막, 가져올 데이터가 없으면, 처음 데이터를 전달한다.
        dictPrompt = next(iter(dictPromptLogMap.values()), {})
        
        return dictPrompt
    
    