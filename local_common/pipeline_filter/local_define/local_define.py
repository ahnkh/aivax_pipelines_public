
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
    
    # opensearch에 저장되는 stage 정의
    FILTER_STAGE_REGEX = "regex",
    FILTER_STAGE_FILE_BLOCK = "file-block",
    
    pass