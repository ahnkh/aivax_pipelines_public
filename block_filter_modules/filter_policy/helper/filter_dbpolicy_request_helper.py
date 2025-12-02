

#rule, base64
import base64

#외부 라이브러리
from lib_include import *

from type_hint import *

'''
Filter 정책 조회 모듈, http 요청, 응답 json 데이터만 받는 형태로 개발
'''

class FilterDBPolicyRequestHelper:
    
    def __init__(self):
        pass
      
    #DB 정책 조회 버전2 - DB에서 직접 조회
    def RequestToDBPolicy(self, dictFilterPolicy:dict, dictPolicyLocalConfig:dict):
      
      '''
      sqlprintf 구조로 변경한다.
      조회된 결과에서 기준 데이터 구조와 동일한 dictionary 형태로 업데이트
      
      '''
      
      '''
      select id, name, type_mask, operator, rule, action, regex_flag, regex_group, regex_group_val, status from policy_rules where status = 'deployed'
      '''
      dictDBResult = {}
      sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_policy_rule", {}, dictDBResult)
      
      lstQueryData:list = dictDBResult.get(DBSQLDefine.QUERY_DATA)
      
      #같은 구조를 유지, 그대로 복사한다. (혹시 몰라 deep copy)  
      #TODO: 키구조 주의, 그대로 넣으면 안된다. 사양 파악후 정리.      
      # 키를 생성해서, 다시 만든다. => DB에서 만든다. 그래도, 바로 복사는 안된다.
      # self.__dictCurrentUserInfo:dict = copy.deepcopy(dictQueryData)
      
      lstNewFilterPolicy = []
      
      #일단 하나로 처리하자.
      for dictPattern in lstQueryData:
          
        # id:str = dictPattern.get("id")
        # name:str = dictPattern.get("name")
        # type_mask:int = dictPattern.get("type_mask")
        # operator:str = dictPattern.get("operator")
        rule:str = dictPattern.get("rule")
        # action:str = dictPattern.get("action")
        # status:str = dictPattern.get("status")
        
        regex_flag:int = dictPattern.get("regex_flag")
        regex_group:int = dictPattern.get("regex_group")
        regex_group_val:str = dictPattern.get("regex_group_val")
        
        #이름 보정
        dictPattern["regexFlag"] = regex_flag
        dictPattern["regexGroup"] = regex_group
        dictPattern["regexGroupVal"] = regex_group_val
          
        #TODO: action, base64 => decode 처리후 저장.
          
        byteBase64Decode = base64.b64decode(rule)
          
        #문자열로 변환
        strBase64Decode = byteBase64Decode.decode("utf-8")
          
        #어차피 여기서만 조회, 그냥 업데이트
        dictPattern["rule"] = strBase64Decode
          
        #그 연산이 그연산..
        # dictNewPattern = {
        #   "id" : id
        # }
          
        lstNewFilterPolicy.append(dictPattern)
        #pass
        
      dictFilterPolicy["data"] = lstNewFilterPolicy
      
      return ERR_OK
    
    
      
    #가상의 테스트 코드, 다음의 데이터가 반환되도록 처리
    def testRequestToDBPolicy(self, dictFilterPolicy:dict, dictPolicyLocalConfig:dict):
      
        '''
        '''
        
        dictLocalVal = {
          
          "data" : [
            {
              "name": "PEM 패턴",                          
              "rule": "-----BEGIN (?P<K>[^-\r\n]+?) KEY-----[\s\S]+?-----END (?P=K) KEY-----",              
              "action": "block",
              "regex_flag" : 16,
              "regex_group" : 0,
              "regex_group_val" : None,              
            },
            
            {
              "name": "JWT",                          
              "rule": "\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",              
              "action": "masking",
              "regex_flag" : 0,
              "regex_group" : 0,
              "regex_group_val" : None              
            },
            
            {
              "name": "aws_access_key_id",                          
              "rule": "\b(?:AKIA|ASIA|ANPA|ABIA)[0-9A-Z]{16}\b",              
              "action": "block",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : None              
            },
            
            {
              "name": "aws_secret_access_key",                          
              "rule": "(?<![A-Za-z0-9/+=])([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])",              
              "action": "masking",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : None              
            },
            
            {
              "name": "azure_storage_account_key",                          
              "rule": "(?i)\bAccountKey=(?P<VAL>[A-Za-z0-9+/=]{30,})",              
              "action": "block",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"              
            },
            
            {
              "name": "azure_conn_string",                          
              "rule": "(?i)\bDefaultEndpointsProtocol=\w+;AccountName=\w+;AccountKey=(?P<VAL>[A-Za-z0-9+/=]{30,})",              
              "action": "masking",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"              
            },
            
            {
              "name": "basic_auth_creds",                          
              "rule": "(?i)\b(?:https?|ftp|ssh)://(?P<CREDS>[^:@\s/]+:[^@\s/]+)@",
              "action": "block",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "CREDS"
            },
            
            {
              "name": "cloudant_creds",                          
              "rule": "(?i)https?://(?P<CREDS>[^:@\s/]+:[^@\s/]+)@[^/\s]*\.cloudant\.com",
              "action": "masking",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "CREDS"              
            },
            
            {
              "name": "discord_bot_token",                          
              "rule": "\b(?P<VAL>[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27})\b",
              "action": "block",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"              
            },
            
            {
              "name": "github_token",                          
              "rule": "\b(?P<VAL>(?:ghp|gho|ghu|ghs|ghr)[-_][A-Za-z0-9]{16,})\b",
              "action": "masking",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"              
            },
            
            {
              "name": "mailchimp_api_key",                          
              "rule": "\b(?P<VAL>[0-9a-f]{32}-us\d{1,2})\b",
              "action": "block",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"              
            },
            
            {
              "name": "slack_token",                          
              "rule": "\b(?P<VAL>xox[abprs]-[A-Za-z0-9-]{10,})\b",
              "action": "masking",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"              
            },
            
            {
              "name": "slack_webhook_path",                          
              "rule": "(?i)https://hooks\.slack\.com/services/(?P<VAL>T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+)",
              "action": "block",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"              
            },
            
            {
              "name": "stripe_secret",                          
              "rule": "\b(?P<VAL>sk_(?:live|test)_[A-Za-z0-9]{16,})\b",
              "action": "masking",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"
            },
            
            {
              "name": "stripe_publishable",                          
              "rule": "\b(?P<VAL>pk_(?:live|test)_[A-Za-z0-9]{16,})\b",
              "action": "block",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"              
            },
            
            {
              "name": "twilio_account_sid",                          
              "rule": "\b(?P<VAL>AC[0-9a-fA-F]{32})\b",
              "action": "masking",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"              
            },
            
            {
              "name": "twilio_auth_token",                          
              "rule": "(?<![A-Za-z0-9])(?P<VAL>[0-9a-fA-F]{32})(?![A-Za-z0-9])",
              "action": "block",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"              
            },
            
            {
              "name": "openai_like",                          
              "rule": "\b(?P<VAL>sk-[A-Za-z0-9]{16,})\b",
              "action": "masking",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"              
            },
            
            {
              "name": "ak_tk_token",                          
              "rule": "\b(?P<VAL>(?:ak|tk)-[a-f0-9]{16,}(?:-(?:dev|test)[a-z0-9]*)?)\b",
              "action": "block",
              "regex_flag" : 0,
              "regex_group" : 1,
              "regex_group_val" : "VAL"              
            },
            
            {
              "name": "candidate",                          
              "rule": "[A-Za-z0-9+/=._\-]{16,}",
              "action": "masking",
              "regex_flag" : 0,
              "regex_group" : 0,
              "regex_group_val" : None
            }
            
          ]  
        }
        
        dictFilterPolicy.update(dictLocalVal)

        return ERR_OK
      
      
  ############################################### 지울 코드
  
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
      
      
      