
import copy
import re
import math

#외부 라이브러리
from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.helper.filter_pattern_base import FilterPatternBase

# 그룹별 regex filter
from block_filter_modules.filter_policy.groupfilter.filter_policy_group_data import FilterPolicyGroupData

# 별도 helper
from block_filter_modules.filter_pattern.helper.regex_policy_helper.regex_policy_generate_helper import RegexPolicygenerateHelper

'''
정책 패턴 탐지, detect secret 패턴
기존 개발 코드 리펙토링, 이후 정책 관리 DB 업데이트
TODO: 내부 정책이 존재할수 있으며, 1차 하드코딩, 모듈 분리
2차 DB로 분리
3차 polling 구조로 가공.

탐지 기능, masking 기능을 각각 제공
'''

class DetectSecretFilterPattern (FilterPatternBase):

    #DB 정책의 Key 정보, 상수로 관리, 사양변경, detect secret을 regex로 통일한다. (정규 표현식은 regex로 통일)
    # 소스코드 이름은, 영향범위가 커서 나중에 수정
    # 대신 pipeline은 detect secret을 당분간 유지 (sslproxy 영향, 테스트후 향후 개선)
    POLICY_FILTER_KEY = DBDefine.FILTER_KEY_REGEX

    def __init__(self):

        super().__init__()

        #TODO: 하드코딩 데이터 => DB로 치환

        #TODO: 이 패턴은 남긴다.
        self.__regexCandidate:re.Pattern = None

        #DB에서 조회한 regex 패턴
        # self.__listDBRegexPattern:List[Tuple[str, str, re.Pattern]] = []
        
        #TODO: scope 단위의 DB 패턴으로 관리
        self.__dictDBScopeRegexPattern:dict = None

        #TODO: 이건 어떤 기능인지 확인후 이름 변경 우선 이기능은 유지
        self.re_b64_shape = None
        self.re_hex_shape = None
        
        #helper 추가
        self.__regexPolicyGenerateHelper:RegexPolicygenerateHelper = None        
        # pass

    #초기화 로직, 상태, 정책이 존재하며, 정책은 향후 detect_secret policy로 이동한다.
    def Initialize(self, dictJsonLocalConfigRoot:dict):

        '''
        일단 하드코딩
        '''

        # 기본 변수 초기화
        
        self.__dictDBScopeRegexPattern:dict = {
            DBDefine.POLICY_FILTER_SCOPE_USER : [],
            DBDefine.POLICY_FILTER_SCOPE_SERVICE : [],
            DBDefine.POLICY_FILTER_SCOPE_GROUP : [],
            DBDefine.POLICY_FILTER_SCOPE_DEFAULT : []
        }
        
        self.__regexPolicyGenerateHelper:RegexPolicygenerateHelper = RegexPolicygenerateHelper()
        
        self.__regexCandidate:re.Pattern = re.compile(r"[A-Za-z0-9+/=._\-]{16,}")  # 후보 토큰(완화)

        #TODO: 이 기능은 파악이 안되어 유지
        self.re_b64_shape = re.compile(r"^[A-Za-z0-9+/=]+$")
        self.re_hex_shape = re.compile(r"^[A-Fa-f0-9]+$")

        # self.__initializeRegexPattern()

        return ERR_OK


    #기본 모듈, 그대로 이동 (기존 detect_spans)
    def DetectPattern(self, strContents:str, valves:Any, strUserID:str, strUUID:str, nServiceType:int): #-> Tuple[List[Tuple[int, int]], Dict[str, int]]:

        '''
        기존 코드, 그대로 이관
        '''

        #TODO: span, count를 반환하는데, 호출측에서 count를 사용하지 않는다.. 추가 분석 필요..
        # return self.__detectDefaultBaseFilter(strContents, valves)
        # return ERR_OK

        #TODO: 반환값이 변경되었다. typing 구문 제외
        return self.__detectFilterFromDB(strContents, valves, strUserID, strUUID, nServiceType)

    #정책 테스트, 한개의 정책만 테스트 한다. TODO: 우선 개발후 2차 리펙토링
    def TestRulePattern(self, strPrompt:str, strRegexRule:str, strAction:str):

        '''
        '''

        counts:dict = {"block": 0, "masking": 0, "accept": 0}

        spans: List[Tuple[int, int]] = []

        #TODO: 탐지된 id, name은 버린다.

        #없으면 실행을 제외한다.
        if None == strRegexRule or 0 == len(strRegexRule):
            raise Exception(f"invalid flag, no rule")

        regexPattern:re.Pattern = re.compile(strRegexRule, 0)

        dictRegexPattern:dict = {
            "id" : "dummy",
            "name" : "dummy",
            "rule" : strRegexRule,
            "action" : strAction,
            "regex_pattern" : regexPattern,
            "regex_flag" : 0,
            "regex_group" : 0,
            "regex_group_val" : None,
        }

        #최초 탐지된 룰 ID, Name을 반환하도록 개선
        # dictDetectRule: dict = {"id": "", "name": ""}
        dictDetectRule: dict = {}

        self.__detectFilterPatternAt(strPrompt, spans, counts, dictDetectRule, dictRegexPattern)

        return (spans, counts, dictDetectRule)

    #상속, DB의 패턴 정책을 수신받는다.
    # def notifyUpdateDBPatternPolicy(self, dictFilterPolicy:dict) -> int:
    def notifyUpdateDBPatternPolicy(self, filterPolicyGroupData:FilterPolicyGroupData) -> int:

        '''
        전체 정책을 받고, 각 정책에서 필요한 부분을 추출해서 사용한다.
        TODO: 인수인계 시점에는 정책의 구분자가 없어, 받은 데이터의 rule에 대해서 로그로 확인까지만 구현한다.

        2단계, filterPolicyGroupData에서 filter key에 해당하는 정책을 수집한다.
        이후 로직은 우선 기존과 동일하게 유지한다.
        '''

        '''
        dictPolicyRuleScopeMap:dict = filterPolicyGroupData.GetPolicyRule(strFilterKey)

        #N개의 scope 존재, 복사가 되어야 한다.

        #정상적인 수신이라는 가정 => 정상이 아니라도, None일수 있다.
        # data:list = dictFilterPolicy.get("data")

        # #기존 모듈, 재활용
        # data:list = lstPolicyRule

        # if None == data:
        #     LOG().error("invalid db data, no data, skip")
        #     return ERR_FAIL

        # LOG().debug(f"notify update db pattern policy in detect secret patternm, rule count = {len(data)}")

        #TEST 디버깅, 필요할경우 정책 업데이트 (향후 제거)
        # for dictPolicy in data:

        #     rule:str = dictPolicy.get("rule")
        #     name:str = dictPolicy.get("name")
        #     action:str = dictPolicy.get("action")

        #     #TODO: 2개의 옵션이 필요 => dictionary쪽이 나을수 있겠다. tuple X
        #     regex_flag:int = dictPolicy.get("regex_flag")
        #     regex_group:str = dictPolicy.get("regex_group")

        #     LOG().debug(f"rule received, rule = {rule}, name = {name}, action = {action}")

        #TODO: 정책에 대한 비교, 이전 정책과 현재 정책이 같으면, skip한다.
        # bFilterChanged:bool = self.IsFilterPolicyChanged(lstPolicyRule)
        
        lstScopeRange = [
            DBDefine.POLICY_FILTER_SCOPE_USER,
            DBDefine.POLICY_FILTER_SCOPE_SERVICE,
            DBDefine.POLICY_FILTER_SCOPE_GROUP,
            DBDefine.POLICY_FILTER_SCOPE_DEFAULT
        ]
        
        bFilterChanged:bool = self.IsScopeBasedFilterPolicyChanged(dictPolicyRuleScopeMap, lstScopeRange)

        #TODO: 분기문 안에서 처리하는게 직관적으로 보인다.
        # if False == bFilterChanged:
        if FilterPatternBase.POLICY_CHANGED == bFilterChanged:

            #정책 카운트, data 항목 이하. TOOD: 모든 항목을 지우는 케이스도 고려할것.
            # data:dict = dictFilterPolicy.get("data", {})
            
            nRuleCount = filterPolicyGroupData.GetRuleCount(strFilterKey)

            LOG().info(f"filter pattern policy is changed, filter = {strFilterKey}, rule count = {nRuleCount}")

            #원본 정책, 저장한다.
            self.UpdateBaseDBFilterPolicy(dictPolicyRuleScopeMap)

            #TODO: 패턴에 대한 반영 기능은 필요하다. 실제 사용 변수에 대한 업데이트, 개별 패턴으로 반영이 필요하다.
            #TODO: rule compile 이슈, rule과 compile을 따로 가져갈지에 대한 검토
            # self.__updateRegexPatternFromDB(data)
            # self.__updateRegexPatternScopeRangeFromDB(dictPolicyRuleScopeMap)            
            self.__regexPolicyGenerateHelper.UpdateRegexPatternScopeRangeFromDB(dictPolicyRuleScopeMap)
            
            # return ERR_OK
            #TODO: 실패시의 예외, 반환에 대한 주의
            
        '''
        
        strFilterKey:str = DetectSecretFilterPattern.POLICY_FILTER_KEY
        
        #TODO: 이 로직은 유지
        dictPolicyRuleScopeMap:dict = filterPolicyGroupData.GetPolicyRule(strFilterKey)
        
        bFilterChanged:bool = self.IsScopeBasedFilterPolicyChanged(dictPolicyRuleScopeMap)
        
        if FilterPatternBase.POLICY_CHANGED == bFilterChanged:
            
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

    ################################################# private
    

    # filter 처리 리펙토링, 탐지 기능 재구현
    def __detectFilterFromDB(self, text:str, valves:Any, strUserID:str, strUUID:str, nServiceType:int):

        '''
        db의 필터 정책을 순회하여, action 값이 maskinig, block여부에 따라, 먼저 걸린 순서로 반환한다.
        우선 전체를 순회하며, block이 하나라도 걸리면 blocking 이고, blocking 이 없을때 masking 여부를 체크한다.
        전체 순회는 수행하며, 기존의 count 필드를 action을 기준으로 카운트하여 반환한다.
        탐지후 결과는 detect_secret 필터에서 처리한다.

        TODO: 일부 하드코딩은 존재하며, 나중에 추가 리펙토링을 진행한다.
        '''

        # counts:dict = {"pem": 0, "jwt": 0, "known": 0, "entropy": 0}
        counts:dict = {"block": 0, "masking": 0, "accept": 0}

        spans: List[Tuple[int, int]] = []

        #최초 탐지된 룰 ID, Name을 반환하도록 개선
        # dictDetectRule: dict = {"id": "", "name": ""}
        dictDetectRule: dict = {}
        
        #순차적으로, scope 별로 조회
        #scope 순서는 user, service, group, default 정책이다.
        #service 타입, user 필드에 따른 서비스별 분기가 있기에, 각각 만들어야 한다.
        
        # 각 연결된 탐지, 수행
        self.__detectLinkedRegexPatternList(self.__dictDBScopeRegexPattern, text, spans, counts, dictDetectRule, strUserID, strUUID, nServiceType)
        
        #탐지는 같은데, 걸리는 조합이 다르다. 탐지가 되면 skip, 아니면 next

        # #TODO: dbRegexPattern, DB 동기화 기능 추가, deepcopy 추가
        # listDBRegexPattern:list = copy.deepcopy(self.__listDBRegexPattern)

        # for dictDBPattern in self.__listDBRegexPattern:
        # for dictDBPattern in listUserPattern:
        #     self.__detectFilterPatternAt(text, spans, counts, dictDetectRule, dictDBPattern)

        # (D) 엔트로피 => 이건 어떤 기능인지 몰라서, 우선 추가
        for s, e in self.__high_entropy_hits(text, valves):
            self.__add_span(spans, s, e)
            counts["entropy"] = counts.get("entropy", 0) +1

        return (spans, counts, dictDetectRule)
    
    def __detectLinkedRegexPatternList(self, dictDBScopeRegexPattern:dict, strPromptText:str, spans: List[Tuple[int, int]], counts:dict, dictDetectRule: dict, 
                                       strUserID:str, strUUID:str, nServiceType:int):
        
        '''
        '''
        
        listUserPattern:list = dictDBScopeRegexPattern.get(DBDefine.POLICY_FILTER_SCOPE_USER)
        bDetectUser:bool = self.__detectUserBasePattern(listUserPattern, strPromptText, spans, counts, dictDetectRule, strUserID, strUUID)
        
        if True == bDetectUser:
            return ERR_OK
        
        listServicePattern:list = self.__dictDBScopeRegexPattern.get(DBDefine.POLICY_FILTER_SCOPE_SERVICE)
        
        bDetectService:bool = self.__detectServiceBaseRegexPattern(listServicePattern, strPromptText, spans, counts, dictDetectRule, nServiceType)
        
        if True == bDetectService:
            return ERR_OK
        
        # #TODO: 그룹, 무시
        # listGroupPattern:list = self.__dictDBScopeRegexPattern.get(DBDefine.POLICY_FILTER_SCOPE_GROUP)
        
        #default: 동일 패턴. 마지막은 동일 패턴, 그대로 전달
        listDefaultPattern:list = self.__dictDBScopeRegexPattern.get(DBDefine.POLICY_FILTER_SCOPE_DEFAULT)
        
        self.__detectDefaultRegexPattern(listDefaultPattern, strPromptText, spans, counts, dictDetectRule)
        
        return ERR_OK
    
    # 사용자 패턴, 사용자로 등록된 정책을 찾아서, 해당 사용자와 일치하는 정책만 탐지를 수행한다. 정책 구조는 동일
    def __detectUserBasePattern(self, listUserPattern:list, strPromptText:str, spans: List[Tuple[int, int]], counts:dict, dictDetectRule: dict, strUserID:str, strUUID:str) -> bool:
        
        '''
        #마지막에 bool값을 전달, next를 수행할지를 결정한다.
        '''
        
        for dictDBPattern in listUserPattern:
            
            #여기서 subjectid, subject val 값와 user를 비교한다. 넘어오는것은 user 명이다. (엔진에서 추출)
            
            #subjectid로 user일때는 user의 uuid가 들어온다.
            strDBUserUUID:str = dictDBPattern.get(DBDefine.DB_FIELD_SUBJECT_ID)
            
            # 일치하는 UserID만 비교
            if strUUID == strDBUserUUID:     
                
                #LOG, 향후 제거
                # strUserUUID = dictDBPattern.get(DBDefine.DB_FIELD_SUBJECT_ID)
                LOG().info(f"detect user base regex pattern, id = {strDBUserUUID}, name = {strUserID}")        
                self.__detectFilterPatternAt(strPromptText, spans, counts, dictDetectRule, dictDBPattern)
                
                # 여기는 detect_secret과 같은 구조 (향후 사양 확인후 개선 필요)
                if spans:
                    return True
            
        return False
    
    # 서비스 패턴, 서비스 id로 매치한다.
    def __detectServiceBaseRegexPattern(self, listservicePattern:list, strPromptText:str, spans: List[Tuple[int, int]], counts:dict, dictDetectRule: dict, nServiceID:int) -> bool:
        
        '''
        '''
        
        for dictDBPattern in listservicePattern:
            
            #여기서 subjectid, subject val 값와 user를 비교한다. 넘어오는것은 user 명이다. (엔진에서 추출)
            nDBServiceID = dictDBPattern.get(DBDefine.DB_FIELD_SUBJECT_ID)
            # sterUserIDVal = dictDBPattern.get(DBDefine.DB_FIELD_SUBJECT_VAL)
            
            # 일치하는 UserID만 비교
            if nServiceID == nDBServiceID:    
                
                #일단 LOG
                LOG().info(f"detect service base regex pattern, id = {nServiceID}")        
                self.__detectFilterPatternAt(strPromptText, spans, counts, dictDetectRule, dictDBPattern)
                
                # 여기는 detect_secret과 같은 구조 (향후 사양 확인후 개선 필요)
                if spans:
                    return True
        
        return False
    
    # default 패턴, 기존과 동일
    def __detectDefaultRegexPattern(self, listDBRegexPattern:list, strPromptText:str, spans: List[Tuple[int, int]], counts:dict, dictDetectRule: dict):
        
        '''
        '''
        
        for dictDBPattern in listDBRegexPattern:
            self.__detectFilterPatternAt(strPromptText, spans, counts, dictDetectRule, dictDBPattern)
        
        #제일 마지막이다.
        # return (spans, counts, dictDetectRule)
        return ERR_OK
        

    #개별 dictionary 별 정책 조회
    def __detectFilterPatternAt(self, text:str, spans:list, dictCount:dict, dictDetectRule:dict, dictDBPattern:dict):

        '''
        '''

        id:str = dictDBPattern.get("id")
        name:str = dictDBPattern.get("name")

        # rule:str = dictDBPattern.get("rule")
        action:str = dictDBPattern.get("action")
        # regex_flag:int = int(dictDBPattern.get("regex_flag"))
        regex_group:int = (dictDBPattern.get("regex_group"))
        regex_group_val:str = dictDBPattern.get("regex_group_val")

        regex_pattern:re.Pattern = dictDBPattern.get("regex_pattern")

        #group 여부인지, 아닌지에 따른 분기, 여기는, 우선 나누지 않는다.

        #TODO: 1차 예외처리
        if None == regex_pattern:

            LOG().error(f"invalid regex pattern, id = {id}, name = {name}, skip")
            return ERR_FAIL

        if CONFIG_OPT_ENABLE == regex_group:
            for m in regex_pattern.finditer(text):
                if regex_group_val and regex_group_val in m.groupdict():
                    s, e = m.span(regex_group_val)
                else:
                    s, e = m.span(0)

                self.__add_span(spans, s, e)
                # counts[action] += 1
                dictCount[action] = dictCount.get(action,0) + 1

                self.__assignFirstDetectedRule(dictDetectRule, id, name)
        else:
            for m in regex_pattern.finditer(text):
                self.__add_span(spans, m.start(), m.end())
                # counts[action] += 1
                dictCount[action] = dictCount.get(action,0) + 1

                self.__assignFirstDetectedRule(dictDetectRule, id, name)

        return ERR_OK

    #최초 탐지된 룰 정보 할당.
    def __assignFirstDetectedRule(self, dictDetectRule:dict, strRuleID:str, strRuleName:str):

        '''
        '''

        #최초 탐지되면, 추가 (TODO: 리펙토링)
        if 0 == len(dictDetectRule):
            #test
            LOG().debug(f"assign first detect rule, id = {strRuleID}, name = {strRuleName}")
            dictDetectRule["id"] = strRuleID
            dictDetectRule["name"] = strRuleName

        return ERR_OK


    # ---------- 마스킹 유틸 ----------
    def __add_span(self, spans: List[Tuple[int, int]], start: int, end: int):

        '''
        '''

        if start < end:
            spans.append((start, end))

        #pass


    def overlaps_url(self, url_spans:list, s: int, e: int) -> bool:

        '''
        '''
        for us, ue in url_spans:
            if not (e <= us or s >= ue):
                return True
        return False


    # ---------- 엔트로피 매치 ----------
    def __high_entropy_hits(self, text: str, valves:Any) -> List[Tuple[int, int]]:

        '''
        '''

        # v = self.valves
        hits: List[Tuple[int, int]] = []

        # URL 범위는 엔트로피 검사에서 제외(오탐 방지)
        url_spans = [m.span() for m in re.finditer(r"https?://\S+", text)]

        for m in self.__regexCandidate.finditer(text):

            s0, e0 = m.start(), m.end()

            if self.overlaps_url(url_spans, s0, e0):
                continue

            raw = m.group(0)
            norm:str = self.__normalize_for_entropy(raw)

            nLen:int = len(norm)
            if nLen < 12:  # 정규화 후 너무 짧으면 스킵(완화)
                continue

            fHighVal:float = self.__entropy(norm)

            looks_b64:bool = self.__looks_b64(re.sub(r"[^A-Za-z0-9+/=]", "", raw))
            looks_hex:bool = self.__looks_hex(norm)

            keep = False
            if looks_b64:
                keep = (nLen >= valves.min_len_b64 and fHighVal >= valves.thr_b64)
            elif looks_hex:
                keep = (nLen >= valves.min_len_hex and fHighVal >= valves.thr_hex)
            else:
                keep = (nLen >= valves.min_len_mixed and fHighVal >= valves.thr_mixed)

            # 프리픽스 완화(옵션)
            if valves.prefix_relax and not keep:

                low = raw.lower()
                if low.startswith(("ak-", "tk-", "ghp-", "ghp_", "gho-", "gho_", "ghu-", "ghu_", "ghs-", "ghs_", "ghr-", "ghr_")):
                    has_digit = any(c.isdigit() for c in norm)
                    has_alpha = any(c.isalpha() for c in norm)
                    if nLen >= 16 and has_digit and has_alpha and fHighVal >= 3.4:  # 더 완화
                        keep = True

            if keep:
                hits.append((s0, e0))

        return hits


    def __normalize_for_entropy(self, strData: str) -> str:

        '''
        '''

        # 접미사 -dev\d+, -test\d+ 제거 후 비영숫자 제거
        strData = re.sub(r"[-_](?:dev|test)[0-9]*$", "", strData, flags=re.IGNORECASE)
        return re.sub(r"[^A-Za-z0-9]", "", strData)

    # ---------- 엔트로피 계산/정규화 ----------
    def __entropy(self, s: str) -> float:
        '''
        '''
        if not s:
            return 0.0

        counts = {}

        for ch in s:
            counts[ch] = counts.get(ch, 0) + 1

        n = len(s)

        return -sum((c / n) * math.log2(c / n) for c in counts.values())


    def __looks_b64(self, s: str) -> bool:
        return bool(self.re_b64_shape.match(s))

    def __looks_hex(self, s: str) -> bool:
        return bool(self.re_hex_shape.match(s))



    # ---------- 탐지 스팬 산출(정규식 + 엔트로피) ----------
    # def __detectSpans(self, text:str, valves:Any) -> Tuple[List[Tuple[int, int]], Dict[str, int]]:
    # def __detectDefaultBaseFilter(self, text:str, valves:Any) -> Tuple[List[Tuple[int, int]], Dict[str, int]]:

    #     '''
    #     TODO: 반환값의 정리가 필요하며, 우선 기존과 호환되도록 개발한다.
    #     '''

    #     spans: List[Tuple[int, int]] = []

    #     #TODO: 이 패턴 구조를 활용. 먼저 걸리는 정책, 차단이 되면, 해당 정책을 반환한다.
    #     #지금 정책은 버리고, 별도의 DB 정책을 제공해야 할 가능성.
    #     counts:dict = {"pem": 0, "jwt": 0, "known": 0, "entropy": 0}

    #     # (A) PEM 블록(멀티라인 전체)
    #     for m in self.__regexPEMBlock.finditer(text):
    #         self.__add_span(spans, m.start(), m.end())
    #         counts["pem"] += 1

    #     # (B) JWT
    #     for m in self.__regexJWTPattern.finditer(text):
    #         self.__add_span(spans, m.start(), m.end())
    #         counts["jwt"] += 1

    #     # (C) 알려진 패턴(값 그룹만 마스킹)
    #     for _, pat, grp in self.__listKnownRegexPatterns:

    #         for m in pat.finditer(text):
    #             if grp and grp in m.groupdict():
    #                 s, e = m.span(grp)
    #             else:
    #                 s, e = m.span(0)
    #             self.__add_span(spans, s, e)
    #             counts["known"] += 1

    #     # (D) 엔트로피(완화 임계치)
    #     for s, e in self.__high_entropy_hits(text, valves):
    #         self.__add_span(spans, s, e)
    #         counts["entropy"] += 1


    #     # #DB 패턴 추가, TODO: counts 데이터는 현재 사용하지 않는다.
    #     # #하지만, 해당 형상은 유지하고, 향후 개선해 본다.
    #     # # for name, rule, regexPattern in self.__listDBRegexPattern:
    #     # for _, _, regexPattern in self.__listDBRegexPattern:

    #     #     #나머지 항목은 우선 하드코딩, 향후 개선
    #     #     for m in regexPattern.finditer(text):
    #     #         if grp and grp in m.groupdict():
    #     #             s, e = m.span(grp)
    #     #         else:
    #     #             s, e = m.span(0)
    #     #         self.__add_span(spans, s, e)
    #     #         counts["known"] += 1

    #     return spans, counts
    
    
    #기 구현된 detect secret pattern의 정규식, 이관
    # def __initializeRegexPattern(self, ):

    #     '''
    #     '''

    #     # ---------- 엔트로피 후보/도우미 ----------
    #     self.__regexCandidate:re.Pattern = re.compile(r"[A-Za-z0-9+/=._\-]{16,}")  # 후보 토큰(완화)

    #     #TODO: 이 기능은 파악이 안되어 유지
    #     self.re_b64_shape = re.compile(r"^[A-Za-z0-9+/=]+$")
    #     self.re_hex_shape = re.compile(r"^[A-Fa-f0-9]+$")

    #     return ERR_OK

    #     #정책 테스트, 별도의 정책을 만든다. DB 패턴, List로 관리
    #     #기존 정책, 한개를 제외
    #     #해당 정책에서 rule을 조회

    #     # self.__regexPEMBlock:re.Pattern = re.compile(
    #     #     r"-----BEGIN (?P<K>[^-\r\n]+?) KEY-----[\s\S]+?-----END (?P=K) KEY-----",
    #     #     re.MULTILINE,
    #     # )

    #     # # JwtTokenDetector: JWT 토큰
    #     # self.__regexJWTPattern:re.Pattern = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

    #     # # ---------- 알려진 패턴(값 그룹명 group='VAL' 권장, 필요시 개별 그룹명) ----------
    #     # key_kv:str = r"(?:api[_-]?key|x-api-key|api[_-]?token|x-api-token|auth[_-]?token|password|passwd|pwd|secret|private[_-]?key)"
    #     # sep:str = r"\s*[:=]\s*"

    #     # #TODO: DB 테스트를 위해서, 정책을 주석 처리한다.
    #     # # (label, pattern, value_group_name) — group 없으면 전체 매치 사용
    #     # self.__listKnownRegexPatterns: List[Tuple[str, re.Pattern, Optional[str]]] = [

    #     #     # AWSKeyDetector

    #     #     ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|ANPA|ABIA)[0-9A-Z]{16}\b"), None),
    #     #     ("aws_secret_access_key", re.compile(r"(?<![A-Za-z0-9/+=])([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])"), None),

    #     #     # AzureStorageKeyDetector (connection string)
    #     #     ("azure_storage_account_key", re.compile(r"(?i)\bAccountKey=(?P<VAL>[A-Za-z0-9+/=]{30,})"), "VAL"),
    #     #     ("azure_conn_string", re.compile(r"(?i)\bDefaultEndpointsProtocol=\w+;AccountName=\w+;AccountKey=(?P<VAL>[A-Za-z0-9+/=]{30,})"), "VAL"),

    #     #     # Base64HighEntropyString — 정규식으로 직접 잡기보다는 엔트로피가 담당(아래)

    #     #     # BasicAuthDetector: scheme://user:pass@host
    #     #     ("basic_auth_creds", re.compile(r"(?i)\b(?:https?|ftp|ssh)://(?P<CREDS>[^:@\s/]+:[^@\s/]+)@"), "CREDS"),

    #     #     # CloudantDetector: https://user:pass@<account>.cloudant.com
    #     #     ("cloudant_creds", re.compile(r"(?i)https?://(?P<CREDS>[^:@\s/]+:[^@\s/]+)@[^/\s]*\.cloudant\.com"), "CREDS"),

    #     #     # DiscordBotTokenDetector
    #     #     ("discord_bot_token", re.compile(r"\b(?P<VAL>[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27})\b"), "VAL"),

    #     #     # GitHubTokenDetector (classic/pat 등)
    #     #     ("github_token", re.compile(r"\b(?P<VAL>(?:ghp|gho|ghu|ghs|ghr)[-_][A-Za-z0-9]{16,})\b"), "VAL"),

    #     #     # MailchimpDetector (키 형태: 32 hex + -usN)
    #     #     ("mailchimp_api_key", re.compile(r"\b(?P<VAL>[0-9a-f]{32}-us\d{1,2})\b"), "VAL"),

    #     #     # SlackDetector
    #     #     ("slack_token", re.compile(r"\b(?P<VAL>xox[abprs]-[A-Za-z0-9-]{10,})\b"), "VAL"),
    #     #     ("slack_webhook_path", re.compile(r"(?i)https://hooks\.slack\.com/services/(?P<VAL>T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+)"), "VAL"),

    #     #     # StripeDetector
    #     #     ("stripe_secret", re.compile(r"\b(?P<VAL>sk_(?:live|test)_[A-Za-z0-9]{16,})\b"), "VAL"),
    #     #     ("stripe_publishable", re.compile(r"\b(?P<VAL>pk_(?:live|test)_[A-Za-z0-9]{16,})\b"), "VAL"),

    #     #     # TwilioKeyDetector
    #     #     ("twilio_account_sid", re.compile(r"\b(?P<VAL>AC[0-9a-fA-F]{32})\b"), "VAL"),
    #     #     ("twilio_auth_token", re.compile(r"(?<![A-Za-z0-9])(?P<VAL>[0-9a-fA-F]{32})(?![A-Za-z0-9])"), "VAL"),

    #     #     # KeywordDetector (일반 할당형)
    #     #     ("kv_quoted", re.compile(rf'(?i)\b{key_kv}\b{sep}"(?P<VAL>[^"\r\n]{{6,}})"'), "VAL"),
    #     #     ("kv_single_quoted", re.compile(rf"(?i)\b{key_kv}\b{sep}'(?P<VAL>[^'\r\n]{{6,}})'"), "VAL"),
    #     #     ("kv_bare", re.compile(rf"(?i)\b{key_kv}\b{sep}(?P<VAL>[^\s\"'`]{{8,}})"), "VAL"),

    #     #     # OpenAI/Custom-like
    #     #     ("openai_like", re.compile(r"\b(?P<VAL>sk-[A-Za-z0-9]{16,})\b"), "VAL"),
    #     #     # 사내/커스텀 접두(예: ak-, tk- ... -dev/-test 꼬리)
    #     #     ("ak_tk_token", re.compile(r"\b(?P<VAL>(?:ak|tk)-[a-f0-9]{16,}(?:-(?:dev|test)[a-z0-9]*)?)\b"), "VAL"),

    #     # ]


