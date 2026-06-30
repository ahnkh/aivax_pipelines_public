
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
    def AnalyzeFileBlockDetailReason(self, strDetailTempDir:str, strOfficeFilePath:str, strMimeType:str, nFileReadTimeout:int, dictRegexPattern:dict, dictEachFileOutput:dict, officeDetectService:OfficeDetectServiceEx):
        
        '''
        정규 표현식, 정책 번호등 상세 정보의 수집이 필요하다.
        약간의 부하가 있더라도, 파일을 한번 더 읽는다. (pdf 변환, libreoffice)
        분석의 파라미터는 ModelItem을 사용해 본다.
        
        분석 결과, dictionary로 정의, 우선 opensearch를 활용한다.
        '''
        
        regexPattern:re.Pattern = dictRegexPattern.get("regex_pattern")
        
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
        officeDetectService.GenerateRegexBaseEvidence(strDetailTempDir, strOfficeFilePath, strMimeType, regexPattern, nFileReadTimeout, lstDetailReason)
                    
        dictEachFileOutput["detail_reason"] = dictDetailReason
        
        return ERR_OK
    
    ############################################### private
    
