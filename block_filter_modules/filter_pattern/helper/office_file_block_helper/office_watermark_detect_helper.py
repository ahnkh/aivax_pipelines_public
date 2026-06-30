
import re

from lib_include import *

from type_hint import *

from service_modules.office_service.office_watermark_service import OfficeWaterMarkService

from service_modules.model_item.service_model_item import (
    OfficeRegexBaseDRMDetectItem,
    OfficeOCRBaseWaterMarkDetectItem
)

from service_modules.office_service.local_etc_common.local_define import *

'''
office 문서 watermark 추출
모듈 wrapping
'''

class OfficeWaterMarkDetectHelper:
    
    def __init__(self):
        
        self.__cachedRegexBaseDRMParameterItem:OfficeRegexBaseDRMDetectItem = None
        self.__cachedOCRBaseWaterMarkParameterItem:OfficeOCRBaseWaterMarkDetectItem = None
        
        self.__officeWaterMarkService:OfficeWaterMarkService = None
        pass
    
    # 초기화 로직, 설정등 필요
    def Initialize(self,):
        '''
        TODO: initialize 시점에 설정 정보를 받아온다. 만일 정책이 변경되면, 객체를 다시 초기화한다. (좀더 고려 필요)
        '''
        
        #TODO: parameter는 기본값으로 초기화, 일단 변경된다는 가정으로, 빈번하지 않다.
        self.__cachedRegexBaseDRMParameterItem:OfficeRegexBaseDRMDetectItem = OfficeRegexBaseDRMDetectItem(

        )
        
        self.__cachedOCRBaseWaterMarkParameterItem:OfficeOCRBaseWaterMarkDetectItem = OfficeOCRBaseWaterMarkDetectItem(
            
        )
        
        self.__officeWaterMarkService:OfficeWaterMarkService = OfficeWaterMarkService()
        self.__officeWaterMarkService.Initialize()
        
        return ERR_OK
    
    # 초기화 와 별도로 설정, 업데이트
    def UpdateWatermarkPolicy(self, dictWatermarkPolicy:dict):
        
        '''
        다시 생성, 정책은 향후 업데이트 기능을 외부 호출측에서 구현
        '''
        
        # 사용여부
        # TODO: 미사용이면, 그대로 skip 한다.
        use_regex_base_drm:bool = dictWatermarkPolicy.get("use_regex_base_drm")
        use_ocr_base_watermark:bool = dictWatermarkPolicy.get("use_ocr_base_watermark")
        
        # drm, regex 패턴
        #TODO: regex 패턴, 변환후 업데이트
        drm_detect_pattern:str = dictWatermarkPolicy.get("drm_detect_pattern")
        drm_pattern_flag:int = dictWatermarkPolicy.get("drm_pattern_flag")
        
        #TODO: 잘못된 패턴에 대한 예외처리, 현시점에서는 안하고, DB등 UI에서 설정할때 변경한다.  
        #TODO: file의 header에 대한 패턴 분석, byte로 컴파일 해야 한다.      
        # drmDetectPattern:re.Pattern = re.compile(drm_detect_pattern, drm_pattern_flag)
        drmDetectPattern:re.Pattern = re.compile(drm_detect_pattern.encode("utf-8"), drm_pattern_flag)
        
        drm_file_header_limit:int = dictWatermarkPolicy.get("drm_file_header_limit")
        
        # ocr, watermark 패턴
        ocr_pattern_list:list = dictWatermarkPolicy.get("ocr_pattern_list")
        ocr_pattern_flag:int = dictWatermarkPolicy.get("ocr_pattern_flag")
        
        ocr_max_hitcount:int = dictWatermarkPolicy.get("ocr_max_hitcount")
        ocr_page_list:list = dictWatermarkPolicy.get("ocr_page_list")
        
        lstOCRPattern:list = []
        
        for strOCRPattern in ocr_pattern_list:
            
            #TODO: local 설정은 예외처리 X, 반드시 확인하고 배포해야 한다.
            ocrPattern:re.Pattern = re.compile(strOCRPattern, ocr_pattern_flag)
            lstOCRPattern.append(ocrPattern)
        
        self.__cachedRegexBaseDRMParameterItem:OfficeRegexBaseDRMDetectItem = OfficeRegexBaseDRMDetectItem(
            use_detect = use_regex_base_drm,
            pattern = drmDetectPattern,
            file_header_limit = drm_file_header_limit
        )
        
        self.__cachedOCRBaseWaterMarkParameterItem:OfficeOCRBaseWaterMarkDetectItem = OfficeOCRBaseWaterMarkDetectItem(
            use_detect = use_ocr_base_watermark,
            pattern_list = lstOCRPattern,
            hit_max_count = ocr_max_hitcount,
            page_list = ocr_page_list
        )
        
        return ERR_OK
    
    
    # 문서 파일, waterMark의 포함 여부
    def DetectOfficeFileWithWaterMark(self, strOfficeFilePath:str, dictEachFileOutput:dict):
        
        '''
        watermark는 여러가지 케이스가 있을수 있으며, 각 케이스별로 추출한다.
        일부 custom 성격, config로 제어가 필요할 수 있다.
        TODO: 사용 여부, 안에서 처리한다.
        TODO: 에러가 발생시, 예외처리, 그대로 True 반환
        '''
        
        # drm을 먼저 탐지, 탐지가 되었으면 탐지로 반환
        
        try:
            
            bBlockDRMWaterMark:bool = self.__officeWaterMarkService.DetectRegexBaseIncludeWaterMarkDRM(strOfficeFilePath, self.__cachedRegexBaseDRMParameterItem)
        
            if True == bBlockDRMWaterMark:
                
                offset:int = self.__cachedRegexBaseDRMParameterItem.offset
                length:int = self.__cachedRegexBaseDRMParameterItem.length
                match_text:str = self.__cachedRegexBaseDRMParameterItem.match_text
                
                strReason:str = f"{FilePolicyDefine.BLOCK_REASON_WATER_MARK_HEADER_DETECT}, offset={offset}/{length}, match={match_text}"
                
                # watermark 차단, 업데이트 (이후 summary가 없다. 분기 및 재가공에 대한 고려)
                dictEachFileOutput[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
                dictEachFileOutput[ApiParameterDefine.POLICY_ID] = "" #정책은 없다.
                dictEachFileOutput[ApiParameterDefine.POLICY_NAME] = FilePolicyDefine.BLOCK_MESSAGE_WATER_MARK_FILE_DETECT
                
                dictEachFileOutput[DBDefine.DB_FIELD_RULE_NAME] = FilePolicyDefine.BLOCK_MESSAGE_WATER_MARK_FILE_DETECT
                dictEachFileOutput[DBDefine.DB_FIELD_RULE_TARGET] = FilePolicyDefine.BLOCK_CATEGORY_WATER_MARK_FILE_DETECT
                dictEachFileOutput[DBDefine.DB_FIELD_RULE_CATEGORY] = FilePolicyDefine.BLOCK_CATEGORY_WATER_MARK_FILE_DETECT
                dictEachFileOutput[DBDefine.DB_FIELD_RULE_SCOPE] = FilterDetectDefine.SCOPE_DEFAULT
                
                
                #TODO: 감사로그
                LOG().error(f"block watermark drm file, reason = {strReason}, category = {FilePolicyDefine.BLOCK_CATEGORY_WATER_MARK_FILE_DETECT}")
                return True
            
            # 이후, watermark를 탐지, 탐지가 되었으면 탐지로 반환
            bBlockOCRWaterMark:bool = self.__officeWaterMarkService.DetectOCRBaseSensitiveWord(strOfficeFilePath, self.__cachedOCRBaseWaterMarkParameterItem)
            
            if True == bBlockOCRWaterMark:
                
                # watermark 차단, 업데이트
                
                page_no:int = self.__cachedOCRBaseWaterMarkParameterItem.hit_page_no
                
                #OCR 문자, 탐지건수
                detect_hit_count:int = self.__cachedOCRBaseWaterMarkParameterItem.detect_hit_count
                hit_max_count:int = self.__cachedOCRBaseWaterMarkParameterItem.hit_max_count
                
                strReason:str = f"{FilePolicyDefine.BLOCK_REASON_WATER_MARK_OCR_TEXT_DETECT}, page={page_no}, hit={hit_max_count} over {detect_hit_count}"
                
                dictEachFileOutput[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
                dictEachFileOutput[ApiParameterDefine.POLICY_ID] = "" 
                dictEachFileOutput[ApiParameterDefine.POLICY_NAME] = FilePolicyDefine.BLOCK_MESSAGE_OCR_SENSITIVE_FILE_DETECT
                
                dictEachFileOutput[DBDefine.DB_FIELD_RULE_NAME] = FilePolicyDefine.BLOCK_MESSAGE_OCR_SENSITIVE_FILE_DETECT                
                dictEachFileOutput[DBDefine.DB_FIELD_RULE_TARGET] = FilePolicyDefine.BLOCK_CATEGORY_WATER_MARK_FILE_DETECT
                dictEachFileOutput[DBDefine.DB_FIELD_RULE_CATEGORY] = FilePolicyDefine.BLOCK_CATEGORY_WATER_MARK_FILE_DETECT
                dictEachFileOutput[DBDefine.DB_FIELD_RULE_SCOPE] = FilterDetectDefine.SCOPE_DEFAULT
                
                LOG().error(f"block watermark ocr file, reason = {strReason}, category = {FilePolicyDefine.BLOCK_CATEGORY_WATER_MARK_FILE_DETECT}")
                return True
            
            # 미탐
            return False
            
        except Exception as err:
            LOG().error(traceback.format_exc())
            return False
        
        