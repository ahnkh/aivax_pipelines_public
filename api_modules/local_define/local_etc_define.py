
from lib_include import *

'''
Error code 정의

'''

class ApiErrorDefine:
    
    HTTP_404_NOT_FOUND = status.HTTP_404_NOT_FOUND
    HTTP_404_NOT_FOUND_MSG = "Resource Not Found Error"
    
    HTTP_500_INTERNAL_SERVER_ERROR = status.HTTP_500_INTERNAL_SERVER_ERROR
    HTTP_500_INTERNAL_SERVER_ERROR_MSG = "Internal Server Error"    
    
    API_UNKNOWN_ERROR = 9999
    API_UNKNOWN_ERROR_MSG = "Unkown Application Error"
    
    pass


#요청 및 응답 파라미터 정의
class ApiParameterDefine:
    
    PARAM_MAIN_APP = "mainapp"
    
    #action, code 두개를 만든다. (이미 action 값을 사용중)
    OUT_ACTION_CODE = "action_code"
    OUT_ACTION = "action"
    OUT_DESRIPTION = "description"
    
    #TODO: GUI에서 사용하는 필드라, 변경할수 없다. 엔진에서 호출시에만 masked_contents로 변경한다.
    OUT_CONTENT = "content"
    OUT_MASKED_CONTENTS = "masked_contents"
    OUT_BLOCK_MESSAGE = "block_message"
    
    #사용자 관련, 추가
    NAME = "name"
    EMAIL = "email"
    SESSION_ID = "session_id"
    AI_SERVICE = "ai_service"
    pass