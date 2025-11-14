
'''
pipeline filter 관련 define
'''

class PipelineFilterDefine:
    
    #filter 옵션 탐지 및 차단
    ACTION_ALLOW = "allow"
    ACTION_BLOCK = "block"
    ACTION_MASKING = "masking"
    
    #code도 생성한다.
    CODE_ALLOW = 0
    CODE_BLOCK = 1
    CODE_MASKING = 2
    
    pass