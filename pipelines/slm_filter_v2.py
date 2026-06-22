
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
        
        # metadata:dict = body.get(ApiParameterDefine.META_DATA)
        
        # dictUser:dict = __user__
        
        messages = body.get(ApiParameterDefine.MESSAGES)
        strLocalContents:str = self.__gatherContents(messages)
        
        dictSLMPolicyResult:dict = {
            
            # DBDefine.DB_FIELD_RULE_ID : "",
            # DBDefine.DB_FIELD_RULE_NAME : "",
            DBDefine.DB_FIELD_RULE_ACTION : "",
            # DBDefine.DB_FIELD_RULE_TARGET : "",
            # DBDefine.DB_FIELD_RULE_CATEGORY : "", 
            
            SLMDetectDefine.SLM_EVIDENCE : []
        }
        
        slmFilterPattern:SLMFilterPattern = self.GetFilterPatternModule(FilterPatternManager.PATTERN_FILTER_SLM)
        
        #TODO: 향후 예외처리
        slmFilterPattern.DetectPattern(strLocalContents, dictOuputResponse, dictSLMPolicyResult)
        
        strSLMAction:str = dictSLMPolicyResult.get(ApiParameterDefine.OUT_ACTION)
        
        # strPolicyName:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_NAME, "")
        lstEvidence:list = dictSLMPolicyResult.get(SLMDetectDefine.SLM_EVIDENCE)
        
        self.__updateApiOutResponse(strSLMAction, lstEvidence, dictOuputResponse)
        
        # self.__addLogData(dictOuputResponse, dictSLMPolicyResult, metadata, dictUser, strLocalContents, dictLogBuffer)
        self.__addLogData(dictOuputResponse, dictSLMPolicyResult, dictLogBuffer)
        
        return ERR_OK
    
    
    async def testRule(self, strPrompt:str, strAction:str, dictTestOutResponse:dict):
        
        '''
        '''
        
        dictOuputResponse:dict = {}
        
        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
        dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_ALLOW
        
        dictSLMPolicyResult:dict = {
            
            # DBDefine.DB_FIELD_RULE_ID : "",
            # DBDefine.DB_FIELD_RULE_NAME : "",
            DBDefine.DB_FIELD_RULE_ACTION : "",
            # DBDefine.DB_FIELD_RULE_TARGET : "",
            # DBDefine.DB_FIELD_RULE_CATEGORY : "", 
            
            SLMDetectDefine.SLM_EVIDENCE : []
        }
        
        slmFilterPattern:SLMFilterPattern = self.GetFilterPatternModule(FilterPatternManager.PATTERN_FILTER_SLM)
        
        slmFilterPattern.DetectPattern(strPrompt, dictOuputResponse, dictSLMPolicyResult)
        
        # 차단결과 => 정책에 등록된 SLM, 논리가 비약하다..
        # strSLMAction:str = dictSLMPolicyResult.get(ApiParameterDefine.OUT_ACTION)
        
        #TODO 다중으로 출력, Wins모델로 결정되어, 탐지된 컨텐츠를 여러개 표시한다.
        # strPolicyName:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_NAME, "") => 불필요, 제거        
        lstEvidence:list = dictSLMPolicyResult.get(SLMDetectDefine.SLM_EVIDENCE)
        
        #TODO: 26.06.22 evidence 추가
        #TODO: 같이 사용하지 못한다. 중복이지만 별도로 작성한다.
        
        # self.__updateApiOutResponse(strAction, lstEvidence, dictOuputResponse)
        
        if PipelineFilterDefine.ACTION_BLOCK == strAction:
            
            dictTestOutResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
            
            strCustomContent:str = '''
허용되지 않은 프롬프트 문맥이 SLM에 의해 탐지되어 요청이 차단되었습니다. 
'''
                           
            dictTestOutResponse[ApiParameterDefine.OUT_CONTENT] = strCustomContent
            
            dictTestOutResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = self.__filterCustomUtil.CustomSLMBlockMessage(lstEvidence)
            
        elif PipelineFilterDefine.ACTION_MASKING == strAction:
            
            #TODO: masking은 없다. 동일 메시지로..
            
            strCustomContent:str = '''
허용되지 않은 프롬프트 문맥이 SLM에 의해 탐지되어 요청이 마스킹 되었습니다. 
'''
            
            dictTestOutResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_MASKING
            
            dictTestOutResponse[ApiParameterDefine.OUT_MASKED_CONTENTS] = self.__filterCustomUtil.CustomMaskMessageOfSLM(lstEvidence)
        
        else:
            
            dictTestOutResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_UNDETECTED
            # dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_ALLOW
        
        #TODO:저장외 동일하다... => 공통화 필요
        
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
    
    # def __addLogData(self, dictOuputResponse:dict, dictSLMPolicyResult:dict, dictMetaData:dict, dictUser:dict, strContents:str, dictLogBuffer:dict):
    def __addLogData(self, dictOuputResponse:dict, dictSLMPolicyResult:dict, dictLogBuffer:dict):
        
        '''
        '''
        
        strSLMContent:str = dictOuputResponse.get(ApiParameterDefine.OUT_SLM_CONTENT)
        
        # strPolicyID:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_ID, "")
        # strPolicyName:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_NAME, "")
        
        strPolicyAction:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_ACTION, "")
        # strPolicyTarget:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_TARGET, "")
        # strPolicyCategory:str = dictSLMPolicyResult.get(DBDefine.DB_FIELD_RULE_CATEGORY, "")
        
        dictLogBuffer[DBDefine.DB_FIELID_FILTER_DETECT].append({
            "filter" : PipelineFilterDefine.FILTER_STAGE_SLM,
            "mode": strPolicyAction, 
            # "policy_id" : strPolicyID,
            # "policy_name" : strPolicyName,
            # "target": strPolicyTarget,
            # "category": strPolicyCategory,
            "slm_content" : strSLMContent
        })
        
        return ERR_OK
    
    # API 응답 결과 업데이트
    def __updateApiOutResponse(self, strSLMAction, lstEvidence:list, dictOuputResponse:dict):
        
        '''
        '''
                
        if PipelineFilterDefine.ACTION_BLOCK == strSLMAction:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
            dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_BLOCK
            
            dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = self.__filterCustomUtil.CustomSLMBlockMessage(lstEvidence)
            
        elif PipelineFilterDefine.ACTION_MASKING == strSLMAction:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_MASKING
            dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_MASKING
            
            dictOuputResponse[ApiParameterDefine.OUT_MASKED_CONTENTS] = self.__filterCustomUtil.CustomMaskMessageOfSLM(lstEvidence)
        
        else:
            
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
            dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_ALLOW
        
        return ERR_OK
    