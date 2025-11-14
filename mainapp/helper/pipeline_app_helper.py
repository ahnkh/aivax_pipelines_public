

import orjson

from lib_include import *

from type_hint import *

#TODO: MainApp, AppHelper에서만 참조하고 다른 곳은 참조하지 않도록 제한
from utils.log_write_modules.log_write_handler import LogWriteHandler

class PipelineAppHelper:
    
    def __init__(self):
        pass
    
    #LogHandler에 Log를 추가한다.
    def AddLogData(self, logWriteHandler:LogWriteHandler, strDataType:str, dictOuptut:dict):
        
        '''
        일단 여기까지만 오염되고, 추가적인 Helper는 구현의 복잡도가 커져서 고려하지 않는다.
        '''
        
        #TODO: output, 감싸서 들어올수 있다. DB 데이터외 부가정보, 개발하면서 확정
        byteLogData:bytes = orjson.dumps(dictOuptut)
            
        #마지막에 byte, 개행 추가
        byteLogData += b'\n'
        
        #Queue 추가, 이후의 예외처리는 logHandler에서 담당한다.
        logWriteHandler.AddData(strDataType, byteLogData)
        
        return ERR_OK
    
    #Pipeline 모듈을 업데이트 하고 연결한다. 양방향 연결
    def LinkPipelineModules(self, mainApp:Any, dictPipelineModules:dict):
        
        '''
        각 pipeline을 순회하면서 mainApp을 연결한다.
        모든 pipeline에 연결을 걸자. 테스트도. 
        상속도 고려.
        '''
        
        from local_common.pipeline_filter.pipeline_base import PipelineBase
        
        strLinkAppMethodName = "link_mainapp"
        
        for strPipelineKey in dictPipelineModules.keys():
            
            pipeline:PipelineBase = dictPipelineModules.get(strPipelineKey)
            
            #TODO: 신규 pipeline에는 없을수 있어서, 있는것만 추가하고, 없는건 LOG추가
            #TODO: 성능 이슈, 향후에는 모든 pipeline에 추가한다.
            
            if hasattr(pipeline, strLinkAppMethodName):
                
                methodFunction = getattr(pipeline, strLinkAppMethodName)
                
                methodFunction(mainApp)
            else:
                
                LOG().info(f"skip {strLinkAppMethodName}, pipeline = {strPipelineKey}")
                continue
            
        
        return ERR_OK