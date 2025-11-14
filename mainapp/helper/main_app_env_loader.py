
import os

from dotenv import load_dotenv, find_dotenv

#외부 라이브러리
from lib_include import *

'''
전역설정, 환경 설정 관리는 env loader로 관리한다.
'''

class MainAppEnvLoader:
    
    def __init__(self):
        pass
    
    
    #초기 환경변수, 
    def Initalize(self, dictOpt:dict, dictJsonLocalConfigRoot:dict):
        
        strConfigFilePath:str = dictOpt.get(APP_PARMETER_DEFINE.CONFIG)
        
        #과거 로직, 승계 유지
        self.__loadLegacyEnvFile()
        
        #mainapp등 로직등에서 필요한 config, load
        self.__localLocalConfigFile(strConfigFilePath, dictJsonLocalConfigRoot)
        
        return ERR_OK
    
    def __localLocalConfigFile(self, strConfigFilePath:str, dictJsonLocalConfigRoot:dict):
        
        '''
        config 파일을 로딩한다. json 정보를 dictionary로 변환후 전역으로 가지고 있는다.
        '''
        
        LOG().info(f"load local config, path = {strConfigFilePath}")
        
        #TODO: 모듈 활용
        nErrorLoadJson = JsonHelper.JsonFileToDictionary(strConfigFilePath, dictJsonLocalConfigRoot)
        
        if ERR_FAIL == nErrorLoadJson:
            raise Exception(f"fail load local config, path = {strConfigFilePath}")
        
        return ERR_OK
    
    #.env 파일 로드, 기존 로직, TODO: 불필요한 exception 처리하지 말것.
    def __loadLegacyEnvFile(self):
        
        '''
        '''
        
        load_dotenv(find_dotenv("./.env"))
        
        # env 정보, 업데이트 된 이후에 설정 정보를 업데이트
        # 변수의 관리는 lib_include에서 관리.    
        # TODO: 기존 작성된 전역변수는 유지하되, 여기만 더럽힌다.
        API_KEY = os.getenv("PIPELINES_API_KEY", "0p3n-w3bu!")
        PIPELINES_DIR = os.getenv("PIPELINES_DIR", "./pipelines")
        
        #config 관련, 소스 감춤
        
        #main 함수로 이동 => 이 기능은 전역 자원 관리로 이동한다.
        #env가 먼저 기동 되어야 한다. 디버깅, 어떤 디렉토리인지 확인 필요
        #일단 이건 유지하자.
        if not os.path.exists(PIPELINES_DIR):
            os.makedirs(PIPELINES_DIR)
        
        return ERR_OK
    
    