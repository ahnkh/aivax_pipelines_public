
import re

#외부 라이브러리
from lib_include import *

from type_hint import *

# 그룹별 regex filter
from block_filter_modules.filter_policy.groupfilter.filter_policy_group_data import FilterPolicyGroupData

from block_filter_modules.filter_pattern.helper.filter_pattern_base import FilterPatternBase

'''
regex 패턴, 그룹별 패턴 정책 데이터 생성, helper
'''

class RegexPolicygenerateHelper:
    
    def __init__(self):
        pass
    
    # 그룹별 Regex 패턴, 공통화
    def GenerateRegexGroupPolicy(self, dictPolicyRuleScopeMap:dict, dictDBScopeRegexPattern:dict):
        
        '''
        '''
        
        # dictPolicyRuleScopeMap:dict = filterPolicyGroupData.GetPolicyRule(strFilterKey)
        
        # lstScopeRange = [
        #     DBDefine.POLICY_FILTER_SCOPE_USER,
        #     DBDefine.POLICY_FILTER_SCOPE_SERVICE,
        #     DBDefine.POLICY_FILTER_SCOPE_GROUP,
        #     DBDefine.POLICY_FILTER_SCOPE_DEFAULT
        # ]
        
        # bFilterChanged:bool = filterPattern.IsScopeBasedFilterPolicyChanged(dictPolicyRuleScopeMap, lstScopeRange)
        
        # if FilterPatternBase.POLICY_CHANGED == bFilterChanged:

        #정책 카운트, data 항목 이하. TOOD: 모든 항목을 지우는 케이스도 고려할것.
        # data:dict = dictFilterPolicy.get("data", {})
        
        # nRuleCount = filterPolicyGroupData.GetRuleCount(strFilterKey)

        # LOG().info(f"filter pattern policy is changed, filter = {strFilterKey}, rule count = {nRuleCount}")

        #원본 정책, 저장한다.
        # filterPattern.UpdateBaseDBFilterPolicy(dictPolicyRuleScopeMap)

        #TODO: 패턴에 대한 반영 기능은 필요하다. 실제 사용 변수에 대한 업데이트, 개별 패턴으로 반영이 필요하다.
        #TODO: rule compile 이슈, rule과 compile을 따로 가져갈지에 대한 검토
        # self.__updateRegexPatternFromDB(data)
        # self.__updateRegexPatternScopeRangeFromDB(dictPolicyRuleScopeMap)            
        self.__updateRegexPatternScopeRangeFromDB(dictPolicyRuleScopeMap, dictDBScopeRegexPattern)
            
            # return ERR_OK
            #TODO: 실패시의 예외, 반환에 대한 주의
        
        return ERR_OK
    
    ########################################### private
    
    # scope 단위로, Regex 패턴을 생성, 업데이트
    def __updateRegexPatternScopeRangeFromDB(self, dictPolicyRuleScopeMap:dict, dictDBScopeRegexPattern:dict):
        
        '''        
        '''
        
        # # 먼저 초기화
        # self.__dictDBScopeRegexPattern:dict = {
        #     DBDefine.POLICY_FILTER_SCOPE_USER : [],
        #     DBDefine.POLICY_FILTER_SCOPE_SERVICE : [],
        #     DBDefine.POLICY_FILTER_SCOPE_GROUP : [],
        #     DBDefine.POLICY_FILTER_SCOPE_DEFAULT : []
        # }
        
        #TODO: 전체 loop
        for strScope in dictPolicyRuleScopeMap:
            
            #scope 별 list, 참조로 반환
            lstFilterData:list = dictPolicyRuleScopeMap.get(strScope)
            
            #새로 추가할 regex rule
            lstNewFilterData:list = dictDBScopeRegexPattern.get(strScope)
            
            self.__updateRegexPatternFromDB(lstFilterData, lstNewFilterData)
        
        return ERR_OK

    #DB에서 정책을 받아서 업데이트 한다.
    def __updateRegexPatternFromDB(self, lstFilterData:list, lstNewFilterData:list):

        '''

        다음 패턴, rule만 받아서, 지정된 패턴에 추가한다.
        data": [
        {
        "id": "78c85826-78f5-4e93-8aaf-833acb34d43c",
        "name": "Masking rule",
        "targets": [
            "api"
        ],
        "typeMask": 2,
        "operator": "AND",
        "rule": "",
        "prompt": "프롬프트",
        "scope": "api",
        "action": "masking",
        "status": "deployed",
        "adminId": "973050c6-b5ee-4afd-979e-07d4b1659c8b"
        },

        TODO: 우선 rule을 받고, 이후 하나씩 확장한다.

        추가적인 정규식 flag 패턴이 필요하다. MULTILINE으로 되어 있다.

        SRE_FLAG_IGNORECASE = 2 # case insensitive
        SRE_FLAG_LOCALE = 4 # honour system locale
        SRE_FLAG_MULTILINE = 8 # treat target as multiline string
        SRE_FLAG_DOTALL = 16 # treat target as a single string
        SRE_FLAG_UNICODE = 32 # use unicode "locale"
        SRE_FLAG_VERBOSE = 64 # ignore whitespace and comments
        SRE_FLAG_DEBUG = 128 # debugging
        SRE_FLAG_ASCII = 256 # use ascii "locale"
        '''

        #TODO: 마찬가지로, 기존 데이터 삭제, 다만 데이터가 동일한지 체크하는 로직 필요
        # self.__listDBRegexPattern.clear()

        for dictPolicy in lstFilterData:

            #정책ID, 이름을 추가, 최초에 걸린 ID와 Name을 추가하여, 반환하도록 개선.
            id:str = dictPolicy.get("id")
            name:str = dictPolicy.get("name")
            targets:str = dictPolicy.get(DBDefine.DB_FIELD_RULE_TARGET) #targets 추가

            rule:str = dictPolicy.get("rule")

            action:str = dictPolicy.get(DBDefine.DB_FIELD_RULE_ACTION)

            #수신받은 rule을, 신규의 db filter 패턴에 추가한다.
            #기존과 동일한 패턴으로 관리를 위해서 tuple로 관리, 이름과 rule
            #성능의 약간의 개선을 위해서 컴파일된 객체로 관리 (TODO: DB 갱신 시점에 반복 갱신은 개선 필요)

            #TODO: 추가적인 컴파일 옵션이 필요하다. 우선
            # regex_flag:int = re.DOTALL

            #TODO: 2개의 옵션이 필요 => dictionary쪽이 나을수 있겠다. tuple X
            regexFlag:int = dictPolicy.get("regexFlag", 0)
            regexGroup:int = dictPolicy.get("regexGroup", 0)
            regexGroupVal:str = dictPolicy.get("regexGroupVal", None)
            
            strDBSubjectID:str = dictPolicy.get(DBDefine.DB_FIELD_SUBJECT_ID, "")
            strDBSubjectVal:str = dictPolicy.get(DBDefine.DB_FIELD_SUBJECT_VAL, "")

            #TODO: 없는 경우에 대한 처리
            
            #TODO: slm 정책, 룰이 없을수 있다. 반대로 예외처리 필요.            
            regexPattern:re.Pattern = None
                        
            if None != rule and 0 < len(rule):

            # #없으면 실행을 제외한다.
            # if None == rule or 0 == len(rule):
            #     LOG().error(f"invalid flag, no rule")
            #     continue

                #regexFlag, 예외처리
                if None == regexFlag:
                    regexFlag = re.DOTALL

                if None == regexGroup:
                    regexGroup = 0

                #2025.12.02 정규표현식 오류,
                #try로 묶어서 임시 테스트

                try:
                    regexPattern:re.Pattern = re.compile(rule, regexFlag)
                except Exception:
                    LOG().error(traceback.format_exc())
                    
                #pass

                #rule도 같이 넣는다. TODO: dictionary가 더 직관적일지도.
                # tupleRulePattern = (name, rule, regexPattern)

                # 예외처리, regexPattern이 존재하지 않으면, skip
                # 정책을 설정했는데도, 룰 컴파일을 못했으면, 버린다.
                if None == regexPattern:

                    LOG().error(f"compile pattern error, skip regex pattern, id = {id}, name = {name}, rule = {rule}")
                    return ERR_FAIL

            dictRegexPattern:dict = {
                "id" : id,
                "name" : name,
                "rule" : rule,
                "action" : action,
                DBDefine.DB_FIELD_RULE_TARGET : targets, #targets, 카테고리 추가
                "regex_pattern" : regexPattern, #룰을 컴파일 못했으면, 예외처리
                "regex_flag" : regexFlag,
                "regex_group" : regexGroup,
                "regex_group_val" : regexGroupVal,
                DBDefine.DB_FIELD_SUBJECT_ID : strDBSubjectID,
                DBDefine.DB_FIELD_SUBJECT_VAL : strDBSubjectVal,
            }

            # LOG().debug(f"update regex pattern, policy = {dictPolicy}")

            # self.__listDBRegexPattern.append(dictRegexPattern)
            lstNewFilterData.append(dictRegexPattern)

        return ERR_OK
    
    
    
    
    
    