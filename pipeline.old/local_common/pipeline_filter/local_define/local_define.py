
'''
'''

class PipelineFilterDefine:
    
    ACTION_ALLOW = "allow"
    ACTION_BLANK = "" 
    ACTION_ACCEPT = "accept"
    ACTION_BLOCK = "block"
    ACTION_MASKING = "masking"
    ACTION_UNDETECTED = "undetected"
    
    CODE_ALLOW = 0
    CODE_BLOCK = 1
    CODE_MASKING = 2
    
    FILTER_STAGE_REGEX = "regex"
    FILTER_STAGE_FILE_BLOCK = "file block"
    FILTER_STAGE_SLM = "slm"
    
    SLM_RESPONSE_SAFE = "Safe"
    SLM_RESPONSE_UNSAFE = "Unsafe"
    
    SLM_RESONSE_CHOICE = "choices"
    SLM_RESONSE_MESSAGE = "message"
    SLM_RESONSE_CONTENT = "content"    
    pass