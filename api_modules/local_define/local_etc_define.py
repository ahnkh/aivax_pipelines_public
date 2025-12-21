
from lib_include import *

'''
Api module, define 정의
'''

# Error code 정의
class ApiErrorDefine:
    
    HTTP_404_NOT_FOUND = status.HTTP_404_NOT_FOUND
    HTTP_404_NOT_FOUND_MSG = "Resource Not Found Error"
    
    HTTP_500_INTERNAL_SERVER_ERROR = status.HTTP_500_INTERNAL_SERVER_ERROR
    HTTP_500_INTERNAL_SERVER_ERROR_MSG = "Internal Server Error"    
    
    API_UNKNOWN_ERROR = 9999
    API_UNKNOWN_ERROR_MSG = "Unkown Application Error"    
    # pass


#요청 및 응답 파라미터 정의
class ApiParameterDefine:
    
    PARAM_MAIN_APP = "mainapp"
    
    #action, code 두개를 만든다. (이미 action 값을 사용중)
    OUT_ACTION_CODE = "action_code"
    OUT_ACTION = "action"
    OUT_DESRIPTION = "description"
    
    #TODO: GUI에서 사용하는 필드라, 변경할수 없다. 엔진에서 호출시에만 masked_contents로 변경한다.
    OUT_CONTENT = "content"
    
    OUT_SLM_CONTENT = "slm_content"
    OUT_MASKED_CONTENTS = "masked_contents"
    OUT_BLOCK_MESSAGE = "block_message"
    
    #사용자 관련, 추가    
    USER_ID = "user_id"
    NAME = "name"
    EMAIL = "email"
    
    SESSION_ID = "session_id"
    MESSAGE_ID = "message_id"
    
    AI_SERVICE = "ai_service"
    CLIENT_HOST = "client_host"
    
    #message, pipeline으로 전달하는 데이터, 우선 여기에 추가
    META_DATA = "metadata"
    MESSAGES = "messages"
    ATTACH_FILE = "attach_file"
    
    FILE_NAME = "file_name"
    FILE_SUMMARY = "file_summary" #파일 차단, 요약 (TODO: 별도로 분리해야 한다.)
    # FILE_DETAIL = "file_detail" #파일 차단, 상세
    FILE_INFO = "file_info" #파일 속성, 부가정보
    
    POLICY_ID = "policy_id"
    POLICY_NAME = "policy_name"
    POLICY_RULE = "policy_rule"
    
    #TODO: 여기서 부터는 API와 상관없는 항목이다. => API에서 생성되는 항목으로, 여기까지는 같이 관리..
    USER_KEY = "user_key" #사용자 키, 추가
    UUID = "uuid" #uuid, 새로 추가 (프롬프트 수집시점에, uuid가 저장되어야 한다.)
    ROLE = "role" #사용자 역할, 향후 수집 및 Define 분리 필요, 우선은 현재 사양 유지
    # pass