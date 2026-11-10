

from lib_include import *

# from type_hint import *

'''
pipeline base module, 일단 상속 최소화
'''

class PipelineBase:
    
    def __init__(self):
        
        '''
        '''
        
        #TODO: 순환 참조 문제
        from mainapp.pipeline_main_app import PipeLineMainApp

        #mainapp 연결
        self.__mainApp:PipeLineMainApp = None
        
        pass
    
    #mainapp 참조 연결, 설정, getattr로 접근, snake notation
    def link_mainapp(self, mainApp:Any):
        
        '''
        '''
        
        #참조 연결, TODO: None 체크
        self.__mainApp = mainApp
        
        return ERR_OK
    
    #TODO: 가급적 자제
    def GetMainApp(self, ) -> Any:
        
        '''
        '''        
        return self.__mainApp
    
    #한번더 wrapping 하자.
    def AddLogData(self, strDataType:str, dictOuptut:dict):
        
        '''
        '''
        
        self.__mainApp.AddLogData(strDataType, dictOuptut)
        return ERR_OK
    
    #filter pattern key를 통해서 모듈 반환, 상속 대신 키를 통한 다형 패턴으로 제공
    def GetFilterPatternModule(self, strFilterPatternKey:str) -> Any:
        
        '''
        TODO: 예외처리는 exception 존재.
        '''
        
        return self.__mainApp.GetFilterPatternModule(strFilterPatternKey)
    
    