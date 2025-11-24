
#외부 라이브러리
from lib_include import *

from type_hint import *

'''
테스트 모듈 추가
'''

class PipelineTestModule:
    
    def __init__(self):
        
        pass
    
    def test(self):
        
        self.testSqlprintf()
        
        pass
    
    
    #sqlprint, 테스트.
    def testSqlprintf(self):
        
        '''
        '''
        
        LOG().debug("test sqlprint")
        
        # ai 서비스, 계정 추가, insert or replace

        dictDBInfo = {
            "user_id" : "ghahn",
            "reg_date" : datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "email" : "ghahn@wins21.co.kr",
            "ai_service" : 0, #순차적으로 GPT, claude, gemini, copilot, ..
            "etc_comment" : "", #comment
            "use_flag" : 1, #1:활성, 0:비활성
        }
        
        dictDBResult:dict = {}
        sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_insert_update_ai_user_account", dictDBInfo, dictDBResult)
        
        LOG().debug(f"insert user account, result = {dictDBResult}")
        
        # ai 서비스, 계정 조회
        dictDBResult = {}
        sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_ai_user_account", {"where" : "", "limit":1}, dictDBResult)
        
        LOG().debug(f"select user account = {dictDBResult}")
        
        pass