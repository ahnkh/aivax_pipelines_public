
import hashlib
import re
# docx 파싱, 빠른 속도
import docx2txt

import magic

# import os
import fitz  # PyMuPDF

# 느린 속도, 제거
# from docx import Document
from pptx import Presentation
from openpyxl import load_workbook
import olefile

from multiprocessing import Pool, cpu_count

#외부 라이브러리
from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.helper.filter_pattern_base import FilterPatternBase

# 그룹별 regex filter
from block_filter_modules.filter_policy.groupfilter.filter_policy_group_data import FilterPolicyGroupData

# 별도 helper
from block_filter_modules.filter_pattern.helper.regex_policy_helper.regex_policy_generate_helper import RegexPolicygenerateHelper


'''
file filter 패턴, 
TODO: 기존 pipeline 패턴과 동일 패턴으로, 신규 추가
제공 기능 :
- 탐지 시점에 file 정보를 분석하고, 텍스트를 추출한다.
- 정책을 수신 받으며 (별도의 DB 정책), 정책에 의해서 파일내 민감정보를 추출한다.
- Masking 기능은 불필요, 차단 여부를 선택한다.
- 결과는 기존 BlockFilter과 유사 패턴으로 제공한다.

TODO: 파일명 체크, 절대 경로이면 그대로 사용하고, 상대경로이면 지정된 경로에서 가져온다.
- 1차 개발은 절대 경로로 지정한다.
'''

class FileBlockFilterPattern(FilterPatternBase):
    
    POLICY_FILTER_KEY = DBDefine.FILTER_KEY_BLOCK_FILE
    
    def __init__(self):
        
        super().__init__()
        
        # DB 정책 패턴, 향후 추가
        '''
        [
            A 정책 타입 => 1개만 필요할것 같다.
            {"정책코드", "정책명", "파일패턴", "포함/제외", "파일사이즈"}
            
            B 정책 타입 - 정규식 패턴, 이건 기존 정규식 사용
        ]
        
        # 기본 값은 정책 대신 설정을 사용
        - 동시 파일 개수 : 최대 10개 제한
        - 정규식 범위, 10만글자 => 이건 테스트후.
        - 그렇다면, 이 패턴은 regexfilter로 요청하는 구조여야 할듯 한데. 좀더 고민     
        - detect secret을 그대로 사용하되, 기존 span,masking을 분리하자.   
        '''
        
        #regex 패턴, scope 단위로 관리
        self.__dictDBScopeRegexPattern:dict = None
        
        #helper 추가
        self.__regexPolicyGenerateHelper:RegexPolicygenerateHelper = None        
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
        
        return ERR_OK
    
    # 패턴 탐지, 이름은 동일, 파라미터, 전달 인자는 상이.
    def DetectPattern(self, lstFileName:list, dictOuputResponse:dict):
        
        '''
        다수의 파일을 요청받는다. 각 파일에 대해서 각 정책 조건을 확인한다.
        파일중 하나라도 차단이면, 모두 차단이다.
        정책은 파일명, 유형, 사이즈, 타입으로 파일 분석전에 필터, 해당되면 차단이고
        이후 파일의 데이터를 추출, 차단한다.
        
        파일명외의 설정은 정책 또는 상수로 관리한다.
        
        파일의 동시 분석개수 제한도 설정한다.        
        TODO: 파일이 추출되었으면, hash및 history 정보는 수집한다. 단, 메모리에서만 보관한다. (오탐시 재기동 대응)
        '''
        
        #1차 테스트, 임의로 테스트, 경로는 절대 경로를 가정한다.
        #테스트, 로그로 확인한다.
        
        #응답 데이터 설계
        #최종 차단/실패 필요
        #차단된 파일 , 정책
        # 각 파일별 이력 (파일명, 속성, 차단 결과), 앞에서 차단되면 미수행 (state 정의 필요)
        # TODO: 차단은 하나더라도, 모든 파일을 탐지해야 한다.
        '''
        {
            "file_summary": {
                "action" : [block/pass],
                
                "policy_code" : 1,
                "policy_name" : "test",
                "file" : "test.docx"                    
            },
            
            "file_detail":[
                {"file":"test.docx", "size":10, "hash":"", "action":""}\                
            ]
        }
        '''
        
        #TODO: 우선 테스트, 한번에 읽어 보자.
        # nCPUCount = int(cpu_count() * 0.5)

        # 단일, 또는 소수의 파일일때는, 멀티 프로세스가 더 느릴수 있다.        
        # with Pool(processes=nCPUCount) as pool:
        #     results = pool.map(self.read_file_worker, lstFileName)
        
        # file 별로, 추출하고 정규식을 반영해 본다.
        # TODO: 정책의 구조는 기존과 동일하되, 파일 분석 시점에는 uuid, service type을 알수 없어서
        # default 정책만 설정 가능하도록 설정한다.
        
        lstFileStatus:list = []
        
        for strFileName in lstFileName:
            
            # 각 파일별 결과, list가 낫겠다.
            dictEachFileOutput:dict = {
                ApiParameterDefine.OUT_ACTION : PipelineFilterDefine.ACTION_ALLOW,
                ApiParameterDefine.FILE_NAME : strFileName,
                
                ApiParameterDefine.POLICY_ID : "",
                ApiParameterDefine.POLICY_NAME : "",
            }
            
            self.__detectEachFileAt(strFileName, dictEachFileOutput)
            
            # 개별 차단 결과의 저장 (모든 파일에 대해서는 탐지를 수행한다. (파일 개수에 다른 병렬처리 검토)
            lstFileStatus.append(dictEachFileOutput)
            
            #최종 결과, 하나라도 차단이 되었으면, 차단이다.
            strAction:str = dictEachFileOutput.get(ApiParameterDefine.OUT_ACTION)
            
            #TODO: 응답, 하나라도 걸리면 차단
            if PipelineFilterDefine.ACTION_BLOCK == strAction:
                
                #이건 이상태로, BLOCK이면 계속 업데이트
                dictOuputResponse[ApiParameterDefine.OUT_ACTION] = dictEachFileOutput.get(ApiParameterDefine.OUT_ACTION)
                
                # # 반드시 존재하는 값
                # dictFileSummary:dict = dictFileDetectResult.get(ApiParameterDefine.FILE_SUMMARY)
                
                # #TODO: 이름 나중에 정리
                # strAction:str = dictFileSummary.get(ApiParameterDefine.OUT_ACTION)
                
                # if PipelineFilterDefine.ACTION_ALLOW == strAction:
                    
                #     #TODO: 더 좋은 방법을 찾을것.
                #     dictFileSummary[ApiParameterDefine.OUT_ACTION] = dictEachFileOutput.get(ApiParameterDefine.OUT_ACTION)
                    
                #     #TODO: file 별로 저장, 이건 불필요.
                #     # dictFileSummary[ApiParameterDefine.POLICY_ID] = dictEachFileOutput.get(ApiParameterDefine.POLICY_ID)
                #     # dictFileSummary[ApiParameterDefine.POLICY_NAME] = dictEachFileOutput.get(ApiParameterDefine.POLICY_NAME)
                    
                # pass
            # pass
            
        dictOuputResponse[ApiParameterDefine.FILE_SUMMARY] = lstFileStatus
        
        return ERR_OK
    
    def notifyUpdateDBPatternPolicy(self, filterPolicyGroupData:FilterPolicyGroupData) -> int:
        
        '''
        '''
        
        #test, regex 패턴으로 변경
        # strFilterKey:str = DBDefine.FILTER_KEY_REGEX
        strFilterKey:str = FileBlockFilterPattern.POLICY_FILTER_KEY
        
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
    
    ####################################### private
    
    def __detectEachFileAt(self, strFileName:str, dictEachFileOutput:dict):
        
        '''
        파일 타입을 읽고, 그 파일에 따라 파일을 읽는 모듈을 분기한다.
        
        2단계, 먼저 파일의 컨텐츠를 가져오고 => 이게 프롬프트 개념
        이후 regex 정책으로 테스트 한다. 정책은 default만 지원, uuid, servicetype을 알수 없다.
        '''
        
        strMimeType:str = magic.from_file(strFileName, mime=True)
        
        #file 유형, 파일 확장자가 아닌, mimetype으로 분기, dict
        
        #TODO: 기타 정보 수집
        #TODO: 리펙토링은 나중, 우선 만들어 보자.
        
        stat = os.stat(strFileName)
        
        dictEachFileOutput[ApiParameterDefine.FILE_INFO] = {
            "mime_type" : strMimeType,
            "file_ext" : FileDefine.FILE_EXT.get(strMimeType, FileDefine.FILE_EXT_UNKNOWN),
            "size" : stat.st_size,
            "hash" : hashlib.sha256(open(strFileName,'rb').read()).hexdigest()
        }
        
        # self.__detectGetFileType(strFileName)
        
        #TODO: size가 방대하여, 정규식을 사용할수 없는지 확인 필요. 1차는 미확인
        strContents:str = ""
        
        if FileDefine.MIME_DOCX == strMimeType:
        
            # 텍스트 추출, 테스트,word 만 테스트
            strContents = docx2txt.process(strFileName)
            
        elif FileDefine.MIME_HWP == strMimeType:
            pass
        
        else:
            raise Exception (f"unsupported file type {strMimeType}")
        
        # 텍스트에 대해서, 정책을 반영한다. 우선 틀을 잡고 향후 DB에 반영
        
        # 여기서 정규식 매칭.
        # 우선 테스트
        nContentsLen:int = len(strContents)
        
        LOG().info(f"read document contents, len = {nContentsLen}")
        
        #이걸 프롬프트로, regex 필터에 요청하고, 결과로 차단/탐지, 정책명을 수집한다.
        # filterid만 바꾸면, 재활용 가능하다.
        
        # 중복된 코드이나, 약간 다른 부분이 많아서, 두번 작성한다.
        listDefaultPattern:list = self.__dictDBScopeRegexPattern.get(DBDefine.POLICY_FILTER_SCOPE_DEFAULT)
        
        # 정규 표현식, TODO: 차단, 탐지만 확인하면 된다.        
        self.__detectDefaultRegexPattern(listDefaultPattern, strContents, dictEachFileOutput)
        
        return ERR_OK
    
    def __detectDefaultRegexPattern(self, listDBRegexPattern:list, strPromptText:str, dictEachFileOutput: dict):
        
        '''
        '''
        
        for dictDBPattern in listDBRegexPattern:
            
            bBlockContent:bool = self.__detectFilterPatternAt(strPromptText, dictEachFileOutput, dictDBPattern)
            
            if True == bBlockContent:
                
                #테스트
                LOG().info(f"block contents")
                return True
        
        return ERR_OK
    
    #개별 dictionary 별 정책 조회
    def __detectFilterPatternAt(self, strPromptText:str, dictEachFileOutput:dict, dictDBPattern:dict) -> bool:

        '''
        TODO: 하나라도 걸리면, 차단이다.
        
        '''

        # 정책
        id:str = dictDBPattern.get("id")
        name:str = dictDBPattern.get("name")

        #차단, 마스킹 무시 향후 비활성화면 검토
        # action:str = dictDBPattern.get("action")
        
        # regex_flag:int = int(dictDBPattern.get("regex_flag"))
        regex_group:int = (dictDBPattern.get("regex_group"))
        regex_group_val:str = dictDBPattern.get("regex_group_val")

        regex_pattern:re.Pattern = dictDBPattern.get("regex_pattern")

        #group 여부인지, 아닌지에 따른 분기, 여기는, 우선 나누지 않는다.

        #TODO: 1차 예외처리
        if None == regex_pattern:

            LOG().error(f"invalid regex pattern, id = {id}, name = {name}, skip")
            return ERR_FAIL

        #그룹정책, TODO: 현재 UI에서 개발되어져 있지 않다.
        if CONFIG_OPT_ENABLE == regex_group:
            
            #TODO: 단순하게 정책에  포함되면, 차단이다. 테스트 필요.
            for m in regex_pattern.finditer(strPromptText):
                
                #테스트 로그
                rule:str = dictDBPattern.get("rule")
                
                LOG().info(f"block file text, id = {id}, name = {name}, rule = {rule}")
                
                # 차단 시점의 정책 추가
                dictEachFileOutput[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
                dictEachFileOutput[ApiParameterDefine.POLICY_ID] = id
                dictEachFileOutput[ApiParameterDefine.POLICY_NAME] = name
                
                return True
                
                # if regex_group_val and regex_group_val in m.groupdict():
                #     s, e = m.span(regex_group_val)
                # else:
                #     s, e = m.span(0)

                # self.__add_span(spans, s, e)
                # counts[action] += 1
                # dictCount[action] = dictCount.get(action,0) + 1

                # self.__assignFirstDetectedRule(dictDetectRule, id, name)
        else:
            
            for m in regex_pattern.finditer(strPromptText):
                # self.__add_span(spans, m.start(), m.end())
                # counts[action] += 1
                # dictCount[action] = dictCount.get(action,0) + 1

                # self.__assignFirstDetectedRule(dictDetectRule, id, name)
                
                # 차단 시점의 정책 추가
                dictEachFileOutput[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
                dictEachFileOutput[ApiParameterDefine.POLICY_ID] = id
                dictEachFileOutput[ApiParameterDefine.POLICY_NAME] = name
                
                return True
                # pass

        #안걸렸으면, 다음 정규식
        return False
    
    # file 확장 타입, 분기
    def __getFileExt(self, strMimeType:str) -> str:
        
        '''
        '''
        
        return ""
    
    # # 파일을 읽는 로직 분리,mimetype에 따른 분기
    # # string 반환은, 감당하자. string을 저장하는건 메모리 부담이 크다.
    # def __readDocument(self, strFileName) -> str:
        
    #     '''
    #     '''
        
        
    #     return strContents
    
    # # 파일 유형의 감지, 우선 개발
    # def __detectGetFileType(self, strFileName:str):
    #     '''
    #     '''
        
    #     mime = magic.from_file(strFileName, mime=True)
        
    #     LOG().info(f"mime = {mime}")
        
    #     return ERR_OK
    
    
    # def read_file_worker(self, path: str):
        
    #     try:
    #         return path, self.read_docx(path)
        
    #     #TODO: 예외처리는 나중.
    #     except Exception as e:
    #         return path, f"[ERROR] {e}"
        
        
    # def read_docx(self, path: str) -> str:
    #     doc = Document(path)
    #     return "\n".join(paragraph.text for paragraph in doc.paragraphs)


