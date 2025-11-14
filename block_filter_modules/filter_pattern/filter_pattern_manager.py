
#외부 라이브러리
from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.helper.filter_pattern_base import FilterPatternBase

from block_filter_modules.filter_pattern.helper.detect_secret_filter_pattern import DetectSecretFilterPattern

from block_filter_modules.filter_pattern.helper.regex_filter_pattern import RegexFilterPattern


'''
filter 패턴에 대한 탐지 관리, Manager
다른 pattern도 통칭하여 관리하고, 공통화등 관리를 담당한다.
Filter 모듈의 최소화, 탐지 여부는 이 모듈에서 담당한다.
maskinig, 차단, pipeline과 분리한다.

1차 리펙토링, 기존 코드 그대로 이동. 이후 다시 DB 모듈화
각 패턴별로 다시 모듈화
'''

class FilterPatternManager:
    
    #filter 패턴 키, 상수 나중에 공통으로 이동
    PATTERN_FILTER_DETECT_SECRET = "detect_secret"
    PATTERN_FILTER_REGEX = "regex"
    
    def __init__(self):
        
        # 키정보, 미리 선언
        self.__filterPatternMap:dict = {
            
            FilterPatternManager.PATTERN_FILTER_DETECT_SECRET : None,
            FilterPatternManager.PATTERN_FILTER_REGEX : None,
        }
        
        pass
    
    
    #초기화 로직, TODO: Filter 정책관리 모듈과 연결한다.
    def Initialize(self, dictJsonLocalConfigRoot:dict):
        
        '''
        '''
        
        detectSecretPattern = DetectSecretFilterPattern()
        detectSecretPattern.Initialize(dictJsonLocalConfigRoot)
        
        regexFilterPattern = RegexFilterPattern()
        regexFilterPattern.Initialize(dictJsonLocalConfigRoot)
        
        self.__filterPatternMap[FilterPatternManager.PATTERN_FILTER_DETECT_SECRET] = detectSecretPattern
        self.__filterPatternMap[FilterPatternManager.PATTERN_FILTER_REGEX] = regexFilterPattern
        
        return ERR_OK
    
    #정책관리자에서 정책이 변경되면, 변경된 정책을 dictionary 형태로 수신 받는다.
    def notifyDBPolicyUpdateSignal(self, dictFilterPolicy:dict):
        
        '''
        각 패턴 필터에 브로드캐스팅한다. 
        TODO: 아직 정책의 DB구조는 완벽하지 않다. 받은 정책 원본을 전달하고, 각 정책에서 필요한 정책을 가져가는 구조로 개발한다.
        현재 DB정책이 pipeline 별로 분리되어 있지는 않다.
        '''
        
        for strPatternKey in self.__filterPatternMap:
            
            filterPatternModule:FilterPatternBase = self.__filterPatternMap.get(strPatternKey)
            
            filterPatternModule.notifyUpdateDBPatternPolicy(dictFilterPolicy)
        
        return ERR_OK
    
    #Filter 반환, 타입별 저장
    def GetFilterPattern(self, strFilterKey:str) -> Any:
        
        filterPattern:Any = self.__filterPatternMap.get(strFilterKey)
        
        #미존재시 Exception 처리
        if None == filterPattern:
            raise Exception(f"invalid filter pattern, not exist, filter key = {strFilterKey}")
            #return None
        
        return filterPattern
    