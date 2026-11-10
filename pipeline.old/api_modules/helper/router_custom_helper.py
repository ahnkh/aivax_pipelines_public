
import base64

from lib_include import *

from type_hint import *

'''
router 관련 customize helper
TODO: 매번 호출해야 하는 문제, mainapp에 넣기도 애매
이건 static 처리
'''

class RouterCustomHelper:
    
    def __init__(self):
        
        pass
    
    # #filter 메시지, 프롬프트 convert    
    # def ConvertPromptMessage(self, modelItem: VariantFilterForm) -> str:
        
    #     '''
    #     프롬프트 관리, 입력값을 변환하여, pipeline filter에서 사용가능한 프롬프트로 변환한다.
    #     향후 입력값의 사양이 변경되어도, 프롬프트 메시지는 여기서 처리한다.
    #     user, contents 구조등, 사양이 변경되어도 여기서 처리.
    #     인코딩 옵션 추가, base64 인코딩 처리
        
    #     우선 multiple_filter 만 대상
    #     '''
        
    #     strPromptMessage:str = modelItem.prompt
        
    #     # 아예 제거
    #     # bEncoding:bool = modelItem.encoding
    #     # if True == bEncoding:
            
    #     #     #base64 인코딩 처리한다. 인코딩 오류가 발생하면, 예외 발생 (api에서 처리)
            
    #     #     bytebase64DecodePlainPrompt:bytes = base64.b64decode(strPromptMessage)
            
    #     #     strPromptMessage = bytebase64DecodePlainPrompt.decode('utf-8')
            
    #     #     LOG().debug(f"decode prompt, plain message = {strPromptMessage}")
        
    #     return strPromptMessage
    
    #inlet으로 filter함수를 통일하고, body 요청 메시지를 생성한다. 
    def GenerateInletBodyParameter(self, modelItem: VariantFilterForm) -> dict:
        '''
        #다음의 구조이다.
        "body":
        {
            "metadata": {
                "session_id": "",
                "message_id": ""
            },
            
            "messages": [
                {"role":"user", "content":""}
            ]
        }
        
        프롬프트를 추출 (encoding 포함)하여 body를 생성해서 반환한다.
        예외적으로 dictionary를 반환하며, 각 inlet마다 공통으로 전달한다.
        role 하드코딩은 나중에 수정
        '''
        
        # strPromptMessage:str = self.ConvertPromptMessage(modelItem)
                
        # #TODO: file 분석시 다시 확인, prompt가 없을 수 있다. => modelItem에서 예외처리 된다. 없을경우 공백 상태로 그대로 동작한다.
        # if None == strPromptMessage or 0 == len(strPromptMessage):
        #     strErrorMessage:str = f"invalid prompt, no data"            
        #     self.GenerateHttpException(ApiErrorDefine.HTTP_500_INTERNAL_SERVER_ERROR, ApiErrorDefine.HTTP_500_INTERNAL_SERVER_ERROR_MSG, strErrorMessage, None)            
        #     return None
        
        #TODO: file 정보 list를 list<dict>로 변환한다. model_dump는 호환성 문제로 가급적 사용 안한다.
        lstAttachFile:list = []
        self.__generateAttachFile(lstAttachFile, modelItem)
                
        dictBody = {
            
            ApiParameterDefine.META_DATA : {
                ApiParameterDefine.SESSION_ID : modelItem.session_id,
                ApiParameterDefine.MESSAGE_ID : modelItem.message_id #TODO: 없는 필드, sessionid를 같이 추가한다.
            },
            
            #TODO: 이 구조를 변경하면, 약간의 성능 개선 기대.
            ApiParameterDefine.MESSAGES: [
                {"role":"user", ApiParameterDefine.MESSAGE_PROMPT : modelItem.prompt}
            ],
            
            #file 정보, 별도로 추가, 여러개일수 있다. modelitem에서 전달되는 file명을 전달한다.
            # ApiParameterDefine.ATTACH_FILE : modelItem.attach_files
            ApiParameterDefine.ATTACH_FILE : lstAttachFile
        }
        
        return dictBody
    
    #output parameter, 거의 동일하다.
    def GenerateOutletBodyParameter(self, modelItem: OutputFilterItem) -> dict:
        '''
        TOOD: inline과 일부 코드 중복, 향후 리펙토링 (또는 유지)
        '''
        
        strOutput:str = modelItem.llm_output
        
        dictBody = {
            ApiParameterDefine.MESSAGES: [
                {"role":"user", "content":strOutput}
            ],
            
            ApiParameterDefine.META_DATA : {
                ApiParameterDefine.SESSION_ID : modelItem.session_id,
                ApiParameterDefine.MESSAGE_ID : modelItem.message_id #TODO: 없는 필드, sessionid를 같이 추가한다.
            },
            
        }
        
        return dictBody
    
        
    #응답 데이터 가공, 판정 기능의 개발, 일단 하나의 모듈에서 개발, 향후 분리한다.\    
    def GenerateOutputFinalDecision(self, dictFinalResult:dict, dictEachFilterOutput:dict, nSSLProxyBypassBitMask:int):
        
        '''
        개별 output을 순회한다.
        action을 확인한다. block이 발견되면 가장 먼저 발견된 block으로 masking 한다.
        block이 없으면 mask 된 filter를 확인한다. => filter가 많지 않을것 같다. 
        block을 먼저 찾고, block이 없으면 가장 첫번째 mask된 filter에서 가져온다.
        masking, block 모두 없으면, 공백의 allow를 반환한다.
        
        ApiParameterDefine.OUT_ACTION
        ApiParameterDefine.OUT_MASKED_CONTENTS
        ApiParameterDefine.OUT_BLOCK_MESSAGE
        
        TODO: block, mask 각각 처음것을 찾는쪽이 더 효율적으로 보인다. 
        HitData 개념으로, 각각 하나씩 가지고, block을 우선순위를 높게 설정하여 저장한다.
        
        26.03.23 사양변경, policy/id,name, category 필드도 추가
        '''
        
        #최초 수집 데이터 저장용 History buffer
        # dictBlockHitHistory = {} => block은 걸리면 바로 전달, 종료
        dictMaskHitHistory = {}
        
        for strFilterKey in dictEachFilterOutput.keys():
            
            #TODO: 2 depth
            '''
            "input_filter": {
                "action": "allow"
            },
            "secret_filter": {
                "action": "masking",
                "description": "secret_filter filter 차단을 수행합니다.",
                "content": "내 API key는 [AIVAX MASKING] 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요",
                "block_message": "[AIVAX] 프롬프트 차단\nAIVAX 정책에 의해 민감정보가 프롬프트에 포함된 것으로 탐지되었습니다.\n❌탐지 유형은 'API 키의 탐지' 입니다.\n민감 정보를 전송할 경우, 기밀 정보 또는 개인 정보 유출등의 피해가 발생할 수 있으니 각별한 주의를 부탁드려요\n요청하신 프롬프트는 AIVAX에 의해서 요청이 차단되었습니다.\n세부 지침 사항은 관리자에게 문의해주세요\n(김상표,김윤정,김인호,서유진,안규현,이주용 드림)\n        "
            }
            '''
            
            dictFilterOutput:dict = dictEachFilterOutput.get(strFilterKey)
            
            #TODO: 여기는 예외처리 보강 필요
            
            #action code, 기본값 allow
            nActionCode:int = dictFilterOutput.get(ApiParameterDefine.OUT_ACTION_CODE, PipelineFilterDefine.CODE_ALLOW)
            
            #Block, 찾으면 바로 업데이트 후 Skip
            if PipelineFilterDefine.CODE_BLOCK == nActionCode:
                
                if nSSLProxyBypassBitMask & FilterDefine.SSL_PROXY_BYPASS_BLOCK:
                    
                    # 일단 LOG
                    LOG().info(f"skip block by proxy bypass bitmask, mask = {nSSLProxyBypassBitMask}")
                    break
                
                self.__updateOutputContents(dictFinalResult, dictFilterOutput)
                return ERR_OK
            
            #masking, 최초 masking 만 저장한다.
            #TODO: 한화 시스템 요구사항 - masking이 되면, 네트워크 트래픽은 유지하고, aivax에만 기록을 남기도록 처리
            # 옵션화 필요 => 옵션 정보를 별도로 받는다. 현재 db등 정책이 없어서 local 설정으로 받아온다.
            elif PipelineFilterDefine.CODE_MASKING == nActionCode:
                
                #ssl proxy bypass 모드 추가 
                # bypass 옵션에 block, masking => code가 들어있으면 bypass
                # 성능 보다는 가독성 중심으로 수정
                # 0b0001 block
                # 0b0002 masking
                # 0b0100 allow
                if nSSLProxyBypassBitMask & FilterDefine.SSL_PROXY_BYPASS_MASKING:
                    
                    # 일단 LOG
                    LOG().info(f"skip masking by proxy bypass bitmask, mask = {nSSLProxyBypassBitMask}")
                    break
                
                if 0 == len(dictMaskHitHistory):
                    
                    LOG().debug("first hit masked contents")
                    dictMaskHitHistory.update(dictFilterOutput)
        
        #여기서 masking이 있으면 업데이트 한다.
        if 0 < len(dictMaskHitHistory):
            
            # LOG().debug("update masked contents")
            self.__updateOutputContents(dictFinalResult, dictMaskHitHistory)
        
        
        return ERR_OK
    
    # filter에서 최종 mode값을 추출한다.
    def GenerateFilterFinalMode(self, dictLogOutput:dict) -> str:
        
        '''
        filter_detect 리스트를 순회, mode값을 검색한다.
        block -> masking -> allow -> "" 순으로 반환
        각 mode에 대한 집계형태로 추출이 필요하기는 하다.
        '''
        
        dictModeSummary = {            
        }
        
        #항상 존재하는 값
        lstFilterDetect:list = dictLogOutput.get(DBDefine.DB_FIELID_FILTER_DETECT)
        
        for dictFilter in lstFilterDetect:
            
            mode:str = dictFilter.get(DBDefine.DB_FIELID_MODE)
            
            dictModeSummary[mode] = mode
            
        #반환값 우선순위별로 반환
        
        if None != dictModeSummary.get(PipelineFilterDefine.ACTION_BLOCK):
            return PipelineFilterDefine.ACTION_BLOCK
        elif None != dictModeSummary.get(PipelineFilterDefine.ACTION_MASKING):
            return PipelineFilterDefine.ACTION_MASKING
        elif None != dictModeSummary.get(PipelineFilterDefine.ACTION_ACCEPT):
            return PipelineFilterDefine.ACTION_ACCEPT
        elif None != dictModeSummary.get(PipelineFilterDefine.ACTION_UNDETECTED):
            return PipelineFilterDefine.ACTION_UNDETECTED
        else:
            return PipelineFilterDefine.ACTION_BLANK
        
        # return ERR_OK
        
    #최종 filterdetect, 여기는 가공하지 말고 원본을 그대로 반환한다.
    def GenerateFinalFilterDetect(self, dictLogOutput:dict) -> dict:
        
        '''
        logoutput에서 filter_detect를 찾는다. 
        block이면 바로 반환
        block이 아니면, 다시 map으로 전환
        mode, policy id,name, category 값이 들어있는 dictionary를 반환한다.
        '''
        
        lstFilterDetect:list = dictLogOutput.get(DBDefine.DB_FIELID_FILTER_DETECT)
        
        dictModeSortedFilter = {}
        
        for dictFilter in lstFilterDetect:
            
            mode:str = dictFilter.get(DBDefine.DB_FIELID_MODE)
            
            if PipelineFilterDefine.ACTION_BLOCK == mode:
                
                return dictFilter
            
            #나머지는 다시 변환.
            
            dictModeSortedFilter[mode] = dictFilter
            # pass
            
        #일단, 그냥 구현
        dictMaskFilter:dict = dictModeSortedFilter.get(PipelineFilterDefine.ACTION_MASKING)
        
        if None != dictMaskFilter:
            return dictMaskFilter
        
        dictAcceptFilter:dict = dictModeSortedFilter.get(PipelineFilterDefine.ACTION_ACCEPT)
        
        if None != dictAcceptFilter:
            return dictAcceptFilter
        
        dictUndetectFilter:dict = dictModeSortedFilter.get(PipelineFilterDefine.ACTION_UNDETECTED)
        
        if None != dictUndetectFilter:
            return dictUndetectFilter
        
        dictBlankFilter:dict = dictModeSortedFilter.get(PipelineFilterDefine.ACTION_BLANK)
        
        if None != dictBlankFilter:
            return dictBlankFilter
        
        #TODO: 여기까지 왔으면, exception이다.
        return {}
    
    #오류 발생시 대응 공통화    
    def GenerateHttpException(self, nErrorCode:int, strMsgCode:str, strErrorMessage:str, apiResponseHandler:ApiResponseHandlerEX = None):
        
        '''
        '''
        
        if None == apiResponseHandler:
            apiResponseHandler = ApiResponseHandlerEX()
                
        apiResponseHandler.attachFailCode(nErrorCode, strMsgCode, strErrorMessage)

        dictOutput = apiResponseHandler.outResponse()
        
        raise HTTPException(status_code = nErrorCode, detail = dictOutput) 
        # pass
    
    ############################################################# private
    
    # file 정보, list로 생성
    def __generateAttachFile(self, lstAttachFile:list, modelItem: VariantFilterForm):
        
        '''
        TODO: request로 넘어온 정보만, 파일 경로등 부가정보는 사용하는 곳에서 가공
        FileAttachItem
        '''
        
        # lstFiltAttachItem:List[FileAttachItem] = modelItem.attachments
        
        for fileAttachItem in modelItem.attachments:
            
            id:str = fileAttachItem.id
            size:int = fileAttachItem.size
            name:str = fileAttachItem.name
            mime_type:str = fileAttachItem.mime_type
            
            dictFileItem:dict = {
                "id" : id,
                "size" : size,
                "name" : name,
                "mime_type" : mime_type
            }
            
            lstAttachFile.append(dictFileItem)
            # pass
        
        return ERR_OK
    
    #Output 결과 업데이트, 모듈 재활용    
    def __updateOutputContents(self, dictFinalResult:dict, dictFilterOutput:dict):
        
        '''
        '''
        
        nActionCode:int = dictFilterOutput.get(ApiParameterDefine.OUT_ACTION_CODE, PipelineFilterDefine.CODE_ALLOW)
        strAction:str = dictFilterOutput.get(ApiParameterDefine.OUT_ACTION, PipelineFilterDefine.ACTION_ALLOW)
        
        strMaskedContents:str = dictFilterOutput.get(ApiParameterDefine.OUT_MASKED_CONTENTS, "")
            
        # #block message
        strBlockContents:str = dictFilterOutput.get(ApiParameterDefine.OUT_BLOCK_MESSAGE, "")
        
        dictFinalResult[ApiParameterDefine.OUT_ACTION_CODE] = nActionCode
        dictFinalResult[ApiParameterDefine.OUT_ACTION] = strAction
        
        dictFinalResult[ApiParameterDefine.OUT_BLOCK_MESSAGE] = strBlockContents
        dictFinalResult[ApiParameterDefine.OUT_MASKED_CONTENTS] = strMaskedContents
        
        return ERR_OK
        
        
        