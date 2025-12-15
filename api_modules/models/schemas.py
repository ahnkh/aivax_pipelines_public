
# from typing import List, Optional
# from pydantic import BaseModel, ConfigDict

from typing import Union

from lib_include import *
from type_hint import *

class OpenAIChatMessage(BaseModel):
    role: str
    
    #python 3.10 이상만 지원, 3.9 이하에서는 from typing import Union 사용
    # content: str | List
    content: Union[str, List]
    
    model_config = ConfigDict(extra="allow")
    pass

class OpenAIChatCompletionForm(BaseModel):
    stream: bool = True
    model: str
    messages: List[OpenAIChatMessage]

    model_config = ConfigDict(extra="allow")
    pass


class FilterForm(BaseModel):
    body: dict
    user: Optional[dict] = None
    model_config = ConfigDict(extra="allow")
    pass
    
# # 다중 차단 필터 - 사용자 정보 관리 => depth 제거, 이력만 유지
# class VariantFilterUserItem(BaseModel):
    
#     id : Optional[str] = Field(default="", description="사용자ID")
#     email : Optional[str] = Field(default="", description="email")
#     client_host : Optional[str] = Field(default="", description="사용자 host, ip")
#     session_id : Optional[str] = Field(default="", description="session id") 

#엔진등, 다중 차단을 위한 API 데이터
class VariantFilterForm(BaseModel):
    
    '''
    filter_list : 차단 필터 리스트
    
    - llm_filter : AI 필터
    - inlet_raw_logger : 테스트용, 미사용
    - secret_filter : API 차단 필터
    - regex_filter : 정규표현식 기반 필터
    - file_block_filter : 파일 분석 필터
    - input_filter : opensearch 저장 (프롬프트)
    - output_filter : opensearch 저장 (LLM 응답)    
    
    prompt : 프롬프트 문자열 (예: 프롬프트를 입력해주세요)
    
    - prompt, prompt_base64 둘다 사용시, prompt를 우선하여 사용
    
        body": {
        "messages": [
        {"role": "user", "content": "안녕하세요"}
        ]
    },
    "user": {
        "id": "u1234",
        "name": "홍길동"
    }
    }'
    '''
    
    filter_list: Optional[List[str]] = ["input_filter", "secret_filter", "file_block_filter"] #차단 필터 리스트, 기본값 secret_filter
    
    # prompt: str = "프롬프트를 입력해주세요" #
    prompt: str = Field(default="", description="입력 프롬프트")
    
    #케이스1, 결과 = 차단 성공 (secre filter, regex_filter는 차단 실패)
    # prompt:str = "API_key=sk-1234567-0000-abdcdef"
    
    #케이스2, 결과 = 차단 실패, 키 길이 문제, 15~20 으로 유연하게 정규식 변경 필요    
    # prompt:str = "내 API key는 API_key=sk-1234567-0000-abdcdef 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요"
    
    #케이스4, 결과 = 차단 실패
#     prompt:str = '''
#     AI SERVER 20.240.11.15 
# GIT SERVER 192.168.10.19 
# BUILD SERVER 210.10.90.180    
#     '''
    
    #케이스5, 결과 = 차단 성공, secret filter (regex_filter는 차단 실패)
    
    # prompt:str = '''
    # private static final String PRIVATE_KEY = 
    #     "-----BEGIN RSA PRIVATE KEY-----\n" +
    #     "MIIEpAIBAAKCAQEAy8Dbv8prpJ/0kKhlGeJYozo2t60EG8L0561g13R29LvMR5hy\n" +
    #     "vGZlGJpmn65+A4xHXInJYiPuKzrKUnApeLZ+vw1HocOAZtWK0z3r26uA8kQYOKX9\n" +
    #     "Qt/DbCdvsF9wF8gRK0ptx9M6R13NvB9TE4Rf/01H\n" +
    #     "-----END RSA PRIVATE KEY-----"
    
    # # '''
    
    #케이스6, 결과 = 차단 성공 secret filter (regex_filter는 차단 실패)
#     prompt:str = '''
#     [2025-10-01 14:32:15] INFO: API request to payment gateway successful with key: sk_live_51HG7OkLkhB2uTGQhvF
# [2025-10-01 14:32:16] DEBUG: Response from authentication service: {"token":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."}
# [2025-10-01 14:32:17] ERROR: Failed to connect to cloud service with credentials: AKIAIOSFODNN7EXAMPLE:wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
#     '''
    
    #케이스7, 결과 = 차단 성공 secret_filter (regex_filter는 차단 실패)
#     prompt:str = '''
#     production:
#   api_keys:
#     openai: "sk-1234abcd5678efgh9012ijkl3456mnop7890qrst" 
#     aws_secret: "AKIAIOSFODNN7EXAMPLE/wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" 
#     database: "postgres://username:p@ssw0rd123@hostname:5432/database
#     '''
    
    #케이스8, 결과 = 차단 성공 secret_filter (regex_filter는 차단 실패)
#     prompt:str = '''
#     git log -p
# # 출력 결과에 API 키가 포함됨
# +    const API_KEY = "AIzaSyC1b9jkS0Pq5XRxF5PEiGDYOyRLiQ3cXQk";
# +    const SECRET = "8f7b631c-ec38-4073-90b2-23da12da";
#     '''    
    
    # encoding:bool = Field(default=False, description="프롬프트 인코딩 여부")
    encoding: Optional[bool] = Field(default=False, description="프롬프트 인코딩 여부")
    
    # prompt_base64:str = ""
    
    #향후 sessionid등 필요 정보 검토, 일단 고려하지 않고, 지금은 프롬프트가 필요
    
    # 일단 나머지는 유지.    
    # etc_flag: dict = Field(default=None, description="부가옵션")    
    # etc_flag: Optional[dict] = Field(default={}, description="부가옵션")
    # user: Optional[dict] = None
    # model_config = ConfigDict(extra="allow") 
    
    #요청측의 편의성, 하나로 통일
    # user_role: Optional[VariantFilterUserItem] = Field(default=None, description="사용자 관리 정보")
    
    user_id : Optional[str] = Field(default="", description="사용자ID")
    email : Optional[str] = Field(default="", description="email")
    ai_service : Optional[int] = Field(default=0, description="ai 서비스 타입 (GPT=0, CLAUDE=1, GEMINI=2,)")
    client_host : Optional[str] = Field(default="", description="사용자 host, ip")
    session_id : Optional[str] = Field(default="", description="session id")
    
    # file 분석 기능 추가, 옵션, 다수의 리스트를 전달
    # TODO: 파일명으로, 파일 사이즈, 헤더, 파일 속성등을 알아야 할수도 있다.    
    attach_files: Optional[List[str]] = Field(default_factory=list, description="첨부 파일 리스트")
    
    # attach_files: Optional[List[str]] = ["/home1/aivax/data_resource/attach_file/sample.docx"]
    pass

#outputfilter form 추가
class OutputFilterItem(BaseModel):
    
    llm_output: str = Field(default="", description="llm응답 결과") 
    
    user_id : Optional[str] = Field(default="", description="사용자ID")
    email : Optional[str] = Field(default="", description="email")
    ai_service : Optional[int] = Field(default=0, description="ai 서비스 타입 (GPT=0, CLAUDE=1, GEMINI=2,)")
    client_host : Optional[str] = Field(default="", description="사용자 host, ip")
    session_id : Optional[str] = Field(default="", description="session id")   
    pass    
    
class AddPipelineForm(BaseModel):
    url: str

class DeletePipelineForm(BaseModel):
    id: str    
    
#filter 룰 테스트 기능 추가
class FilterRuleTestItem(BaseModel):
    
    prompt: str = Field(default="", description="입력 프롬프트")
    
    rule:str = Field(default="", description="정책 Rule")
    action:str = Field(default="", description="action (block/masking)")    
    pass

