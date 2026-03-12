
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
        
        
        # 제일 마지막, 가져올 데이터가 없으면, 처음 데이터를 전달한다.
        dictPrompt = next(iter(dictPromptLogMap.values()), {})
        
        return dictPrompt
    
    