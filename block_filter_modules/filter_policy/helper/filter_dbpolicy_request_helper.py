

#rule, base64
import base64
import copy

#외부 라이브러리
from lib_include import *

from type_hint import *

from block_filter_modules.filter_policy.groupfilter.filter_policy_group_data import FilterPolicyGroupData

'''
Filter 정책 조회 모듈, http 요청, 응답 json 데이터만 받는 형태로 개발
'''

class FilterDBPolicyRequestHelper:
    
    def __init__(self):
        pass
      
    #DB 정책 데이터 조회
    def RequestFilterDBPolicyGroup(self, filterPolicyGroupData:FilterPolicyGroupData):
      
      '''
      filters 테이블을 조회한다.
      filters 별 Rule을 조회한다. (1차)
      향후 사용자, 그룹등 추가 depth로 확장한다. (향후)      
      
      TODO: 프로그램으로 할건지, DB 쿼리로 할건지 결정.
      1차는 DB 쿼리로 가능, filter 키를 조회하고, filter 키 별로 loop 필요
      다만, filter 키는 어디선가 관리 필요.
      '''
      
      # 정책을 조회한다. 개별 조회
      dictDBPolicyRuleResult = {}
      '''
      select id, name, targets, type_mask, operator, rule, regex_flag, regex_group, regex_group_val, action, status from app.policy_rules where status = 'deployed' order by action desc
      '''
      sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_policy_rule", {}, dictDBPolicyRuleResult)
      
      #정책에 대해서, 변환된 map을 생성한다. 
      lstDBPolicyRule:list = dictDBPolicyRuleResult.get(DBSQLDefine.QUERY_DATA)
      
      dictPolicyRuleIDMap:dict = {}      
      self.__generatePolicyRuleMap(lstDBPolicyRule, dictPolicyRuleIDMap)
      
      # 계정 정보를 조회한다. 서비스 정보는 우선 상수로 관리
      # 사용성을 위해서, ID기준으로 map을 생성
      dictDBUserResult = {}
      sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_ai_user_account", {"where" : ""}, dictDBUserResult)
        
      lstDBUserInfo:list = dictDBUserResult.get(DBSQLDefine.QUERY_DATA)
      
      dictUserIDMap:dict = {}
      self.__generateUserInfoMap(lstDBUserInfo, dictUserIDMap)
      
      #filter 를 조회한다.
      dictDBFilterResult = {}
      sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_policy_filters", {}, dictDBFilterResult)
      
      #AI service Map
      dictAIServiceNameMap:dict = AI_SERVICE_NAME_MAP
      
      lstFilterID:list = dictDBFilterResult.get(DBSQLDefine.QUERY_DATA)
      
      # filter 별로 각 scope에 대한 정책을 만든다.
      for dictFilterID in lstFilterID:
          
        # id:str = dictPattern.get("id")
        # name:str = dictPattern.get("name")
        # type_mask:int = dictPattern.get("type_mask")
        # operator:str = dictPattern.get("operator")
        strFilterID:str = dictFilterID.get("id")
        
        
        #각 filter id별로, policy group을 조회한다. 이름은 DB테이블명을 따라간다.
        dictPolicyRuleFilterResult = {}
        # '''
        # select id, filter_id, scope, policy_rule_id, subject_id from app.policy_rule_filters where filter_id = '{filter_id}'
        # '''
        sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_policy_rule_filters", {"filter_id" : strFilterID}, dictPolicyRuleFilterResult)
        
        #1단계 - 2depth의 자료 구조로 저장
        lstPolicyRuleFilter:list = dictPolicyRuleFilterResult.get(DBSQLDefine.QUERY_DATA)
        
        #데이터 확인, 없으면 skip
        if 0 == len(lstPolicyRuleFilter):
          #테스트용 로그 
          # LOG().debug(f"skip update filter policy, filter = {strFilterID}, no data")
          continue
        
        filterPolicyGroupData.ClearPolicyRule(strFilterID)
        
        #TODO: 데이터 보정 기능이 필요하다. 모듈화. 하나의 단위별로 loop
        # lstNewPolicyRule:list = []
        
        # 개별 필터에 대해서, scope별로 Rule을 저장한다.
        #TODO: scope 범위로, 미리 생성한다., list를 만들어야 한다.
        dictPolicyRuleScopeMap = {
          DBDefine.POLICY_FILTER_SCOPE_USER : [],
          DBDefine.POLICY_FILTER_SCOPE_SERVICE : [],
          DBDefine.POLICY_FILTER_SCOPE_GROUP : [],
          DBDefine.POLICY_FILTER_SCOPE_DEFAULT : [],
        }        
        self.__generateFilterScopeMap(dictPolicyRuleIDMap, lstPolicyRuleFilter, dictPolicyRuleScopeMap, dictUserIDMap, dictAIServiceNameMap)
        
        #filterid 별 정책 추가.
        filterPolicyGroupData.AddPolicyRule(strFilterID, dictPolicyRuleScopeMap)                
        # pass
      
      return ERR_OK
    
    # 파일명 정보, DB 조회
    def RequestFileBlockPolicy(self, dictFileBlockPolicy:dict):
    
      '''
      시간상, 우선 빠르게 개발, 향후 2차 리펙토링
      TODO: 중복값이면 설정 안되게 해야 한다. 필요시 최초 로딩시에만 가져오도록 한다.
      '''
      
      dictDBUserResult = {}
      sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_file_name_block_policy", {}, dictDBUserResult)
        
      lstFileBlockInfo:list = dictDBUserResult.get(DBSQLDefine.QUERY_DATA)
      
      for dictFileBlockInfo in lstFileBlockInfo:

          skey:str = dictFileBlockInfo.get("skey")
          svalue:str = dictFileBlockInfo.get("svalue")
          
          # 허용되는 확장자 - , 로 구분한다.
          if "fileControlAllowedExtensions" == skey:
            lstExtension:list = svalue.split(",")
            
            dictFileBlockPolicy[FileDefine.DB_POLICY_FILE_BLOCK_ALLOW_EXT] = lstExtension
            # pass
          # 최대 크기
          elif "fileControlMaxSize" == skey:
            dictFileBlockPolicy[FileDefine.DB_POLICY_FILE_BLOCK_MAX_SIZE] = int(svalue)
            pass
      
      return ERR_OK
    
      
  ######################################################### private
  
    # 사용자 계정, ID기반 검색을 위한 Map을 생성한다.
    def __generateUserInfoMap(self, lstDBUserInfo:list, dictUserInfoMap:dict):
      '''
      '''
      
      for dictUserInfo in lstDBUserInfo:
        
        id:str = dictUserInfo.get("id")
        
        dictUserInfoMap[id] = dictUserInfo
      
      return ERR_OK
  
    #개별 정책에 대한 scope map을 생성한다.
    def __generateFilterScopeMap(self, dictPolicyRuleIDMap:dict, lstPolicyRuleFilter:list, dictPolicyRuleScopeMap:dict, dictUserIDMap:dict, dictAIServiceNameMap:dict):
      
      '''
      '''
      
      for dictDBPolicyRuleFilter in lstPolicyRuleFilter:
          
          #여기서, scope 별로 분기, filter에 대해서 scope 별로 정책 관리
          
          #TODO: 우선 개발후 리펙토링
          
          # dictDBPolicyRuleFilter.get("id")
          # dictDBPolicyRuleFilter.get("filter_id")
          scope = dictDBPolicyRuleFilter.get("scope")
          policy_rule_id = dictDBPolicyRuleFilter.get("policy_rule_id")
          
          #TODO: subject_id는 저장만 하고, 탐지 시점에 찾아야 하는 방안의 검토.
          subject_id = dictDBPolicyRuleFilter.get(DBDefine.DB_FIELD_SUBJECT_ID)
          
          #TODO: scope가 있던 없던, policy_rule_id는 존재한다., 예외처리는 필요
          #policy_rule_id로 정책을 가져온다.
          
          dictPolicyRule:dict = dictPolicyRuleIDMap.get(policy_rule_id)
          
          #TODO: 예외처리, 정책에 해당하는 룰이 없을수 있다. (잘못된 매핑)
          if None == dictPolicyRule:
            LOG().error(f"invalid policy rule (not exist), policy_rule_id = {policy_rule_id}, scope = {scope}")
            continue
          
          #중간 버퍼를 두고 scope별로 map을 관리.
          #TODO: 중복으로 사용가능하고, subject_id등 추가적인 정보가 필요하다.
          #각 정책에 대해서 복사
          dictNewPolicyRule:dict = copy.deepcopy(dictPolicyRule)
          
          #default가 아니면, subject_id를 저장한다. => 가져오는게 아니라, 부가 정보를 각각 저장해야 할수도 있다.
          #여기에 대한 처리 필요.
          if scope != DBDefine.POLICY_FILTER_SCOPE_DEFAULT:
            dictNewPolicyRule[DBDefine.DB_FIELD_SUBJECT_ID] = subject_id
            
            self.__updateReplaceSubjectIDValue(dictNewPolicyRule, scope, subject_id, dictUserIDMap, dictAIServiceNameMap)
            # pass
          
          
          #여기는 list로 담는다.
          #TODO: scope가 에상 외의 값이 추가되면, 한번쯤 점검해야 한다. range 오류체크는 우선 무시.
          dictPolicyRuleScopeMap[scope].append(dictNewPolicyRule)
          
      return ERR_OK
    
    #subject_id에 따른 분기, ID, Value 업데이트
    def __updateReplaceSubjectIDValue(self, dictNewPolicyRule:dict, strScope:str, strSubjectID:str, dictUserInfoMap:dict, dictNameServiceMap:dict):
      
      '''
      scope가 user => userid 업데이트
      service => serviceid 업데이트
      group => 미정
      default, 해당 없음.
      '''
      
      #ID는 DB값을 추가, join 대체.
      dictNewPolicyRule[DBDefine.DB_FIELD_SUBJECT_ID] = strSubjectID
      
      if DBDefine.POLICY_FILTER_SCOPE_USER == strScope:
        
        #TODO: 향후 디버깅 관련해서 검토 필요.
        
        #일단, ID에 매칭되는 user 정보를 전달한다.
        dictUserInfo:dict = dictUserInfoMap.get(strSubjectID)
        
        dictNewPolicyRule[DBDefine.DB_FIELD_SUBJECT_VAL] = dictUserInfo
        
      # elif DBDefine.POLICY_FILTER_SCOPE_SERVICE == strScope:
        
      #   # 서비스, 서비스 ID만 있으면 된다.
        
      # elif DBDefine.POLICY_FILTER_SCOPE_GROUP == strScope:
        
      #   # 미정
      #   pass
      # # elif DBDefine.POLICY_FILTER_SCOPE_DEFAULT
      
      return ERR_OK
  
    #비교를 위해서 정책을 ID 기반 hashmap으로 생성한다.
    def __generatePolicyRuleMap(self, lstDBPolicyRule:list, dictPolicyRuleIDMap:dict):
      
      '''
      '''
      
      for dictDBPolicyRule in lstDBPolicyRule:
        
        #정책 id, id로 dictionary를 만든다.
        id:str = dictDBPolicyRule.get("id")
        
        nError = self.__convertFilterRule(dictDBPolicyRule)
        
        if ERR_FAIL == nError:
          
          LOG().error(f"fail convert rule, id = {id}, skip insert")
          continue
        
        dictPolicyRuleIDMap[id] = dictDBPolicyRule
        # pass
      
      return ERR_OK
  
    #수집된 정책, convert
    def __convertFilterRule(self, dictDBFilterRule:dict):
      '''
      '''
      
      try:
        
        # id:str = dictPattern.get("id")
        # name:str = dictPattern.get("name")
        # type_mask:int = dictPattern.get("type_mask")
        # operator:str = dictPattern.get("operator")
        rule:str = dictDBFilterRule.get("rule")
        # action:str = dictPattern.get("action")
        # status:str = dictPattern.get("status")
        
        regex_flag:int = dictDBFilterRule.get("regex_flag")
        regex_group:int = dictDBFilterRule.get("regex_group")
        regex_group_val:str = dictDBFilterRule.get("regex_group_val")
        
        #이름 보정
        dictDBFilterRule["regexFlag"] = regex_flag
        dictDBFilterRule["regexGroup"] = regex_group
        dictDBFilterRule["regexGroupVal"] = regex_group_val
          
        #TODO: action, base64 => decode 처리후 저장.
          
        #TODO: base64 인코딩 실패의 예외처리 포함, 예외 발생시, 해당 룰을 제외하고 나머지 룰만 처리
        byteBase64Decode = base64.b64decode(rule)
          
        #문자열로 변환
        strBase64Decode = byteBase64Decode.decode("utf-8")
          
        #어차피 여기서만 조회, 그냥 업데이트
        dictDBFilterRule["rule"] = strBase64Decode
          
        #그 연산이 그연산..
        # dictNewPattern = {
        #   "id" : id
        # }
        
      except Exception as err:
        
        LOG().error(traceback.format_exc())
        
        #TODO: 연산이 실패한 룰은, 무시한다.
        return ERR_FAIL
      
    
      return ERR_OK
      
      
  ############################################### 지울 코드
  
    # #DB 정책 조회 버전2 - DB에서 직접 조회
    # def RequestToDBPolicy(self, dictFilterPolicy:dict, dictPolicyLocalConfig:dict):
      
    #   '''
    #   sqlprintf 구조로 변경한다.
    #   조회된 결과에서 기준 데이터 구조와 동일한 dictionary 형태로 업데이트
      
    #   '''
      
    #   '''
    #   select id, name, type_mask, operator, rule, action, regex_flag, regex_group, regex_group_val, status from policy_rules where status = 'deployed'
    #   '''
    #   dictDBResult = {}
    #   sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_policy_rule", {}, dictDBResult)
      
    #   lstQueryData:list = dictDBResult.get(DBSQLDefine.QUERY_DATA)
      
    #   #같은 구조를 유지, 그대로 복사한다. (혹시 몰라 deep copy)  
    #   #TODO: 키구조 주의, 그대로 넣으면 안된다. 사양 파악후 정리.      
    #   # 키를 생성해서, 다시 만든다. => DB에서 만든다. 그래도, 바로 복사는 안된다.
    #   # self.__dictCurrentUserInfo:dict = copy.deepcopy(dictQueryData)
      
    #   lstNewFilterPolicy = []
      
    #   #일단 하나로 처리하자.
    #   for dictPattern in lstQueryData:
          
    #     '''
    #     # id:str = dictPattern.get("id")
    #     # name:str = dictPattern.get("name")
    #     # type_mask:int = dictPattern.get("type_mask")
    #     # operator:str = dictPattern.get("operator")
    #     rule:str = dictPattern.get("rule")
    #     # action:str = dictPattern.get("action")
    #     # status:str = dictPattern.get("status")
        
    #     regex_flag:int = dictPattern.get("regex_flag")
    #     regex_group:int = dictPattern.get("regex_group")
    #     regex_group_val:str = dictPattern.get("regex_group_val")
        
    #     #이름 보정
    #     dictPattern["regexFlag"] = regex_flag
    #     dictPattern["regexGroup"] = regex_group
    #     dictPattern["regexGroupVal"] = regex_group_val
          
    #     #TODO: action, base64 => decode 처리후 저장.
          
    #     byteBase64Decode = base64.b64decode(rule)
          
    #     #문자열로 변환
    #     strBase64Decode = byteBase64Decode.decode("utf-8")
          
    #     #어차피 여기서만 조회, 그냥 업데이트
    #     dictPattern["rule"] = strBase64Decode
          
    #     #그 연산이 그연산..
    #     # dictNewPattern = {
    #     #   "id" : id
    #     # }
    #     '''
        
    #     self.__convertFilterRule(dictPattern)
          
    #     lstNewFilterPolicy.append(dictPattern)
    #     #pass
        
    #   dictFilterPolicy["data"] = lstNewFilterPolicy
      
    #   return ERR_OK
  
    # #가상의 테스트 코드, 다음의 데이터가 반환되도록 처리
    # def testRequestToDBPolicy(self, dictFilterPolicy:dict, dictPolicyLocalConfig:dict):
      
    #   '''
    #   '''
      
    #   dictLocalVal = {
        
    #     "data" : [
    #       {
    #         "name": "PEM 패턴",                          
    #         "rule": "-----BEGIN (?P<K>[^-\r\n]+?) KEY-----[\s\S]+?-----END (?P=K) KEY-----",              
    #         "action": "block",
    #         "regex_flag" : 16,
    #         "regex_group" : 0,
    #         "regex_group_val" : None,              
    #       },
          
    #       {
    #         "name": "JWT",                          
    #         "rule": "\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",              
    #         "action": "masking",
    #         "regex_flag" : 0,
    #         "regex_group" : 0,
    #         "regex_group_val" : None              
    #       },
          
    #       {
    #         "name": "aws_access_key_id",                          
    #         "rule": "\b(?:AKIA|ASIA|ANPA|ABIA)[0-9A-Z]{16}\b",              
    #         "action": "block",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : None              
    #       },
          
    #       {
    #         "name": "aws_secret_access_key",                          
    #         "rule": "(?<![A-Za-z0-9/+=])([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])",              
    #         "action": "masking",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : None              
    #       },
          
    #       {
    #         "name": "azure_storage_account_key",                          
    #         "rule": "(?i)\bAccountKey=(?P<VAL>[A-Za-z0-9+/=]{30,})",              
    #         "action": "block",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"              
    #       },
          
    #       {
    #         "name": "azure_conn_string",                          
    #         "rule": "(?i)\bDefaultEndpointsProtocol=\w+;AccountName=\w+;AccountKey=(?P<VAL>[A-Za-z0-9+/=]{30,})",              
    #         "action": "masking",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"              
    #       },
          
    #       {
    #         "name": "basic_auth_creds",                          
    #         "rule": "(?i)\b(?:https?|ftp|ssh)://(?P<CREDS>[^:@\s/]+:[^@\s/]+)@",
    #         "action": "block",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "CREDS"
    #       },
          
    #       {
    #         "name": "cloudant_creds",                          
    #         "rule": "(?i)https?://(?P<CREDS>[^:@\s/]+:[^@\s/]+)@[^/\s]*\.cloudant\.com",
    #         "action": "masking",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "CREDS"              
    #       },
          
    #       {
    #         "name": "discord_bot_token",                          
    #         "rule": "\b(?P<VAL>[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27})\b",
    #         "action": "block",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"              
    #       },
          
    #       {
    #         "name": "github_token",                          
    #         "rule": "\b(?P<VAL>(?:ghp|gho|ghu|ghs|ghr)[-_][A-Za-z0-9]{16,})\b",
    #         "action": "masking",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"              
    #       },
          
    #       {
    #         "name": "mailchimp_api_key",                          
    #         "rule": "\b(?P<VAL>[0-9a-f]{32}-us\d{1,2})\b",
    #         "action": "block",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"              
    #       },
          
    #       {
    #         "name": "slack_token",                          
    #         "rule": "\b(?P<VAL>xox[abprs]-[A-Za-z0-9-]{10,})\b",
    #         "action": "masking",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"              
    #       },
          
    #       {
    #         "name": "slack_webhook_path",                          
    #         "rule": "(?i)https://hooks\.slack\.com/services/(?P<VAL>T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+)",
    #         "action": "block",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"              
    #       },
          
    #       {
    #         "name": "stripe_secret",                          
    #         "rule": "\b(?P<VAL>sk_(?:live|test)_[A-Za-z0-9]{16,})\b",
    #         "action": "masking",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"
    #       },
          
    #       {
    #         "name": "stripe_publishable",                          
    #         "rule": "\b(?P<VAL>pk_(?:live|test)_[A-Za-z0-9]{16,})\b",
    #         "action": "block",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"              
    #       },
          
    #       {
    #         "name": "twilio_account_sid",                          
    #         "rule": "\b(?P<VAL>AC[0-9a-fA-F]{32})\b",
    #         "action": "masking",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"              
    #       },
          
    #       {
    #         "name": "twilio_auth_token",                          
    #         "rule": "(?<![A-Za-z0-9])(?P<VAL>[0-9a-fA-F]{32})(?![A-Za-z0-9])",
    #         "action": "block",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"              
    #       },
          
    #       {
    #         "name": "openai_like",                          
    #         "rule": "\b(?P<VAL>sk-[A-Za-z0-9]{16,})\b",
    #         "action": "masking",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"              
    #       },
          
    #       {
    #         "name": "ak_tk_token",                          
    #         "rule": "\b(?P<VAL>(?:ak|tk)-[a-f0-9]{16,}(?:-(?:dev|test)[a-z0-9]*)?)\b",
    #         "action": "block",
    #         "regex_flag" : 0,
    #         "regex_group" : 1,
    #         "regex_group_val" : "VAL"              
    #       },
          
    #       {
    #         "name": "candidate",                          
    #         "rule": "[A-Za-z0-9+/=._\-]{16,}",
    #         "action": "masking",
    #         "regex_flag" : 0,
    #         "regex_group" : 0,
    #         "regex_group_val" : None
    #       }
          
    #     ]  
    #   }
      
    #   dictFilterPolicy.update(dictLocalVal)

    #   return ERR_OK
  
  #DB 정책 조회, TODO: 하드코딩 => 잠시 제거
#     def testRequestToDBPolicy(self, dictFilterPolicy:dict, dictPolicyLocalConfig:dict):
        
#         '''
#         다음 요청, python request 로 수신후, json 데이터를 반환한다.
#         curl -X GET 'http://10.0.17.101:3000/api/internal/policy/rules?sdate=2025-10-23T15:30:00Z&edate=2025-10-23T14:00:00Z&offset=0&limit=0'' 
#         -H 'accept:application/json'
        
#         {
#   "statusCode": 201,
#   "message": "모든 정책 조회에 성공했습니다",
#   "data": [
#     {
#       "id": "78c85826-78f5-4e93-8aaf-833acb34d43c",
#       "name": "Masking rule",
#       "targets": [
#         "api"
#       ],
#       "typeMask": 2,
#       "operator": "AND",
#       "rule": "",
#       "prompt": "프롬프트",
#       "scope": "api",
#       "action": "masking",
#       "status": "deployed",
#       "adminId": "973050c6-b5ee-4afd-979e-07d4b1659c8b"
#     },
#     {
#       "id": "fff8f239-d2ca-4984-8b34-180a802b2ef6",
#       "name": "테스트용 정책",
#       "targets": [
#         "pii"
#       ],
#       "typeMask": 2,
#       "operator": "AND",
#       "rule": "테스트용 정책 내용",
#       "prompt": "비밀번호 출력하지마",
#       "scope": "user",
#       "action": "accept",
#       "status": "deployed",
#       "adminId": "23679764-44d8-45b6-9344-39f0bcc25fd6"
#     },
#     {
#       "id": "e4f68a6e-a0d1-4387-a779-a900b22402a1",
#       "name": "test rule",
#       "targets": [
#         "pii"
#       ],
#       "typeMask": 1,
#       "operator": "AND",
#       "rule": "regex rule",
#       "prompt": "",
#       "scope": "user",
#       "action": "accept",
#       "status": "deployed",
#       "adminId": "973050c6-b5ee-4afd-979e-07d4b1659c8b"
#     }
#   ],
#   "total": 3
# }
#         '''
        
#         # TODO: 접속정보, 기준이 모호한 부분, 우선 외부 파라미터로 받는 부분까지는 개발.
#         strDBServer:str = dictPolicyLocalConfig.get(LOCAL_CONFIG_DEFINE.KEY_DB_SERVER_DEFAULT_IP)
#         nDBPort:int = int(dictPolicyLocalConfig.get(LOCAL_CONFIG_DEFINE.KEY_DB_SERVER_DEFAULT_PORT))
#         strScheme:str = dictPolicyLocalConfig.get(LOCAL_CONFIG_DEFINE.KEY_DB_SERVER_DEFAULT_SCHEME)
                
#         # strScheme:str = "http"
        
#         # # strDBServer:str = "10.0.17.101"
#         # strDBServer:str = "127.0.0.1"
#         # nDBPort:int = 3000
        
#         #TODO: 서버, 접속 여부 체크, 테스트 코드
        
#         import socket
        
#         sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

#         #일단 예외처리는 하지 않는다.
#         sock.settimeout(3)
#         # nError = sock.connect_ex((WebApiLocalDefine.OPT_WEB_API_LOCAL_HOST,nWebApiPort))
#         nError = sock.connect_ex((strDBServer,nDBPort))
#         sock.close()
        
#         if 0 != nError:
#           LOG().error("fail connecto to db api server, close")
#           return ERR_FAIL
                
#         strStartDate:str = datetime.datetime.now().strftime("%Y-%m-%dT00:00:00Z")
#         strEndDate:str = datetime.datetime.now().strftime("%Y-%m-%dT23:59:59Z")
#         nOffset:int = 0
#         nLimit:int = 0
        
#         strUrl = (
#             f"{strScheme}://{strDBServer}:{nDBPort}/api/internal/policy/rules?"
#             f"sdate={strStartDate}&edate={strEndDate}&offset={nOffset}&limit={nLimit}"
#         )
        
#         #ssl 인증서 무시
#         bSSLVerify = False
        
#         nTimeOut = 10
        
#         dictHeader = {
#             "accept" : "application/json"
#         }
        
#         # DB 데이터, 요청
#         response:requests.Response = requests.get(strUrl, verify = bSSLVerify, timeout=nTimeOut, headers=dictHeader)
        
#         strResponseData:str = response.text
        
#         if response.status_code == 200:
                        
#             dictLocalVal = json.loads(strResponseData, strict=False)
#             dictFilterPolicy.update(dictLocalVal)
            
#         else:
#             raise Exception(f"fail request to db server, error = {response.status_code}({strResponseData})")
#             #return ERR_FAIL

#         return ERR_OK
      
      
      