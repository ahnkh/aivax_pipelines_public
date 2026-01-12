

from lib_include import *

from type_hint import *

'''
office file, 상세 분석
파일의 1차 분석과 별개로, 차단된 파일에 대한 상세 분석 진행
'''

class OfficeFileAnalyzeHelper:
    
    def __init__(self):
        pass
    
    
    def Initialize(self,):
        
        return ERR_OK
    
    
    # file 분석 - 상세 분석 결과, 페이지, 라인번호등의 반환
    def AnalyzeFileBlockDetailReason(self, parameterItem: OfficeFileAnalysisParameterItem):
        
        '''
        정규 표현식, 정책 번호등 상세 정보의 수집이 필요하다.
        약간의 부하가 있더라도, 파일을 한번 더 읽는다. (pdf 변환, libreoffice)
        분석의 파라미터는 ModelItem을 사용해 본다.
        
        분석 결과, dictionary로 정의, 우선 opensearch를 활용한다.
        '''
        
        return ERR_OK
    
    