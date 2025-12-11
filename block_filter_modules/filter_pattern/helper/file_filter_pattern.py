
#외부 라이브러리
from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.helper.filter_pattern_base import FilterPatternBase

'''
file filter 패턴, 
TODO: 기존 pipeline 패턴과 동일 패턴으로, 신규 추가
제공 기능 :
- 탐지 시점에 file 정보를 분석하고, 텍스트를 추출한다.
- 정책을 수신 받으며 (별도의 DB 정책), 정책에 의해서 파일내 민감정보를 추출한다.
- Masking 기능은 불필요, 차단 여부를 선택한다.
- 결과는 기존 BlockFilter과 유사 패턴으로 제공한다.

TODO: 파일명 체크, 절대 경로이면 그대로 사용하고, 상대경로이면 지정된 경로에서 가져온다.
- 1차 개발은 절대 경로로 지정한다.
'''

class FileFilterPattern(FilterPatternBase):
    
    def __init__(self):
        
        super().__init__()
        pass
    
    def Initialize(self, dictJsonLocalConfigRoot:dict):
        
        '''
        '''
        
        return ERR_OK
    
    # 패턴 탐지, 이름은 동일, 파라미터, 전달 인자는 상이.
    def DetectPattern(self, ):
        
        return ERR_OK
    
    ####################################### private


