
import subprocess
from pathlib import Path
import tempfile

import pdfplumber
import re

from lib_include import *

from type_hint import *

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
    def AnalyzeFileBlockDetailReason(self, parameterItem: OfficeFileAnalysisParameterItem, dictEachFileOutput:dict):
        
        '''
        정규 표현식, 정책 번호등 상세 정보의 수집이 필요하다.
        약간의 부하가 있더라도, 파일을 한번 더 읽는다. (pdf 변환, libreoffice)
        분석의 파라미터는 ModelItem을 사용해 본다.
        
        분석 결과, dictionary로 정의, 우선 opensearch를 활용한다.
        '''
        
        # file mimetype
        strMimeType:str = parameterItem.mime_type
        
        
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
        
        lstDetailReason:list = [            
        ]
        
        # 우선 하나 추가.
        dictEachFileOutput["detail_reason"] = lstDetailReason
        
        bValidMimeType:bool = self.__checkAvailableMimeType(strMimeType)
        
        if False == bValidMimeType:
            
            return ERR_OK
        
        # pdf 변환과정 -> 로직별 분기 필요, 우선 테스트.
        
        # 한글 문서 => pdf 변환도 다시 고려
        
        #TODO: refactoring
        
        self.__extractToPdfAndDetectFileInfo(parameterItem, lstDetailReason)
            
        # 한번더 업데이트
        dictEachFileOutput["detail_reason"] = lstDetailReason
                                
        # return results            
        # LOG().debug(f"read office {strOfficeFile}, page and line = {results}")
        
        return ERR_OK
    
    ############################################### private
    
    # 유효한 mime type, 체크
    def __checkAvailableMimeType(self, strMimeType:str) -> bool:
        
        '''
        우선 Excel은 1제외한다. (향후 정책, 설정으로.)
        '''
        
        if strMimeType in (
            FileDefine.MIME_XLS,
            FileDefine.MIME_XLSX,
        ):
            return False
        
        return True
    
    def __extractToPdfAndDetectFileInfo(self, parameterItem: OfficeFileAnalysisParameterItem, lstDetailReason: list):
        
        '''
        # 정규 표현식 외.
        # regex_pattern:re.Pattern = dictDBPattern.get("regex_pattern")
        # for m in regex_pattern.finditer(strPromptText):
        
        # libreoffice를 활용, pdf로 변환한다.
        '''
        
        # office 파일 경로
        strOfficeFile:str = parameterItem.file_path
        
        # libreoffce, timeout
        nReadTimeOut:int = parameterItem.read_timeout
        
        dictRegexPattern:dict = parameterItem.regex_pattern
        
        regex_pattern:re.Pattern = dictRegexPattern.get("regex_pattern")
        rule:str = dictRegexPattern.get("rule")

        officePath:Path = Path(strOfficeFile).resolve()

        #파일 추출, 생성, 임시 파일을 사용한다.
        #접두어 aivax_file_detail (이 하드코딩은 skip)
        with tempfile.TemporaryDirectory(prefix="aivax_file_detail_") as tmpdir:
            
            tmpdir = Path(tmpdir)
                        
            # pdf_path = tmpdir / (officePath.stem + ".pdf")
            pdfFilePath:Path = tmpdir / (officePath.stem + ".pdf")
            
            #TOOD: 예외처리 개선, 검토.
            subprocess.run(
                [
                    "soffice",
                    "--headless",
                    "--nologo",
                    "--nolockcheck",
                    "--nodefault",
                    "--nofirststartwizard",
                    "--norestore",
                    "--convert-to", "pdf",
                    "--outdir", str(tmpdir),
                    str(officePath),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=nReadTimeOut,
            )
            
            self.__detectFileInfoFromPattern(pdfFilePath, regex_pattern, rule, lstDetailReason)
        
        return ERR_OK
    
    # 텍스트, 정규표현식 분석 및 이력 저장
    def __detectFileInfoFromPattern(self, pdfFilePath:Path, regexPattern:re.Pattern, strRule:str, lstDetailReason: list):
        
        '''
        '''
        
        with pdfplumber.open(pdfFilePath) as pdf:
            
            # 탐지 번호    
            nDetectNo:int = 0
            
            for nPageIndex, page in enumerate(pdf.pages, start=1):
                words = page.extract_words(use_text_flow=True)

                # 줄 단위 그룹핑 (Y 좌표 기준)
                lines = {}
                for w in words:
                    y = round(w["top"], 1)
                    lines.setdefault(y, []).append(w["text"])

                #TODO: 필요한지 확인.
                sorted_lines = sorted(lines.items())

                for line_no, (y, texts) in enumerate(sorted_lines, start=1):
                    line_text = " ".join(texts)
                    
                    #TODO: 정규표현식 부분, 다시 개선 필요.
                    for m in regexPattern.finditer(line_text):
                        
                        # 탐지된 컨텐츠는 모두 추가.
                        nDetectNo += 1
                        lstDetailReason.append({
                            "detect" : nDetectNo,
                            "pattern": strRule,
                            "page_no": nPageIndex,
                            "line_no": line_no,
                            "y_position": y, #TODO: 필요한지.
                            "context": line_text.strip()
                        })        
        return ERR_OK
        
    
    