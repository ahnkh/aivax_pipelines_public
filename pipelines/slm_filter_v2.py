
import copy

from lib_include import *

from type_hint import *

from block_filter_modules.filter_pattern.filter_pattern_manager import FilterPatternManager
from block_filter_modules.filter_pattern.helper.slm_filter_pattern import SLMFilterPattern

from block_filter_modules.etc_utils.filter_custom_utils import FilterCustomUtils

'''
'''

class Pipeline(PipelineBase):
    
    def __init__(self):
        
        '''
        '''
        
        super().__init__()
        
        self.type = "filter"
        self.id = "slm_filter"
        self.name = "slm_filter"
        
        # 공용 helper
        self.__filterCustomUtil:FilterCustomUtils = FilterCustomUtils()        
        pass
    
    async def inlet(self, body: Dict[str, Any], __user__: Optional[dict] = None, customFilterConfigItem : PipelineCustomFilterConfigItem = None, dictLogBuffer:dict = None, dictOuputResponse:dict = None, __request__: Optional[Request] = None) : #-> Dict[str, Any]:
        '''
        '''

        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
        dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_ALLOW
        
        
        metadata:dict = body.get(ApiParameterDefine.META_DATA)
        
        dictUser:dict = __user__
        
        messages = body.get(ApiParameterDefine.MESSAGES)
        strLocalContents:str = self.__gatherContents(messages)
        
        
        dictSLMPolicyResult:dict = {
            
            DBDefine.DB_FIELD_RULE_ID : "",
            DBDefine.DB_FIELD_RULE_NAME : "",
            DBDefine.DB_FIELD_RULE_ACTION : "",
            DBDefine.DB_FIELD_RULE_TARGET : "",
            DBDefine.DB_FIELD_RULE_CATEGORY : "", 
        }
        
        slmFilterPattern:SLMFilterPattern = self.GetFilterPatternModule(FilterPatternManager.PATTERN_FILTER_SLM)
        
        slmFilterPattern.DetectPattern(strLocalContents, dictOuputResponse, dictSLMPolicyResult)
        
        strSLMAction:str = dictSLMPolicyResult.get(ApiParameterDefine.OUT_ACTION)
        
        strPolicyName:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_NAME, "")
        
        self.__updateApiOutResponse(strSLMAction, strPolicyName, dictOuputResponse)
        
        self.__addLogData(dictOuputResponse, dictSLMPolicyResult, metadata, dictUser, strLocalContents, dictLogBuffer)
        
        return ERR_OK
        
    def __gatherContents(self, lstMessage:str) -> str:
        
        '''
        '''
        
        if None == lstMessage or 0 == len(lstMessage):
            return ""
        
        last:dict = lstMessage[-1]
        
        if None != last:
            
            content = last.get("content", "")
        
            strLocalContents:str = copy.deepcopy(content)
            return strLocalContents
        
        return ""
    
    def __addLogData(self, dictOuputResponse:dict, dictSLMPolicyResult:dict, dictMetaData:dict, dictUser:dict, strContents:str, dictLogBuffer:dict):
        
        '''
        '''
        
        strSLMContent:str = dictOuputResponse.get(ApiParameterDefine.OUT_SLM_CONTENT)
        
        strPolicyID:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_ID, "")
        strPolicyName:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_NAME, "")
        
        strPolicyAction:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_ACTION, "")
        strPolicyTarget:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_TARGET, "")
        strPolicyCategory:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_CATEGORY, "")
        
        dictLogBuffer[DBDefine.DB_FIELID_FILTER_DETECT].append({
            "filter" : PipelineFilterDefine.FILTER_STAGE_SLM,
            "mode": strPolicyAction, 
            "policy_id" : strPolicyID,
            "policy_name" : strPolicyName,
            "target": strPolicyTarget,
            "category": strPolicyCategory,
            "slm_content" : strSLMContent
        })
        
        return ERR_OK
    
    # API 응답 결과 업데이트
    def __updateApiOutResponse(self, strSLMAction, strPolicyName, dictOuputResponse:dict):
        
        '''
        '''
                
        if PipelineFilterDefine.ACTION_BLOCK == strSLMAction:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
            dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_BLOCK
            
            dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = self.__filterCustomUtil.CustomBlockMessages(strPolicyName)
            
        elif PipelineFilterDefine.ACTION_MASKING == strSLMAction:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_MASKING
            dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_MASKING
            
            dictOuputResponse[ApiParameterDefine.OUT_MASKED_CONTENTS] = self.__filterCustomUtil.CustomMaskMessageOfSLM(strPolicyName)
        
        else:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
            dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_ALLOW
        
        return ERR_OK
    