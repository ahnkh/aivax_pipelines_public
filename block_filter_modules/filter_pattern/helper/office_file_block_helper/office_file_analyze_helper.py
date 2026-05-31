
import subprocess
from pathlib import Path
import tempfile

import pdfplumber
import re

from lib_include import *

from type_hint import *

from service_modules.office_service.local_etc_common.local_define import (
    FileDefine
)

from block_filter_modules.filter_pattern.helper.office_file_block_helper.office_detect_service_ex import OfficeDetectServiceEx

'''
office file, 상세 분석
파일의 1차 분석과 별개로, 차단된 파일에 대한 상세 분석 진행
'''

class OfficeFileAnalyzeHelper:
    
    def __init__(self):
        pass
    
    
    def Initialize(self,):
        
        return ERR_OK
    
    
    # file 분석 - 상세 분석 결과, 페이지, 라인번호등의 반환
    def AnalyzeFileBlockDetailReason(self, strFilePath:str, strMimeType:str, nFileReadTimeout:int, dictRegexPattern:dict, dictEachFileOutput:dict, officeDetectService:OfficeDetectServiceEx):
        
        '''
        정규 표현식, 정책 번호등 상세 정보의 수집이 필요하다.
        약간의 부하가 있더라도, 파일을 한번 더 읽는다. (pdf 변환, libreoffice)
        분석의 파라미터는 ModelItem을 사용해 본다.
        
        분석 결과, dictionary로 정의, 우선 opensearch를 활용한다.
        '''
        
        # file mimetype
        # strMimeType:str = parameterItem.mime_type
        
        # dictRegexPattern:dict = parameterItem.regex_pattern
        
        # strFilePath:str = parameterItem.file_path
        
        regexPattern:re.Pattern = dictRegexPattern.get("regex_pattern")
        
        # regexPattern:re.Pattern = dictRegexPattern.get(DBDefine.DB_FIELD_RULE_REGEX_PATTERN)
        
        # id:str = dictRegexPattern.get(DBDefine.DB_FIELD_RULE_ID)
        rule:str = dictRegexPattern.get(DBDefine.DB_FIELD_RULE)
        name:str = dictRegexPattern.get(DBDefine.DB_FIELD_RULE_NAME)
        # action:str = dictRegexPattern.get(DBDefine.DB_FIELD_RULE_ACTION)
        regex_flag:int = dictRegexPattern.get(DBDefine.DB_FIELD_RULE_REGEX_FLAG)
        
        # 전단계, mimetype으로 구분 => pdf로 변환하기에, 우선 pdf 안됨
        # excel, pdf는 건너뛴다.
        
        # 개별 조건으로의 분기, 유효한 mimetype => excel 제외
        # pdf 변환 여부 => pdf는 제외, hwp에 대해서도 확인 필요 
        
        # TODO: 반환값, 기본으로 할당
        '''
        "detail_reason":
        [
            {
                "pattern" : "",
                "page":15,
                "line_no" : 18,
                "y_position" : 630.1,
                "context" : "- pcap 않는다. wireshark mergecap"
            }
        ]
        '''
        
        lstDetailReason:list = []
        
        dictDetailReason:dict = {
            "policy_detail" : { 
                # DBDefine.DB_FIELD_RULE_ID : id,
                DBDefine.DB_FIELD_RULE_NAME : name,
                DBDefine.DB_FIELD_RULE : rule,
                # DBDefine.DB_FIELD_RULE_ACTION : action,
                DBDefine.DB_FIELD_RULE_REGEX_FLAG : regex_flag
            },
            
            "evidence": lstDetailReason
        }

        # 우선 하나 추가.
        dictEachFileOutput["detail_reason"] = dictDetailReason
         
        
        # pdf 변환과정 -> 로직별 분기 필요, 우선 테스트.
        
        # 한글 문서 => pdf 변환도 다시 고려
        
        # 여기는 분기 필요
        officeDetectService.GenerateRegexBaseEvidence(strFilePath, strMimeType, regexPattern, lstDetailReason)
        
        # if strMimeType in (FileDefine.MIME_XLS,
        #     FileDefine.MIME_XLSX,):
            
        #     #xls, skip 한다.
        #     pass
        
        # elif strMimeType in (FileDefine.MIME_PDF):
            
        #     #pdf, 그대로 분석
        #     self.__readPdfAndDetectFileInfo(parameterItem, dictDetailReason)
        #     # pass     
                   
        # elif strMimeType in (FileDefine.MIME_DOC,
        #     FileDefine.MIME_DOCX,
        #     FileDefine.MIME_DOCX_V2,
        #     FileDefine.MIME_HWP,
        #     FileDefine.MIME_DOCX_V2,
        #     FileDefine.MIME_HWPX,
        #     FileDefine.MIME_PPT,
        #     FileDefine.MIME_PPTX
        #     ):
            
        #     self.__extractToPdfAndDetectFileInfo(parameterItem, dictDetailReason)
            
        # else:
        #     #TODO: 감사로그 추가 필요
        #     LOG().error(f"unsupport mime type {strMimeType}")
                    
        dictEachFileOutput["detail_reason"] = dictDetailReason
                                
        # return results            
        # LOG().debug(f"read office {strOfficeFile}, page and line = {results}")
        
        return ERR_OK
    
    ############################################### private
    
    # def __readPdfAndDetectFileInfo(self, parameterItem: OfficeFileAnalysisParameterItem, dictDetailReason: dict):
        
    #     '''
    #     '''
        
    #     # office 파일 경로, pdf 이다.
    #     strOfficeFile:str = parameterItem.file_path
        
    #     officePath:Path = Path(strOfficeFile).resolve()
        
    #     self.__detectFileInfoFromPattern(officePath, parameterItem, dictDetailReason)
        
    #     return ERR_OK
    
    # def __extractToPdfAndDetectFileInfo(self, parameterItem: OfficeFileAnalysisParameterItem, dictDetailReason: dict):
        
    #     '''
    #     # 정규 표현식 외.
    #     # regex_pattern:re.Pattern = dictDBPattern.get("regex_pattern")
    #     # for m in regex_pattern.finditer(strPromptText):
        
    #     # libreoffice를 활용, pdf로 변환한다.
    #     '''
        
    #     #TODO: 예외처리, 상세 분석이 안되면, 탐지만 한다.
    #     try:
            
    #         # office 파일 경로
    #         strOfficeFile:str = parameterItem.file_path
            
    #         # libreoffce, timeout
    #         nReadTimeOut:int = parameterItem.read_timeout

    #         officePath:Path = Path(strOfficeFile).resolve()

    #         #파일 추출, 생성, 임시 파일을 사용한다.
    #         #접두어 aivax_file_detail (이 하드코딩은 skip)
    #         with tempfile.TemporaryDirectory(prefix="aivax_file_detail_") as tmpdir:
                
    #             tmpdir = Path(tmpdir)
                            
    #             # pdf_path = tmpdir / (officePath.stem + ".pdf")
    #             pdfFilePath:Path = tmpdir / (officePath.stem + ".pdf")
                
    #             #TOOD: 예외처리 개선, 검토.
    #             subprocess.run(
    #                 [
    #                     "soffice",
    #                     "--headless",
    #                     "--nologo",
    #                     "--nolockcheck",
    #                     "--nodefault",
    #                     "--nofirststartwizard",
    #                     "--norestore",
    #                     "--convert-to", "pdf",
    #                     "--outdir", str(tmpdir),
    #                     str(officePath),
    #                 ],
    #                 check=True,
    #                 stdout=subprocess.DEVNULL,
    #                 stderr=subprocess.DEVNULL,
    #                 timeout=nReadTimeOut,
    #             )
                
    #             self.__detectFileInfoFromPattern(pdfFilePath, parameterItem, dictDetailReason)
            
    #     except Exception as err:
    #         LOG().error(traceback.format_exc())
        
    #     return ERR_OK
    
    # # 텍스트, 정규표현식 분석 및 이력 저장
    # def __detectFileInfoFromPattern(self, pdfFilePath:Path, parameterItem: OfficeFileAnalysisParameterItem, dictDetailReason: dict):
        
    #     '''
    #     '''
        
    #     dictRegexPattern:dict = parameterItem.regex_pattern
        
    #     regexPattern:re.Pattern = dictRegexPattern.get("regex_pattern")
    #     # strRule:str = dictRegexPattern.get("rule")
        
    #     lstDetailReason:list = dictDetailReason.get("evidence")
        
    #     with pdfplumber.open(pdfFilePath) as pdf:
            
    #         # 탐지 번호    
    #         nDetectNo:int = 0
            
    #         for nPageIndex, page in enumerate(pdf.pages, start=1):
    #             words = page.extract_words(use_text_flow=True)

    #             # 줄 단위 그룹핑 (Y 좌표 기준)
    #             lines = {}
    #             for w in words:
    #                 y = round(w["top"], 1)
    #                 lines.setdefault(y, []).append(w["text"])

    #             #TODO: 필요한지 확인.
    #             sorted_lines = sorted(lines.items())

    #             for line_no, (y, texts) in enumerate(sorted_lines, start=1):
    #                 line_text = " ".join(texts)
                    
    #                 #TODO: 정규표현식 부분, 다시 개선 필요.
    #                 for m in regexPattern.finditer(line_text):
                        
    #                     # 탐지된 컨텐츠는 모두 추가.
    #                     nDetectNo += 1
    #                     lstDetailReason.append({
    #                         "no" : nDetectNo,
    #                         # "pattern": strRule,
    #                         "page_no": nPageIndex,
    #                         "line_no": line_no,
    #                         # "y_position": y, #TODO: 필요한지.
    #                         "context": line_text.strip()
    #                     })        
    #     return ERR_OK
        
    
    