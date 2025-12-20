
#외부 라이브러리
from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.helper.filter_pattern_base import FilterPatternBase

# 그룹별 regex filter
from block_filter_modules.filter_policy.groupfilter.filter_policy_group_data import FilterPolicyGroupData

# 별도 helper
from block_filter_modules.filter_pattern.helper.regex_policy_helper.regex_policy_generate_helper import RegexPolicygenerateHelper

'''
slm 정책, 기존 regex db와 동일한 패턴으로 관리
다만 정책은 1개만 추가된다.
'''

class SLMFilterPattern (FilterPatternBase):
    
    POLICY_FILTER_KEY = DBDefine.FILTER_KEY_SLM
    
    def __init__(self):
        
        super().__init__()
        
        #regex 패턴, scope 단위로 관리
        # regex, slm, file 모두 같은 패턴으로 관리
        self.__dictDBScopeRegexPattern:dict = None
        
        #helper 추가
        self.__regexPolicyGenerateHelper:RegexPolicygenerateHelper = None
        
        # valve 개념, dictionary 정도로..
        self.__dictFilterLocalConfig:dict = None
        pass
    
    def Initialize(self, dictJsonLocalConfigRoot:dict):
        
        '''
        '''
        
        self.__dictDBScopeRegexPattern:dict = {
            DBDefine.POLICY_FILTER_SCOPE_USER : [],
            DBDefine.POLICY_FILTER_SCOPE_SERVICE : [],
            DBDefine.POLICY_FILTER_SCOPE_GROUP : [],
            DBDefine.POLICY_FILTER_SCOPE_DEFAULT : []
        }
        
        self.__regexPolicyGenerateHelper:RegexPolicygenerateHelper = RegexPolicygenerateHelper()
        
        # local config에서 slm등 설정값을 가져오며 향후 확장을 고려한다.
        self.__dictFilterLocalConfig:dict = {}
        
        self.__initializeLocalConfig(dictJsonLocalConfigRoot, self.__dictFilterLocalConfig)
        
        return ERR_OK
    
    # 패턴 탐지
    def DetectPattern(self, strPrompt:str, dictOuputResponse:dict, dictSLMPolicyResult:dict):
        
        '''
        slm 정책, 단순 http post 요청
        탐지 패턴과 정책의 연관은 없고, 하나의 정책의 설정값을 그대로 전달한다... 
        -> 뭔가 논리력 부족, 다시 논의 필요
        '''
        
        # slm 서버 접속 URL
        # strURL:str = "http://127.0.0.1:1200/v1/chat/completions"
        # timeout:int = 60 # 오래 걸릴수 있다. 우선 60초 timeout
        
        strURL:str = self.__dictFilterLocalConfig.get("slm_url")
        request_timeout:int = int(self.__dictFilterLocalConfig.get("request_timeout"))
        
        #SLM 성능이슈, 기본은 비활성이고, 비활성 상태이면 강제로 allow를 반환한다.
        use_skip:int = int(self.__dictFilterLocalConfig.get("use_skip"))
        
        #skip을 설정하고 호출했으면, 로그로 확인이 되어야 한다.
        if CONFIG_OPT_ENABLE == use_skip:
            LOG().info("skip slm filter")
            return ERR_OK
        
        # 요청 패턴, 일단 개발, 향후 개선 (이정도로는 부족)
        post = {
            "model" : "cipherguard01",
            "messages" : [
                {
                    "role" : "user",
                    "content" : strPrompt    
                }                
            ],
            "temperature" : 0.0,
            "max_tokens" : 2048
        }
        
        header:dict = {
            "Content-Type" : "application/json"
        }
        
        resp = requests.post(strURL, json=post, timeout=request_timeout, headers=header)
        resp.raise_for_status()
        
        dictSLMHttpResponse:dict = resp.json()
        
        #TODO: 정책의 개입, 업데이트
        #TODO: 정책, 차단이 되면, 처음 탐지되는 정책으로 업데이트 한다.

        #응답 문자열 파싱, 결과 데이터 저장        
        self.__parseSLMReponse(dictSLMHttpResponse, dictOuputResponse, dictSLMPolicyResult)
        
        return ERR_OK
    
    # 정책 DB 데이터 수신
    
    def notifyUpdateDBPatternPolicy(self, filterPolicyGroupData:FilterPolicyGroupData) -> int:
        
        '''
        '''
        
        #test, regex 패턴으로 변경
        # strFilterKey:str = DBDefine.FILTER_KEY_REGEX
        strFilterKey:str = SLMFilterPattern.POLICY_FILTER_KEY
        
        dictPolicyRuleScopeMap:dict = filterPolicyGroupData.GetPolicyRule(strFilterKey)
        
        bFilterChanged:bool = self.IsScopeBasedFilterPolicyChanged(dictPolicyRuleScopeMap)
        
        if FilterPatternBase.POLICY_CHANGED == bFilterChanged:
            
            # 로깅, 중요, 향후 감사로그
            nRuleCount = filterPolicyGroupData.GetRuleCount(strFilterKey)
            LOG().info(f"filter pattern policy is changed, filter = {strFilterKey}, rule count = {nRuleCount}")
            
            # 먼저 초기화
            self.__dictDBScopeRegexPattern:dict = {
                DBDefine.POLICY_FILTER_SCOPE_USER : [],
                DBDefine.POLICY_FILTER_SCOPE_SERVICE : [],
                DBDefine.POLICY_FILTER_SCOPE_GROUP : [],
                DBDefine.POLICY_FILTER_SCOPE_DEFAULT : []
            }
            
            self.UpdateBaseDBFilterPolicy(dictPolicyRuleScopeMap)
            
            self.__regexPolicyGenerateHelper.GenerateRegexGroupPolicy(dictPolicyRuleScopeMap, self.__dictDBScopeRegexPattern)
            #pass
        
        return ERR_OK
    
    
    ################################################ private
    
    # local config, 설정 정보 업데이트
    def __initializeLocalConfig(self, dictJsonLocalConfigRoot:dict, dictFilterLocalConfig:dict):
        
        '''
        "slm_pipelie_filter_module":
        {
            "slm_url" : "http://127.0.0.1:1200/v1/chat/completions",
            "request_timeout" : 60
        }
        '''
        
        slm_pipelie_filter_module:dict = dictJsonLocalConfigRoot.get("slm_pipelie_filter_module")
        
        # 이값, 그대로 활용한다.
        dictFilterLocalConfig.update(slm_pipelie_filter_module)
        
        return ERR_OK
    
    #데이터 추출, 우선 여기 개발후 리펙토링
    def __parseSLMReponse(self, dictSLMHttpResponse:dict, dictOuputResponse:dict, dictSLMPolicyResult:dict):
        
        '''
        데이터 오류, 또는 반환값이 없으면 allow
        choices/message/content 를 수집한다. 나머지는 아직 불필요
        content 안에 Safe 이면 allow, Unsafe 이면 block
        카테고리 정보는 우선 수집하지 않고, 별도 파싱도 하지 않는다.
        
        {
            "choices": [
                {
                "finish_reason": "stop",
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Safety: Safe\nCategories: None"
                }
                # 민감정보이면
                Safety: Unsafe\nCategories: PII
                }
            ],
            "created": 1765936456,
            "model": "cipher-guard-current.gguf",
            "system_fingerprint": "b7361-a81a56957",
            "object": "chat.completion",
            "usage": {
                "completion_tokens": 8,
                "prompt_tokens": 312,
                "total_tokens": 320
            },
            "id": "chatcmpl-85yqyzUyBhN4oqEd65wBCbxrT2XHGiqr",
            "timings": {
                "cache_n": 311,
                "prompt_n": 1,
                "prompt_ms": 109.252,
                "prompt_per_token_ms": 109.252,
                "prompt_per_second": 9.15315051440706,
                "predicted_n": 8,
                "predicted_ms": 782.808,
                "predicted_per_token_ms": 97.851,
                "predicted_per_second": 10.21961962575753
            }
            }
        '''
        
        # 기본 데이터 초기화 => 별도 응답 대신, 최종 응답에 추가
        # dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
        
        
        # 기본 예외처리, 응답이 없으면 allow, content는 공백
        if None == dictSLMHttpResponse or 0 == len(dictSLMHttpResponse):
            LOG().error("invalid slm response, skip, pass allow")
            return ERR_OK
        
        choices:list = dictSLMHttpResponse.get(PipelineFilterDefine.SLM_RESONSE_CHOICE, [])
        
        if 0 == len(choices):
            return ERR_OK
       
        #choice 안에, message 안에, contents
        dictChoice:dict = choices[0]
        
        message:dict = dictChoice.get(PipelineFilterDefine.SLM_RESONSE_MESSAGE)
        content:str = message.get(PipelineFilterDefine.SLM_RESONSE_CONTENT)
        
        dictOuputResponse[ApiParameterDefine.OUT_SLM_CONTENT] = content
        
        # 차단여부
        
        if PipelineFilterDefine.SLM_RESPONSE_UNSAFE in content:
            
            #TODO: 이값이 필요없을수 있다.
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
            
            # 차단이 되면, DB에 저장된 첫번째 정책을 업데이트 한다.
            lstDBPattern:list = self.__dictDBScopeRegexPattern.get(DBDefine.POLICY_FILTER_SCOPE_DEFAULT)
            
            #데이터가 존재하면, 없으면 공백.
            if 0 < len(lstDBPattern):
                dictDBPattern:dict = lstDBPattern[0]
                
                id:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_ID)
                name:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_NAME)
                targets:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_TARGET)

                #SLM의 응답에 대한 차단 결과와 별개로, 정책의 action이 존재한다.
                #정책의 action은 UI로 전달되며, 1차는 정책의 action으로 sslproxy의 결과를 제어한다.
                # rule:str = dictDBPattern.get("rule")
                action:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_ACTION)
                
                #그대로 저장, 향후 추가적인 정보가 필요하면 전체를 업데이트하는 방향으로 변경
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_ID] = id
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_NAME] = name
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_TARGET] = targets
                
                #TODO: 이 값이 중복으로 사용.. 분리해서 전달이 되거나, 가공되어야 한다.
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_ACTION] = action            
                # pass
            #pass
            
        # else: #차단이 아니면 일단 모두 safe
        
        return ERR_OK
        
        
        
