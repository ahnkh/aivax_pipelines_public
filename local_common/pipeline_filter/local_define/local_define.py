
'''
pipeline filter 관련 define
'''

class PipelineFilterDefine:
    
    #filter 옵션 탐지 및 차단
    ACTION_ALLOW = "allow"
    ACTION_ACCEPT = "accept" #DB에 accept가 존재한다.
    ACTION_BLOCK = "block"
    ACTION_MASKING = "masking"
    
    #code도 생성한다.
    CODE_ALLOW = 0
    CODE_BLOCK = 1
    CODE_MASKING = 2
    
    # opensearch에 저장되는 stage 정의
    FILTER_STAGE_REGEX = "regex",
    FILTER_STAGE_FILE_BLOCK = "file-block",
    FILTER_STAGE_SLM = "slm",
    
    # slm 응답 관련
    SLM_RESPONSE_SAFE = "Safe"
    SLM_RESPONSE_UNSAFE = "Unsafe"
    
    # 추출항목, 일부 중복이나 별도로 가져간다.
    SLM_RESONSE_CHOICE = "choices"
    SLM_RESONSE_MESSAGE = "message"
    SLM_RESONSE_CONTENT = "content"
    
    
    pass