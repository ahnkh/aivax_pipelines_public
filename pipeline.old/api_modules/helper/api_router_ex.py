
from fastapi import APIRouter

from lib_include import *

'''
APIRouter, 확장

'''

class ApiRouterEx(APIRouter):
    
    #일단 여기에 선언
    # STATE_KEY_PIPELINE_MAP = "pipeline_map"
    STATE_KEY_MAINAPP = "mainapp"
    
    def __init__(self, *args, **kwargs):
        
        super().__init__(*args, **kwargs) 
        
        #state 와 유사한 변수 필요
        self.__dictState:dict = {}
        pass
    
    def AddState(self, strStateKey:str, data:Any):
        
        self.__dictState[strStateKey] = data        
        return ERR_OK
    
    # # 그대로 복사한다.
    # def AddState(self, dictState:dict):
        
    #     self.__dictState = dictState        
    #     return ERR_OK
    
    
    def GetState(self, strStateKey:str) -> Any:
        
        return self.__dictState.get(strStateKey)
    