
#외부 라이브러리
from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.helper.filter_pattern_base import FilterPatternBase

'''
regex 패턴 탐지.
우선 기존 작성된 코드를 그대로 이관한다.
DB에서는 regex에 대한 구분이 없어서, 향후 어떤 패턴인지에 대한 구분이 추가되어야 한다.

TODO: 이 구조가 맞는지는 모르겠으나, QuickPII라는 기능을 별도로 가지고 있다.
그에 맞춰서, QuickPII를 모듈화 한다.
'''

class RegexFilterPattern (FilterPatternBase):
    
    def __init__(self):
        pass
    
    
    def Initialize(self, dictJsonLocalConfigRoot:dict):
        
        return ERR_OK
    
    
    #상속, DB의 패턴 정책을 수신받는다. 
    def notifyUpdateDBPatternPolicy(self, dictFilterPolicy:dict) -> int:
        '''
        전체 정책을 받고, 각 정책에서 필요한 부분을 추출해서 사용한다.
        TODO: 인수인계 시점에는 정책의 구분자가 없어, 받은 데이터의 rule에 대해서 로그로 확인까지만 구현한다.
        '''
        
        #count, data 밑에 있다.
        data:dict = dictFilterPolicy.get("data", {})
                
        LOG().debug(f"notify update db pattern policy in regex pattern, rule count = {len(data)}")
        
        return ERR_OK
    
    
    