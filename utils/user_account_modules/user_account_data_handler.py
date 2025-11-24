
from lib_include import *

'''
AI 서비스등 사용자 계정 데이터 관리
구조상 스레드 와 Queue가 존재할수 있다.
TODO: Db에 저장하는 기능을 백그라운드로 수행하며, DB I/O를 최소화 해야 한다.
global 데이터 관리는 pipelineMainApp에서 수행한다. (중간 규모의 관리)
'''

class UserAccountDataHandler:
    
    def __init__(self):
        pass
    
    # 사용자 계정관리, 초기화
    def Initialize(self, dictUserAccountDataLocalConfig:dict):
        
        '''
        '''
        
        LOG().info("initialize user account handler")
        
        return ERR_OK