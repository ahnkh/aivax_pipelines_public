
import hashlib
import re
# docx 파싱, 빠른 속도
# import docx2txt

import magic

# import os
# import fitz  # PyMuPDF

# 느린 속도, 제거
# from docx import Document
from pptx import Presentation
from openpyxl import load_workbook
# import olefile

from multiprocessing import Pool, cpu_count

#외부 라이브러리
from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.helper.filter_pattern_base import FilterPatternBase

# 그룹별 regex filter
from block_filter_modules.filter_policy.groupfilter.filter_policy_group_data import FilterPolicyGroupData

# 별도 helper
from block_filter_modules.filter_pattern.helper.regex_policy_helper.regex_policy_generate_helper import RegexPolicygenerateHelper

from block_filter_modules.local_define.office_document_reader_ex import OfficeDocumentReaderEx

from block_filter_modules.filter_pattern.helper.office_file_block_helper.office_file_analyze_helper import OfficeFileAnalyzeHelper

from block_filter_modules.filter_pattern.helper.office_file_block_helper.office_watermark_detect_helper import OfficeWaterMarkDetectHelper

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
        
        #file Filter
        self.__officeReader:OfficeDocumentReaderEx = None
        
        # local 설정값.
        self.__dictFileBlockInfoLocalConfig:dict = None
        
        # file 설정, 제한값, DB에서 가져온다. local 설정의 업데이트는 하지 않는다. (참조만)
        self.__dictFileBlockDBConfig:dict = None
        
        # office file - 상세 분석 모듈
        self.__officeFileAnalyzeHelper:OfficeFileAnalyzeHelper = None
        
        # office file - watermark 처리 모듈
        self.__officeWaterMarkDetectHelper:OfficeWaterMarkDetectHelper = None
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
        
        self.__dictFileBlockInfoLocalConfig:dict = {}
        
        #file 설정, 제한값
        self.__dictFileBlockDBConfig = {}
        
        self.__regexPolicyGenerateHelper:RegexPolicygenerateHelper = RegexPolicygenerateHelper()
        
        self.__officeReader:OfficeDocumentReaderEx = OfficeDocumentReaderEx()
        
        self.__officeFileAnalyzeHelper:OfficeFileAnalyzeHelper = OfficeFileAnalyzeHelper()
        self.__officeFileAnalyzeHelper.Initialize()
        
        self.__officeWaterMarkDetectHelper:OfficeWaterMarkDetectHelper = OfficeWaterMarkDetectHelper()
        self.__officeWaterMarkDetectHelper.Initialize()
        
        #local 설정 정보, 읽어온다.
        self.__readLocalConfig(dictJsonLocalConfigRoot, self.__dictFileBlockInfoLocalConfig, self.__dictFileBlockDBConfig)
        
        # watermark 탐지 heper, 
        # 우선 local 정책으로 만든다.
        self.__readWatermarkLocalConfig(dictJsonLocalConfigRoot, self.__officeWaterMarkDetectHelper)
        
        
        return ERR_OK
    
    # 패턴 탐지, 이름은 동일, 파라미터, 전달 인자는 상이.
    def DetectPattern(self, lstAttachFile:list, dictOuputResponse:dict):
        
        '''
        다수의 파일을 요청받는다. 각 파일에 대해서 각 정책 조건을 확인한다.
        파일중 하나라도 차단이면, 모두 차단이다.
        정책은 파일명, 유형, 사이즈, 타입으로 파일 분석전에 필터, 해당되면 차단이고
        이후 파일의 데이터를 추출, 차단한다.
        
        파일명외의 설정은 정책 또는 상수로 관리한다.
        
        파일의 동시 분석개수 제한도 설정한다.        
        TODO: 파일이 추출되었으면, hash및 history 정보는 수집한다. 단, 메모리에서만 보관한다. (오탐시 재기동 대응)
        '''
        
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
        
        # file 별로, 추출하고 정규식을 반영해 본다.
        # TODO: 정책의 구조는 기존과 동일하되, 파일 분석 시점에는 uuid, service type을 알수 없어서
        # default 정책만 설정 가능하도록 설정한다.
        
        lstFileStatus:list = []
        
        #파일의 실제 경로, 설정 정보와 조합한다.
        # strAttachFileRealPath:str = ""
        
        # attach_file_base_dir:str = self.__dictFileBlockInfoLocalConfig.get("attach_file_base_dir")
        file_read_timeout:int = self.__dictFileBlockInfoLocalConfig.get("file_read_timeout")
        content_chunk_size:int = self.__dictFileBlockInfoLocalConfig.get("content_chunk_size")
        
        # for strFileName in lstAttachFile:
        for dictFileInfo in lstAttachFile:
            
            id:str = dictFileInfo.get("id") #TODO: 실제 파일 경로
            # dictFileInfo.get("size") #TODO: 불필요 => 실제 파일 사이즈
            strFileName:str = dictFileInfo.get("name") # File Alias
            # dictFileInfo.get("mime_type") #TODO: 불필요
            
            if 0 == len(id) or 0 == len(strFileName):
                continue
            
            # 사양변경,id가 실제 파일 경로 이 로직은 불필요
            #strAttachFileRealPath = f"{attach_file_base_dir}/{strFileName}"
                        
            # 각 파일별 결과, list가 낫겠다. => TODO: UI에서는 ACCEPT로 바라본다.
            dictEachFileOutput:dict = {
                ApiParameterDefine.OUT_ACTION : PipelineFilterDefine.ACTION_BLANK, #TODO: 정책, 탐지되지 않았으면 공백이다. accept, block, masking은 정책으로 탐지한다.
                ApiParameterDefine.FILE_NAME : strFileName,
                
                ApiParameterDefine.POLICY_ID : "",
                ApiParameterDefine.POLICY_NAME : "",
            }
            
            strRealOfficeFilePath:str = id #변수 가독성, 별도 변수로 선언
            
            #TODO: watermark가 포함된 파일은 일반적인 파일 분석으로는 안된다.
            #여기서 분기 필요., 결과처리는 최종 합치는 시점에 고려, 
            # 결과 데이터는, EachOutput, ACTION 과 POLICY_NAME으로 처리 (향후 추가적인 분기, 분석 데이터 수집 필요)
            # TODO: opensearch 3개의 로그에 모든걸 담으려니 복잡도가 너무 늘어났다.
            bBlockWaterMark:bool = self.__detectOfficeFileIncludeWaterMark(strRealOfficeFilePath, dictEachFileOutput)
            
            #TODO: watermark가 포함되어 있으면 해당 파일은 탐지 하지 않는다. 
            #TODO: 향후 정책 제어, 나머지를 전부 탐지할지, 차단할지 결정. sslproxy와 같이 검토 필요 (정리후 리펙토링)
            #TODO: 마지막에 summary에 저장하는 것으로 관리하자. 나중에 로그에 대해서는 다시 정리
            # 여기서는 분기문으로 처리한다.
            if True == bBlockWaterMark:
                
                # 결과 데이터 수집은 함수 내부에서 처리하고, 여기에서는 분기 처리.
                
                # 향후 UI의 표현력 개선을 위해서 reson외 부가 정보를 전달한다.
                # 지금은 성능을 위해서, watermark가 포함되면 종료한다. (평균적으로 파일은 1개 남짓으로 업로드 할것으로 예상된다.)
                #return ERR_OK
                pass
            else: #watermark가 아닌 파일만 탐지, 여기는 나중에 다시 개선
            
                self.__detectEachFileAt(strRealOfficeFilePath, dictEachFileOutput, file_read_timeout, content_chunk_size)
            
            # 개별 차단 결과의 저장 (모든 파일에 대해서는 탐지를 수행한다. (파일 개수에 다른 병렬처리 검토)
            lstFileStatus.append(dictEachFileOutput)
            
        dictOuputResponse[ApiParameterDefine.FILE_SUMMARY] = lstFileStatus
        
        #TODO: 최종 정책 판단, 정책의 code를 mode에 넣어준다. regex, file 모두 같은 로직을 제공한다.
        #현재 코드대로, block이 걸리면 무조건 block으로.
        #TODO: 우선 최종 mode, action 값만 추출한다. 
        dictDetectPolicy:dict = self.__decideFinalPolicyAction(lstFileStatus)
        
        # 일단 PolicyAction을 반영한다.
        strPolicyAction:str = dictDetectPolicy.get(ApiParameterDefine.OUT_ACTION, PipelineFilterDefine.ACTION_BLANK)
        strPolicyID:str = dictDetectPolicy.get(ApiParameterDefine.POLICY_ID, "")
        strPolicyName:str = dictDetectPolicy.get(ApiParameterDefine.POLICY_NAME, "")
        
        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = strPolicyAction
        dictOuputResponse[ApiParameterDefine.POLICY_ID] = strPolicyID
        dictOuputResponse[ApiParameterDefine.POLICY_NAME] = strPolicyName
        
        return ERR_OK
    
    # 정책 DB 데이터 수신
    def notifyUpdateDBPatternPolicy(self, filterPolicyGroupData:FilterPolicyGroupData, dictOutputResponse:dict) -> int:
        
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
    
    # 파일 정보, 확장자 사이즈등 제한값, 이것도 notify로, customize
    def notifyCustomUpdateFileBlockInfo(self, dictFileBlockPolicy:dict):
        
        '''
        '''
        
        lstFileBlockAllowExt:list = dictFileBlockPolicy.get(FileDefine.DB_POLICY_FILE_BLOCK_ALLOW_EXT)
        nFileBlockMaxSize:int = dictFileBlockPolicy.get(FileDefine.DB_POLICY_FILE_BLOCK_MAX_SIZE, -1)
        
        # 기본 예외처리만, 아무것도 허용 안할수도 있다.
        if None == lstFileBlockAllowExt or 0 > nFileBlockMaxSize:
            # TODO: Log Queue, 한번만 출력하는 로거 추가, 이후 로그 정리.
            # LOG().error(f"invalid file block policy, no allow ext and max size, skip")
            return ERR_OK
        
        # 확장자, 제한값, 기본값 추가, 설정이 잘못되면, 모두 차단이다.
        self.__dictFileBlockDBConfig[FileDefine.DB_POLICY_FILE_BLOCK_ALLOW_EXT] = lstFileBlockAllowExt
        self.__dictFileBlockDBConfig[FileDefine.DB_POLICY_FILE_BLOCK_MAX_SIZE] = nFileBlockMaxSize
        
        return ERR_OK
    
    ####################################### private
    
    # 최종 정책의 반영, UI 용도, sslproxy도 동일 연산으로 제공
    def __decideFinalPolicyAction(self, lstFileStatus:list):
        
        '''
        '''
        
        #TODO: 집계쪽이 낫겠다.
        dictDetectPolicy:dict = {}
        
        for dictEachFileOutput in lstFileStatus:
            
            strPolicyAction:str = dictEachFileOutput.get(ApiParameterDefine.OUT_ACTION)
            
            # 향후를 위해 추가, 일단 케이스가 많지 않다.
            dictDetectPolicy[strPolicyAction] = dictEachFileOutput
            #pass
            
        #일단 이렇게 작성.. 향후 개선
        dictRule:dict = dictDetectPolicy.get(PipelineFilterDefine.ACTION_BLOCK)
        
        if None != dictRule:
            return dictRule
        
        dictRule:dict = dictDetectPolicy.get(PipelineFilterDefine.ACTION_MASKING)
        
        if None != dictRule:
            return dictRule
        
        dictRule:dict = dictDetectPolicy.get(PipelineFilterDefine.ACTION_ACCEPT)
        
        if None != dictRule:
            return dictRule
        
        # 기존 regex 패턴과 동일한 방식, 다만 재사용을 고려할 코드는 아니다.
        #allow 는 없으면 공백 반환
        dictRule:dict = dictDetectPolicy.get(PipelineFilterDefine.ACTION_ALLOW, {})
        
        return dictRule
        
        # return ERR_OK
    
    def __detectEachFileAt(self, strFilePath:str, dictEachFileOutput:dict, nFileReadTimeout:int, nContentChunkSize:int):
        
        '''
        파일 타입을 읽고, 그 파일에 따라 파일을 읽는 모듈을 분기한다.
        
        2단계, 먼저 파일의 컨텐츠를 가져오고 => 이게 프롬프트 개념
        이후 regex 정책으로 테스트 한다. 정책은 default만 지원, uuid, servicetype을 알수 없다.
        '''
        
        strMimeType:str = magic.from_file(strFilePath, mime=True)
        
        #file 유형, 파일 확장자가 아닌, mimetype으로 분기, dict
        
        #TODO: 기타 정보 수집
        #TODO: 리펙토링은 나중, 우선 만들어 보자.
        
        stat = os.stat(strFilePath)
        
        strFileExt:str = FileDefine.FILE_EXT.get(strMimeType, FileDefine.FILE_EXT_UNKNOWN)
        nFileSize:int = stat.st_size
        
        dictEachFileOutput[ApiParameterDefine.FILE_INFO] = {
            "mime_type" : strMimeType,
            "file_ext" : strFileExt,
            "size" : nFileSize,
            "hash" : hashlib.sha256(open(strFilePath,'rb').read()).hexdigest()
        }
        
        #TODO: 여기 지저분, 나중에 개선, 분리는 필요.
        bAllowFileExt:bool = True
        strReason:str = ""
        
        (bAllowFileExt,strReason) = self.__isAllowFileExtAndSize(strFileExt, nFileSize)
        
        if False == bAllowFileExt:
        
            dictEachFileOutput[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
            dictEachFileOutput[ApiParameterDefine.POLICY_ID] = "" #정책은 없다.
            dictEachFileOutput[ApiParameterDefine.POLICY_NAME] = strReason
            
            return ERR_OK
        
        # self.__detectGetFileType(strFileName)
        
        #TODO: size가 방대하여, 정규식을 사용할수 없는지 확인 필요. 1차는 미확인
        # strContents:str = ""
                
        strContents:str = self.__readOfficeFileContents(strMimeType, strFilePath, nFileReadTimeout)
        
        # 텍스트에 대해서, 정책을 반영한다. 우선 틀을 잡고 향후 DB에 반영
        
        # 수집이 되던, 안되던, chunk 저장, 256 byte
        strChunk:str = strContents[:nContentChunkSize]
        dictEachFileOutput[ApiParameterDefine.FILE_INFO]["chunk"] = strChunk
        
        # 여기서 정규식 매칭.
        # 우선 테스트
        # nContentsLen:int = len(strContents)        
        # LOG().info(f"read document contents, len = {nContentsLen}")
        
        #이걸 프롬프트로, regex 필터에 요청하고, 결과로 차단/탐지, 정책명을 수집한다.
        # filterid만 바꾸면, 재활용 가능하다.
                
        listDefaultPattern:list = self.__dictDBScopeRegexPattern.get(DBDefine.POLICY_FILTER_SCOPE_DEFAULT)
        
        # 차단, masking이 되었으면, 2차 상세 분석을 진행한다. 
        # 내부에서 MimeType으로 가능한 컨텐츠를 식별한다. (doc, docx, 등등)
        # pdf 분석단계가 있어 세부 조정이 필요하다. => 별도의 helper
        
        parameterItem = OfficeFileAnalysisParameterItem(
                file_path = strFilePath,
                mime_type = strMimeType,
                read_timeout = nFileReadTimeout
            )
        
        # 정규 표현식, TODO: 차단, 탐지만 확인하면 된다.        
        self.__detectDefaultRegexPattern(listDefaultPattern, strContents, dictEachFileOutput, parameterItem)
        
        return ERR_OK
    
    def __detectDefaultRegexPattern(self, listDBRegexPattern:list, strPromptText:str, dictEachFileOutput: dict, parameterItem: OfficeFileAnalysisParameterItem):
        
        '''
        '''
        
        for dictDBPattern in listDBRegexPattern:
            
            bBlockContent:bool = self.__detectFilterPatternAt(strPromptText, dictEachFileOutput, dictDBPattern)
            
            # 정책에 걸렸으면, 그 DB 정책을 patareterItem에 추가해서 상세 분석 요청
            if True == bBlockContent:
                
                #테스트
                # LOG().info(f"block contents")
                parameterItem.regex_pattern = dictDBPattern
                
                self.__analyzeFileBlockDetailReason(parameterItem, dictEachFileOutput)
                return True
        
        return False
    
    #개별 dictionary 별 정책 조회
    def __detectFilterPatternAt(self, strPromptText:str, dictEachFileOutput:dict, dictDBPattern:dict) -> bool:

        '''
        TODO: 하나라도 걸리면, 차단이다.
        
        '''

        # 정책
        id:str = dictDBPattern.get("id")
        name:str = dictDBPattern.get("name")

        #차단, 마스킹 무시 향후 비활성화면 검토
        action:str = dictDBPattern.get("action")
        rule:str = dictDBPattern.get("rule")
        
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
        #TODO: 그룹 분기는, 우선 제외한다. (개발이 필요한 건이나, 현재 미 개발된 건이다.)
        
        # if CONFIG_OPT_ENABLE == regex_group:
            
        #     #TODO: 단순하게 정책에  포함되면, 차단이다. 테스트 필요.
        #     for m in regex_pattern.finditer(strPromptText):
                                
        #         # LOG().info(f"block file text, id = {id}, name = {name}, rule = {rule}")
                
        #         # 차단 시점의 정책 추가
        #         dictEachFileOutput[ApiParameterDefine.OUT_ACTION] = action
        #         dictEachFileOutput[ApiParameterDefine.POLICY_ID] = id
        #         dictEachFileOutput[ApiParameterDefine.POLICY_NAME] = name
                
        #         # dictEachFileOutput[ApiParameterDefine.POLICY_RULE] = rule
                
        #         return True
                
        #         # if regex_group_val and regex_group_val in m.groupdict():
        #         #     s, e = m.span(regex_group_val)
        #         # else:
        #         #     s, e = m.span(0)

        #         # self.__add_span(spans, s, e)
        #         # counts[action] += 1
        #         # dictCount[action] = dictCount.get(action,0) + 1

        #         # self.__assignFirstDetectedRule(dictDetectRule, id, name)
        # else:
            
        #     for m in regex_pattern.finditer(strPromptText):
        #         # self.__add_span(spans, m.start(), m.end())
        #         # counts[action] += 1
        #         # dictCount[action] = dictCount.get(action,0) + 1

        #         # self.__assignFirstDetectedRule(dictDetectRule, id, name)
                
        #         # 차단 시점의 정책 추가
        #         dictEachFileOutput[ApiParameterDefine.OUT_ACTION] = action
        #         dictEachFileOutput[ApiParameterDefine.POLICY_ID] = id
        #         dictEachFileOutput[ApiParameterDefine.POLICY_NAME] = name
        #         # dictEachFileOutput[ApiParameterDefine.POLICY_RULE] = rule
                
        #         return True
        #         # pass
                
        for m in regex_pattern.finditer(strPromptText):
            # self.__add_span(spans, m.start(), m.end())
            # counts[action] += 1
            # dictCount[action] = dictCount.get(action,0) + 1

            # self.__assignFirstDetectedRule(dictDetectRule, id, name)
            
            # 차단 시점의 정책 추가
            dictEachFileOutput[ApiParameterDefine.OUT_ACTION] = action #DB에 저장된 action
            dictEachFileOutput[ApiParameterDefine.POLICY_ID] = id
            dictEachFileOutput[ApiParameterDefine.POLICY_NAME] = name
            # dictEachFileOutput[ApiParameterDefine.POLICY_RULE] = rule
            
            return True

        #안걸렸으면, 다음 정규식
        return False
    
    #watermak 파일의 포함여부 탐지
    def __detectOfficeFileIncludeWaterMark(self, strRealOfficeFilePath:str, dictEachFileOutput:dict) -> bool:
        
        '''
        watermark가 식별되었으면 차단이다.
        watermark로직은 회사마다 상이할수 있어 옵션화가 필요하다.
        우선 개발후 분기
        '''
        
        bDetectWaterMark:bool = self.__officeWaterMarkDetectHelper.DetectOfficeFileWithWaterMark(strRealOfficeFilePath, dictEachFileOutput)
        
        #TODO: 응답에 따른 분기, watermark가 존재하는 파일이면, 해당 파일은 탐지를 중단한다.
        #결과 데이터, 저장 로직 존재, 추가 고려
        
        return bDetectWaterMark
    
    def __isAllowFileExtAndSize(self, strFileExt:str, nFileSize:int):
        
        '''
        '''
        
        # 사이즈, 확장자 체크 => 일단 메모리 연산이니, 가독성 차원에서 이정도 비용은 감수한다.
        lstFileBlockAllowExt:list = self.__dictFileBlockDBConfig.get(FileDefine.DB_POLICY_FILE_BLOCK_ALLOW_EXT)
        nFileBlockMaxSize:int = self.__dictFileBlockDBConfig.get(FileDefine.DB_POLICY_FILE_BLOCK_MAX_SIZE)
        
        # bBlock:bool = False
        strReason:str = "" #사유, 일단 임의의 문자열
        
        # File 확장자 제한
        if not (strFileExt in lstFileBlockAllowExt):
            # strExtension = ",".join(lstFileBlockAllowExt)
            strReason = f"{FileDefine.BLOCK_REASON_FILE_EXT_LIMIT} ({strFileExt})"
            return (False, strReason)
        
        # file size 제한
        if nFileBlockMaxSize < nFileSize:
            strReason = f"{FileDefine.BLOCK_REASON_FILE_SIZE_LIMIT} ({nFileSize} / {nFileBlockMaxSize})"
            return (False, strReason)
        
        return (True,"")
    
    # 과거 자원정보, 저장이 되어야 한다.
    def __readLocalConfig(self, dictJsonLocalConfigRoot:dict, dictFileBlockInfoLocalConfig:dict, dictFileBlockDBConfig:dict):
        
        '''
        '''
        
        file_block_filter_module:dict = dictJsonLocalConfigRoot.get("file_block_filter_module")
        
        # 그대로 저장한다. local config는 불변이다.
        dictFileBlockInfoLocalConfig.update(file_block_filter_module)
        
        file_allow_ext:list = file_block_filter_module.get("file_allow_ext", [])
        file_max_size:list = file_block_filter_module.get("file_max_size", 0)
        
        # 확장자, 제한값, 기본값 추가, 설정이 잘못되면, 모두 차단이다.
        dictFileBlockDBConfig[FileDefine.DB_POLICY_FILE_BLOCK_ALLOW_EXT] = file_allow_ext
        dictFileBlockDBConfig[FileDefine.DB_POLICY_FILE_BLOCK_MAX_SIZE] = file_max_size
        
        return ERR_OK
    
    # watermark에 대한 정책,우선 local로 설정하고, 향후 DB, UI 정책으로 만든다.
    def __readWatermarkLocalConfig(self, dictJsonLocalConfigRoot:dict, officeWaterMarkDetectHelper:OfficeWaterMarkDetectHelper):
        
        '''
        '''
        
        file_block_filter_module:dict = dictJsonLocalConfigRoot.get("file_block_filter_module")
        
        #TODO: 가공하지 않고, 그대로 전달한다.
        office_watermark_filter:dict = file_block_filter_module.get("office_watermark_filter")
        
        officeWaterMarkDetectHelper.UpdateWatermarkPolicy(office_watermark_filter)
        
        return ERR_OK
    
    
    # # 파일을 읽는 로직 분리,mimetype에 따른 분기, string 참조의 전달은.. 감수하자.
    def __readOfficeFileContents(self, strMimeType:str, strFilePath:str, nFileReadTimeout:int) -> str:
        
        '''
        '''
        
        if FileDefine.MIME_DOCX == strMimeType or FileDefine.MIME_DOCX_V2 == strMimeType:
        
            # 텍스트 추출, 테스트,word 만 테스트
            # strContents = docx2txt.process(strFileName)            
            strContents = self.__officeReader.ReadDocxToText(strFilePath)
            
        elif FileDefine.MIME_DOC == strMimeType:            
            strContents = self.__officeReader.ReadDocToText(strFilePath, nFileReadTimeout)
            
        elif FileDefine.MIME_HWP == strMimeType:
            strContents = self.__officeReader.ReadHwpToText(strFilePath, nFileReadTimeout)
            # pass
        
        elif FileDefine.MIME_HWPX == strMimeType:
            strContents = self.__officeReader.ReadHwpxToText(strFilePath)
            # pass
            
        elif FileDefine.MIME_PDF == strMimeType:
            strContents = self.__officeReader.ReadPdfToText(strFilePath)
            
        elif FileDefine.MIME_PPT == strMimeType:
            strContents = self.__officeReader.ReadLegacyPowerPointToText(strFilePath)
            
        elif FileDefine.MIME_PPTX == strMimeType:
            strContents = self.__officeReader.ReadPPTXToText(strFilePath)
            
        elif FileDefine.MIME_XLS == strMimeType:
            strContents = self.__officeReader.ReadLegacyExcelToText(strFilePath)
            
        elif FileDefine.MIME_XLSX == strMimeType:
            strContents = self.__officeReader.ReadXlsxToText(strFilePath)
        
        else:
            #TODO: 에러를 발생하면 안되고, 공백으로 반환한다.
            # raise Exception (f"unsupported file type {strMimeType}")
            LOG().error(f"unsupported file type {strMimeType}")
            return ""
        
        return strContents
    
    # 파일 - 2차 분석, 차단이 발생했으면, 차단 사유에 대해서도 분석 결과를 수집한다.
    def __analyzeFileBlockDetailReason(self, parameterItem: OfficeFileAnalysisParameterItem, dictEachFileOutput:dict):
        
        '''
        파일에 대해서, pdf를 변환후, 분석된 컨텐츠 위치를 찾는다.
        최초 확인된 정책, 정규식을 다시 적용한다.        
        '''
        
        self.__officeFileAnalyzeHelper.AnalyzeFileBlockDetailReason(parameterItem, dictEachFileOutput)
        
        return ERR_OK
    


