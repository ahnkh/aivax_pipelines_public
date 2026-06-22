
# from typing import List, Optional
# from pydantic import BaseModel, ConfigDict

from typing import Union

from dataclasses import dataclass, field

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

# 차단정보, 별도의 form으로 전달한다. => 변환하되, 부가정보는 불필요
# 파일명만 넘어온다. 경로는 설정에 의해.
class FileAttachItem(BaseModel):
    
    id : Optional[str] = Field(default="", description="file id")
    size : Optional[int] = Field(default=0, description="file size")
    name : Optional[str] = Field(default="", description="file name")
    mime_type : Optional[str] = Field(default="", description="mime type")
    # pass

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
    
    # filter_list: Optional[List[str]] = ["input_filter", "secret_filter", "file_block_filter"] #차단 필터 리스트, 기본값 secret_filter
    filter_list: Optional[List[str]] = ["input_filter", "secret_filter", "file_block_filter", "slm_filter"] #테스트
    
    # prompt: str = "프롬프트를 입력해주세요" #
    prompt: str = Field(default="", description="입력 프롬프트")
    
    #케이스1, 결과 = 차단 성공 (secre filter, regex_filter는 차단 실패)
    # prompt:str = "API_key=sk-1234567-0000-abdcdef"
    
    #케이스2, 결과 = 차단 실패, 키 길이 문제, 15~20 으로 유연하게 정규식 변경 필요    
    prompt:str = "내 API key는 API_key=sk-1234567-0000-abdcdef 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요"
    
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
    # attach_files: Optional[List[str]] = Field(default_factory=list, description="첨부 파일 리스트")
    # attach_files: Optional[List[FileAttachItem]] = Field(default_factory=list, description="첨부 파일 리스트")
    attachments: Optional[List[FileAttachItem]] = Field(default_factory=list, description="첨부 파일 리스트")
    
    # attach_files: Optional[List[str]] = ["/home1/aivax/data_resource/attach_file/sample.docx"]
    
    # 요청 및 응답간의 연결 키
    message_id:str = Field(default="", description="message id")
    
    debug: Optional[bool] = Field(default=False, description="debug mode")
    pass

#outputfilter form 추가
# @dataclass(slots=True)
class OutputFilterItem(BaseModel):
    
    llm_output: str = Field(default="", description="llm응답 결과") 
    
    user_id : Optional[str] = Field(default="", description="사용자ID")
    email : Optional[str] = Field(default="", description="email")
    ai_service : Optional[int] = Field(default=0, description="ai 서비스 타입 (GPT=0, CLAUDE=1, GEMINI=2,)")
    client_host : Optional[str] = Field(default="", description="사용자 host, ip")
    session_id : Optional[str] = Field(default="", description="session id")   
    
    message_id:str = Field(default="", description="message id") 
    
    debug: Optional[bool] = Field(default=False, description="debug mode")   
    # pass    
    
class AddPipelineForm(BaseModel):
    url: str

class DeletePipelineForm(BaseModel):
    id: str    
    
#filter 룰 테스트 기능 추가
class FilterRuleTestItem(BaseModel):
    
    prompt: str = Field(default="", description="입력 프롬프트")
    
    rule:str = Field(default="", description="정책 Rule")
    action:str = Field(default="", description="action (block/masking)")   
    
    # test 구분 필드
    typeMask:int = Field(default=1, description="1: regex, 2:slm, 3:regex+slm")   
     
    pass

class FilterPolicySignalItem(BaseModel):
    
    '''
    '''
    
    date : datetime.datetime = Field(default_factory=datetime.datetime.now)    
    pass


# # file 분석, parameterItem
# class OfficeFileAnalysisParameterItem(BaseModel):
    
#     file_path:str = Field(default="", description="office 파일 경로")
    
#     mime_type:str = Field(default=FileDefine.FILE_EXT_UNKNOWN, description="office 파일 Mime type")
    
#     read_timeout : Optional[int] = Field(default=60, description=" 파일 read timeout")
    
#     regex_pattern:dict = Field(default={}, description="정책 패턴") 
    
#     pass


#FilterConfig, Pipeline별 세부 정책
class RegexFilterConfigItem(BaseModel):
    
    enable_filter: Optional[bool] = Field(default=False, description="Regex filter, 사용여부")
    full_scan_flag: Optional[bool] = Field(default=False, description="Regex filter, fulll 스캔 설정, 기본 비활성")
    pass

class FileFilterConfigItem(BaseModel):
    enable_filter: Optional[bool] = Field(default=False, description="file filter, 사용여부")
    pass

class SLMFilterConfigItem(BaseModel):
    enable_filter: Optional[bool] = Field(default=False, description="SLM filter, 사용여부")
    pass

# pipeline filter, 세부 config item
class PipelineCustomFilterConfigItem(BaseModel):
    '''
    '''
    
    # TODO: 정책 filter 흐름제어, ssl proxy로 전달 결과 제어 flag, bitmask
    # 0: allow, 1: block, 2: masking, 3: block+masking
    ssl_proxy_bypass_bitmask : Optional[int] = Field(default=FilterDefine.SSL_PROXY_BYPASS_ALLOW, description="ssl proxy bypass 설정")
    
    # 차단후 다음 차단을 수행할지 여부 => 이름 다시. 이름과는 별개로, 이게 가장 쉽다.
    next_detect_after_block : Optional[bool] = Field(default=False, description="전체 pipeline filter의 탐지 여부")
    
    # masking, 차단 문구, template
    
    # filter 동작 제어, 차단으로 탐지후에도 다음 filter를 수행 여부, 기본값 OFF
    
    regex_filter_config: Optional[RegexFilterConfigItem] = Field(default_factory=RegexFilterConfigItem, description="regx filter에 개별 config")
    file_filter_config: Optional[RegexFilterConfigItem] = Field(default_factory=FileFilterConfigItem, description="file filter에 개별 config")
    slm_filter_config: Optional[RegexFilterConfigItem] = Field(default_factory=SLMFilterConfigItem, description="slm filter에 개별 config")
    
    pass

#Regex Filter 탐지 요청 (detect secret)
# class RegexPatternDetectFilterParameterItem:
    
#     '''
#     Regex 패턴 요청, Filter로 요청
#     '''
    
#     # contents, 반드시 존재해야 한다.
#     contents:str = Field(default="", description="AI 프롬프트")
    
#     user_id:str = Field(default="", )
#     uuid:str = Field(default="", )
    
#     ai_service_type : Optional[int] = Field(default=AI_SERVICE_DEFINE.SERVICE_UNDEFINE)
    
#     valves:Any = Field(default=None, )
    
#     #regex, 전체 scan옵션
#     regex_fullscan_flag:Optional[bool] = Field(default=False, description="regex 패턴, 전체 scan flag")
#     # pass

@dataclass(slots=True)
class RegexPatternDetectFilterParameterItem:
    
    '''
    Regex 패턴 요청, Filter로 요청
    '''

    contents:str
    
    user_id:str
    uuid:str
    
    ai_service_type : Optional[int]    
    valves:Any
    
    #regex, 전체 scan옵션
    regex_fullscan_flag:Optional[bool]    
    # regex_fullscan_flag: bool | None = False
    # pass
    
#Regex Filter 탐지 결과
# class RegexPaternDetectFilterResultItem(BaseModel):
    
#     '''
#     Regex 패턴 응답 결과
#     '''
    
#     #일단 기존것 추가, 그대로 동작하도록 처리
    
#     counts:dict = {"block": 0, "masking": 0, "accept": 0}
#     spans: List[Tuple[int, int]] = []
#     dictDetectRule: dict = {}
    
#     #탐지된 룰 정보
#     detect_rule_list:list = []
    
#     pass

@dataclass(slots=True)
class RegexPaternDetectFilterResultItem:
    
    '''
    Regex 패턴 응답 결과
    '''
    
    #일단 기존것 추가, 그대로 동작하도록 처리
    
    # counts:dict = field(default_factory=dict, description="regx filter에 개별 config")
    counts:dict = field(default_factory=lambda:{"block": 0, "masking": 0, "accept": 0})
    spans: List[Tuple[int, int]] = field(default_factory=list)
    
    dictDetectRule: dict = field(default_factory=dict)
    
    #탐지된 룰 정보
    # detect_rule_list:list = []
    detect_rule_list: List = field(default_factory=list)    
    pass

# event 알람 메시지 관련
@dataclass(slots=True)
class EventAlarmMessage:
    
    '''
    Regex 패턴 요청, Filter로 요청
    '''
    
    time_stamp: str
    messageid: str
    
    user_id: str
    user_role: str
    email: str
    uuid : str
    
    ai_service : str
    prompt: str
    mode : str
    output_text : str    
    # pass
    
# regex 패턴, db등 별도 설정 관리
@dataclass(slots=True)    
class RegexPatternFilterConfig:
    
    '''
    '''
    
    prompt_size_limit:int =  1024 * 1024 # 프롬프트 사이즈 제한 (기본값 1MB)
    # pass