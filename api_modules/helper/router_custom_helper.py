
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
    
    #filter 메시지, 프롬프트 convert    
    def ConvertPromptMessage(self, modelItem: VariantFilterForm) -> str:
        
        '''
        프롬프트 관리, 입력값을 변환하여, pipeline filter에서 사용가능한 프롬프트로 변환한다.
        향후 입력값의 사양이 변경되어도, 프롬프트 메시지는 여기서 처리한다.
        user, contents 구조등, 사양이 변경되어도 여기서 처리.
        인코딩 옵션 추가, base64 인코딩 처리
        
        우선 multiple_filter 만 대상
        '''
        
        bEncoding:bool = modelItem.encoding
        
        strPromptMessage:str = modelItem.prompt
        
        if True == bEncoding:
            
            #base64 인코딩 처리한다. 인코딩 오류가 발생하면, 예외 발생 (api에서 처리)
            
            bytebase64DecodePlainPrompt:bytes = base64.b64decode(strPromptMessage)
            
            strPromptMessage = bytebase64DecodePlainPrompt.decode('utf-8')
            
            LOG().debug(f"decode prompt, plain message = {strPromptMessage}")
        
        return strPromptMessage
    
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
        
        strPromptMessage:str = self.ConvertPromptMessage(modelItem)
        
        #TODO: file 분석시 다시 확인, prompt가 없을 수 있다.
        if None == strPromptMessage or 0 == len(strPromptMessage):
            strErrorMessage:str = f"invalid prompt, no data"            
            self.GenerateHttpException(ApiErrorDefine.HTTP_500_INTERNAL_SERVER_ERROR, ApiErrorDefine.HTTP_500_INTERNAL_SERVER_ERROR_MSG, strErrorMessage, apiResponseHandler)            
            return None
                
        dictBody = {
            
            ApiParameterDefine.META_DATA : {
                ApiParameterDefine.SESSION_ID : modelItem.session_id,
                ApiParameterDefine.MESSAGE_ID : modelItem.message_id #TODO: 없는 필드, sessionid를 같이 추가한다.
            },
            
            ApiParameterDefine.MESSAGES: [
                {"role":"user", "content":strPromptMessage}
            ],
            
            #file 정보, 별도로 추가, 여러개일수 있다. modelitem에서 전달되는 file명을 전달한다.
            ApiParameterDefine.ATTACH_FILE : modelItem.attach_files
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
    def GenerateOutputFinalDecision(self, dictFinalResult:dict, dictEachFilterOutput:dict):
        
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
                
                # # #masked contents
                # #TODO: 중복, 함수화
                # strMaskedContents:str = dictFilterOutput.get(ApiParameterDefine.OUT_MASKED_CONTENTS, "")
            
                # # #block message
                # strBlockContents:str = dictFilterOutput.get(ApiParameterDefine.OUT_BLOCK_MESSAGE, "")
                
                # dictFinalResult[ApiParameterDefine.OUT_BLOCK_MESSAGE] = strBlockContents
                # dictFinalResult[ApiParameterDefine.OUT_MASKED_CONTENTS] = strMaskedContents
                
                self.__updateOutputContents(dictFinalResult, dictFilterOutput)
                return ERR_OK
            
            #masking, 최초 masking 만 저장한다.
            elif PipelineFilterDefine.CODE_MASKING == nActionCode:
                
                if 0 == len(dictMaskHitHistory):
                    
                    LOG().debug("first hit masked contents")
                    dictMaskHitHistory.update(dictFilterOutput)
        
        #여기서 masking이 있으면 업데이트 한다.
        if 0 < len(dictMaskHitHistory):
            
            LOG().debug("update masked contents")
            self.__updateOutputContents(dictFinalResult, dictMaskHitHistory)
        
        return ERR_OK
    
    #오류 발생시 대응 공통화    
    def GenerateHttpException(self, nErrorCode:int, strMsgCode:str, strErrorMessage:str, apiResponseHandler:ApiResponseHandlerEX = None):
        
        '''
        '''
        
        #TODO: 생성하자 마자. api code 성공 상태로 추가
        apiResponseHandler.attachFailCode(nErrorCode, strMsgCode, strErrorMessage)

        dictOutput = apiResponseHandler.outResponse()
        
        raise HTTPException(status_code = nErrorCode, detail = dictOutput) 
        # pass
    
    ############################################################# private
    
    #Output 결과 업데이트, 모듈 재활용    
    def __updateOutputContents(self, dictFinalResult:dict, dictFilterOutput:dict):
        
        '''
        '''
        
        nActionCode:int = dictFilterOutput.get(ApiParameterDefine.OUT_ACTION_CODE, PipelineFilterDefine.CODE_ALLOW)
        
        strMaskedContents:str = dictFilterOutput.get(ApiParameterDefine.OUT_MASKED_CONTENTS, "")
            
        # #block message
        strBlockContents:str = dictFilterOutput.get(ApiParameterDefine.OUT_BLOCK_MESSAGE, "")
        
        dictFinalResult[ApiParameterDefine.OUT_ACTION] = nActionCode
        dictFinalResult[ApiParameterDefine.OUT_BLOCK_MESSAGE] = strBlockContents
        dictFinalResult[ApiParameterDefine.OUT_MASKED_CONTENTS] = strMaskedContents
        
        return ERR_OK
        
        
        