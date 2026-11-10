
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
    
    # SLM 모델 버전
    MODEL_VERSION_MTM_CPU = 1
    MODEL_VERSION_MTM_GPU = 2
    MODEL_VERSION_WINS_GPU = 3
    MODEL_VERSION_WINS_GPU_V4 = 4 #2026.07.13 모델 v4 추가 (사양 변경됨)
    
    GUARD_RAIL_DEFAULT = "NONE"
    
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
        model_version:int = int(self.__dictFilterLocalConfig.get("model_version"))
        
        #skip을 설정하고 호출했으면, 로그로 확인이 되어야 한다.
        if CONFIG_OPT_ENABLE == use_skip:
            # LOG().info("skip slm filter")
            return ERR_OK
        
        #TODO: 정책의 개입, 업데이트
        #TODO: 정책, 차단이 되면, 처음 탐지되는 정책으로 업데이트 한다.
        
        if SLMFilterPattern.MODEL_VERSION_MTM_CPU == model_version:
            
            # # 요청 패턴, 일단 개발, 향후 개선 (이정도로는 부족)
            # post:dict = {
            #     "model" : "cipherguard01", #TODO: 스마트엠투엠만 사용하는 model, 응답값이 같다.
            #     "messages" : [
            #         {
            #             "role" : "user",
            #             "content" : strPrompt    
            #         }                
            #     ],
            #     "temperature" : 0.0,
            #     "max_tokens" : 2048
            # }
            
            # header:dict = {
            #     "Content-Type" : "application/json"
            # }
            
            # dictSLMHttpResponse:dict = self.__requestToSLM(strURL, post, request_timeout, header)
            
            dictSLMHttpResponse:dict = self.__requestSmartMTMType(strURL, strPrompt, request_timeout)
            
            if None == dictSLMHttpResponse or 0 == len(dictSLMHttpResponse):
                LOG().info("fail request to slm, skip slm filter")
                return ERR_FAIL
        
            self.__parseSLMReponse(dictSLMHttpResponse, dictOuputResponse, dictSLMPolicyResult)
            
        elif SLMFilterPattern.MODEL_VERSION_MTM_GPU == model_version:
            
            #TODO: 스마트엠투엠 model은 요청 방식이 같다. 나중에 공통화
            
            dictSLMHttpResponse:dict = self.__requestSmartMTMType(strURL, strPrompt, request_timeout)
            
            if None == dictSLMHttpResponse or 0 == len(dictSLMHttpResponse):
                LOG().info("fail request to slm, skip slm filter")
                return ERR_FAIL
            
            self.__parseSLMReponseV2(dictSLMHttpResponse, dictOuputResponse, dictSLMPolicyResult)
            
        elif SLMFilterPattern.MODEL_VERSION_WINS_GPU == model_version:
            
            dictSLMHttpResponse:dict = self.__requestWinsType(strURL, strPrompt, request_timeout)
            
            if None == dictSLMHttpResponse or 0 == len(dictSLMHttpResponse):
                LOG().info("fail request to slm, skip slm filter")
                return ERR_FAIL
            
            self.__parseWinsGPUSLMReponse(dictSLMHttpResponse, dictOuputResponse, dictSLMPolicyResult)
            pass
        
        # 모델 v4, 일부 중복된 코드가 있으나, 공통화로 보완하되, 코드는 분리한다.
        elif SLMFilterPattern.MODEL_VERSION_WINS_GPU_V4 == model_version:
            
            dictSLMHttpResponse:dict = self.__requestWinsType(strURL, strPrompt, request_timeout)
            
            if None == dictSLMHttpResponse or 0 == len(dictSLMHttpResponse):
                LOG().info("fail request to slm, skip slm filter")
                return ERR_FAIL
            
            self.__parseWinsGPUV4SLMReponse(dictSLMHttpResponse, dictOuputResponse, dictSLMPolicyResult)
            
    
        
        return ERR_OK
    
    # 정책 DB 데이터 수신
    
    def notifyUpdateDBPatternPolicy(self, filterPolicyGroupData:FilterPolicyGroupData, dictOutputResponse:dict) -> int:
        
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
            
            dictOutputResponse[strFilterKey] = f"filter pattern policy is changed, rule count = {nRuleCount}"
            
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
            
        else:
            dictOutputResponse[strFilterKey] = f"filter pattern policy is equal (no changed)" 
        
        return ERR_OK
    
    
    ################################################ private
    
    # local config, 설정 정보 업데이트
    def __initializeLocalConfig(self, dictJsonLocalConfigRoot:dict, dictFilterLocalConfig:dict):
        
        '''        
        '''
        
        slm_pipeline_filter_module:dict = dictJsonLocalConfigRoot.get("slm_pipeline_filter_module")
        
        # 이값, 그대로 활용한다.
        dictFilterLocalConfig.update(slm_pipeline_filter_module)
        
        return ERR_OK
    
    # TODO: 그냥 반환하자.
    def __requestSmartMTMType(self, strURL:str, strPrompt:str, nRequestTimeOut:int):
        
        '''
        '''
        
        post:dict = {
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
        
        dictSLMHttpResponse:dict = self.__requestToSLM(strURL, post, nRequestTimeOut, header)
        
        if None == dictSLMHttpResponse:
            LOG().info("fail request to slm, skip slm filter")
            return None
        
        return dictSLMHttpResponse
    
    def __requestWinsType(self, strURL:str, strPrompt:str, nRequestTimeOut:int):
        
        '''
        '''
        
        post:dict = {
            
            "messages" : [
                {
                    "role" : "user",
                    "content" : strPrompt    
                }                
            ],
            "temperature" : 0,
            "max_tokens" : 3000,
            "repeat_penalty" : 1.15
        }
        
        header:dict = {
            "Content-Type" : "application/json"
        }
        
        dictSLMHttpResponse:dict = self.__requestToSLM(strURL, post, nRequestTimeOut, header)
        
        if None == dictSLMHttpResponse:
            LOG().info("fail request to slm, skip slm filter")
            return None
        
        return dictSLMHttpResponse
        
        # return ERR_OK
    
    #데이터 추출, 우선 여기 개발후 리펙토링
    def __parseSLMReponse(self, dictSLMHttpResponse:dict, dictOuputResponse:dict, dictSLMPolicyResult:dict):
        
        '''
        
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
                category:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_CATEGORY)

                #SLM의 응답에 대한 차단 결과와 별개로, 정책의 action이 존재한다.
                #정책의 action은 UI로 전달되며, 1차는 정책의 action으로 sslproxy의 결과를 제어한다.
                # rule:str = dictDBPattern.get("rule")
                action:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_ACTION)
                
                #그대로 저장, 향후 추가적인 정보가 필요하면 전체를 업데이트하는 방향으로 변경
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_ID] = id
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_NAME] = name
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_TARGET] = targets
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_CATEGORY] = category
                
                #TODO: 이 값이 중복으로 사용.. 분리해서 전달이 되거나, 가공되어야 한다.
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_ACTION] = action            
                # pass
            #pass
            
        # else: #차단이 아니면 일단 모두 safe
        
        return ERR_OK
    
    def __parseSLMReponseV2(self, dictSLMHttpResponse:dict, dictOuputResponse:dict, dictSLMPolicyResult:dict):
        
        '''        
        '''
        
        if None == dictSLMHttpResponse or 0 == len(dictSLMHttpResponse):
            LOG().error("invalid slm response, skip, pass allow")
            return ERR_FAIL
        
        choices:list = dictSLMHttpResponse.get(PipelineFilterDefine.SLM_RESONSE_CHOICE, [])
        
        if 0 == len(choices):
            return ERR_OK
       
        dictChoice:dict = choices[0]
        
        message:dict = dictChoice.get(PipelineFilterDefine.SLM_RESONSE_MESSAGE)
        content:str = message.get(PipelineFilterDefine.SLM_RESONSE_CONTENT)
        
        dictOuputResponse[ApiParameterDefine.OUT_SLM_CONTENT] = content
        
        strJsonContents:str = content.replace("```json", "").replace("```", "").strip()
        
        dictContents:dict = json.loads(strJsonContents)
        
        detected_items:list = dictContents.get("detected_items")
        
        bUnsafe:bool = False
        
        dictDetectedItem:dict = detected_items[0]
        
        dictDetectedItem.get("item")
        category:str = dictDetectedItem.get("category")
        confidence:int = dictDetectedItem.get("confidence")
        
        if not "없음" in category and 0.0 < confidence:
            bUnsafe = True
                
        if True == bUnsafe:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
            
            lstDBPattern:list = self.__dictDBScopeRegexPattern.get(DBDefine.POLICY_FILTER_SCOPE_DEFAULT)
            
            if 0 < len(lstDBPattern):
                dictDBPattern:dict = lstDBPattern[0]
                
                id:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_ID)
                name:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_NAME)
                targets:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_TARGET)
                category:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_CATEGORY)

                action:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_ACTION)
                
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_ID] = id
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_NAME] = name
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_TARGET] = targets
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_CATEGORY] = category
                
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_ACTION] = action            
                # pass
            #pass
        
        return ERR_OK
    
    
    def __parseWinsGPUSLMReponse(self, dictSLMHttpResponse:dict, dictOuputResponse:dict, dictSLMPolicyResult:dict):
        
        '''
        "choices": [
            {
                "finish_reason": "stop",
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "{\"has_pii\": false, \"is_abuse\": false, \"items\": []}"
                }
            }
        ]
        '''
        
        choices:list = dictSLMHttpResponse.get(PipelineFilterDefine.SLM_RESONSE_CHOICE, [])
        
        if 0 == len(choices):
            return ERR_OK
       
        dictChoice:dict = choices[0]
        
        message:dict = dictChoice.get(PipelineFilterDefine.SLM_RESONSE_MESSAGE)
        
        # TOOD: json 타입 문자열
        content:str = message.get(PipelineFilterDefine.SLM_RESONSE_CONTENT)
        
        dictOuputResponse[ApiParameterDefine.OUT_SLM_CONTENT] = content
        
        #TOOD: 오류 발생시, 예외처리 구간으로 빠진다. 별도 에외처리 안한다.
        dictContents:dict = json.loads(content)
        
        # 수집원본, 그대로 저장한다.
        
        has_pii:bool = dictContents.get("has_pii")
        is_abuse:bool = dictContents.get("is_abuse")
        
        # #TODO: 탐지가 되면 존재한다.
        
        # if 0 < len(items):
            
        #     for dictItem in items:
                
        #         type:str = dictItem.get("type")
        #         value:str = dictItem.get("type")
        
        # TODO: 차단 시점의 로직 체크 필요
        if True == has_pii or True == is_abuse:

            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
            
            lstDBPattern:list = self.__dictDBScopeRegexPattern.get(DBDefine.POLICY_FILTER_SCOPE_DEFAULT)
            
            # evidence는 항상 추가
            # evidence 추가, 응답 메시지에 출력
            # items:list = dictContents.get("items")
            dictSLMPolicyResult[SLMDetectDefine.SLM_EVIDENCE] = dictContents.get("items")
            
            if 0 < len(lstDBPattern):
                
                # 정책이 존재하면, 첫번째 정책으로 업데이트.
                dictDBPattern:dict = lstDBPattern[0]
                
                # 전부 불필요
                # id:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_ID)
                # name:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_NAME)
                # targets:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_TARGET)
                # category:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_CATEGORY)

                action:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_ACTION)

                # db정책, 불필요                
                # dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_ID] = id
                # dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_NAME] = name
                # dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_TARGET] = targets
                # dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_CATEGORY] = category
                
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_ACTION] = action            
                
            else: #정책이 없으면, 무조건 차단
                
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_ACTION] = PipelineFilterDefine.ACTION_BLOCK
                
        
        return ERR_OK
    
    
    # Wins Model - V4 버전 (26.07.13)
    def __parseWinsGPUV4SLMReponse(self, dictSLMHttpResponse:dict, dictOuputResponse:dict, dictSLMPolicyResult:dict):
        
        '''
        {
            "has_pii" : true,
            "is_abuse" : false,
            "guardrail" : "NONE",
            "items: : [
                {
                    "type" : "PASSWORD",
                    "value" : "service!!"
                }
            ]
        }
        '''
        
        #TODO: 중복된 코드, 나중에 개선
        
        choices:list = dictSLMHttpResponse.get(PipelineFilterDefine.SLM_RESONSE_CHOICE, [])
        
        if 0 == len(choices):
            return ERR_OK
       
        dictChoice:dict = choices[0]
        
        message:dict = dictChoice.get(PipelineFilterDefine.SLM_RESONSE_MESSAGE)
        
        # TOOD: json 타입 문자열
        content:str = message.get(PipelineFilterDefine.SLM_RESONSE_CONTENT)
        
        dictOuputResponse[ApiParameterDefine.OUT_SLM_CONTENT] = content
        
        #TOOD: 오류 발생시, 예외처리 구간으로 빠진다. 별도 에외처리 안한다.
        dictContents:dict = json.loads(content)
        
        # 수집원본, 그대로 저장한다.
        
        has_pii:bool = dictContents.get("has_pii")
        is_abuse:bool = dictContents.get("is_abuse")
        
        #TODO: PII일때만 items 값이 수집된다.
        
        # 위반시 프롬프트 인젝션으로 표기된다.
        guardrail:str = dictContents.get("guardrail") #실제 guard rail 카테고리가 추출된다. (어떻게 표현할지는 향후 고민)
        
        # PII 위반, 또는 guard rail
        if True == has_pii or True == is_abuse or SLMFilterPattern.GUARD_RAIL_DEFAULT != guardrail:

            #pii, 악성/욕설/유해, guardrail 이면 차단
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
            
            lstDBPattern:list = self.__dictDBScopeRegexPattern.get(DBDefine.POLICY_FILTER_SCOPE_DEFAULT)
            
            # evidence는 항상 추가
            # evidence 추가, 응답 메시지에 출력
            # items:list = dictContents.get("items")
            
            lstPiiItem:list = dictContents.get("items")
            
            if 0 < len (lstPiiItem):
            
                dictSLMPolicyResult[SLMDetectDefine.SLM_EVIDENCE] = dictContents.get("items")
                
            elif None != guardrail and 0 < len(guardrail):
                
                # guard rail 일때, evidence를 맞춰 준다.
                dictSLMPolicyResult[SLMDetectDefine.SLM_EVIDENCE] = [
                    {
                        "type" : guardrail,
                        "value" : "" 
                    }
                ]
            
            # else:
            #     # 없을수 있다.
            
            if 0 < len(lstDBPattern):
                
                # 정책이 존재하면, 첫번째 정책으로 업데이트.
                dictDBPattern:dict = lstDBPattern[0]

                action:str = dictDBPattern.get(DBDefine.DB_FIELD_RULE_ACTION)
                
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_ACTION] = action            
                
            else: #정책이 없으면, 무조건 차단
                
                dictSLMPolicyResult[DBDefine.DB_FIELD_RULE_ACTION] = PipelineFilterDefine.ACTION_BLOCK
                # pass
        
        return ERR_OK
    
    
    # SLM, API 요청
    def __requestToSLM(self, strURL:str, dictJsonTypePost:dict, nRequestTimeOut:int, dictHeader:dict) -> dict:
        
        '''
        '''
        
        try:
            
            resp = requests.post(strURL, json=dictJsonTypePost, timeout=nRequestTimeOut, headers=dictHeader)
        
            # 4xx, 5xx 일때 오류 발생
            resp.raise_for_status()
            
            dictSLMHttpResponse:dict = resp.json()
            
            return dictSLMHttpResponse
            
        except requests.exceptions.Timeout:
            LOG().error("fail request to slm, time out exception")
            
        except requests.exceptions.ConnectionError:            
            LOG().error("fail request to slm, connect error")
            
        except requests.exceptions.HTTPError as e:
            LOG().error(f"fail request to slm, http error {e}")
            
        # except requests.exceptions.RequestException as e:
        #     print(f"기타 오류: {e}")
        except Exception as e:            
            LOG().error(traceback.format_exc())
                        
        return None
        
        
        
