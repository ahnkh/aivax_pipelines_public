# filters/regex_and_entropy_secret_filter.py
# OpenWebUI Pipelines - Filter
# 목적: 유저 프롬프트(content)에서
#   1) 알려진 시크릿 패턴(AWS/Azure/Base64HighEntropy/BasicAuth/Cloudant/Discord/GitHub/JWT/Keyword/Mailchimp/PrivateKey/Slack/Stripe/Twilio)
#   2) 엔트로피 높은 토큰(완화 임계치)
# 를 탐지하여 해당 "토큰/값"만 [MASKING]으로 치환

# import re
# import math
# import logging
# from typing import Any, Dict, List, Optional, Tuple
# from pydantic import BaseModel, Field

from lib_include import *

from type_hint import *

'''
2025.10.21 pipeline과 pipeliemainapp간 공유
mainapp와는 양방향 구조로 가져간다.
webapp는 mainapp를 통해서 pipeline에 접근한다.
'''

# MASK_DEFAULT = "[MASKING]"
MASK_DEFAULT = "[AIVAX MASKING]"

class Pipeline(PipelineBase):
    
    def __init__(self):        
        '''
        '''
        
        super().__init__()
        
        self.type = "filter"
        self.id = "secret_filter"
        self.name = "secret_filter"
        
        self.valves = self.Valves()
        
        #TODO: 사용하지 않는 필드, 향후 제거
        self.toggle = True
        # self.logger = self._setup_logger()
        
        #TODO: 하단의 정규 표현식은, 정책으로 분리한다.

        ''' #위치 이동 -> detect_secret_filter_pattern
        # ---------- 멀티라인/블록 패턴 ----------
        # PrivateKeyDetector: PEM 블록
        self.re_pem_block = re.compile(
            r"-----BEGIN (?P<K>[^-\r\n]+?) KEY-----[\s\S]+?-----END (?P=K) KEY-----",
            re.MULTILINE,
        )
        # JwtTokenDetector: JWT 토큰
        self.re_jwt = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

        # ---------- 알려진 패턴(값 그룹명 group='VAL' 권장, 필요시 개별 그룹명) ----------
        key_kv = r"(?:api[_-]?key|x-api-key|api[_-]?token|x-api-token|auth[_-]?token|password|passwd|pwd|secret|private[_-]?key)"
        sep = r"\s*[:=]\s*"

        # (label, pattern, value_group_name) — group 없으면 전체 매치 사용
        self.known_patterns: List[Tuple[str, re.Pattern, Optional[str]]] = [
            # AWSKeyDetector
            ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|ANPA|ABIA)[0-9A-Z]{16}\b"), None),
            ("aws_secret_access_key", re.compile(r"(?<![A-Za-z0-9/+=])([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])"), None),

            # AzureStorageKeyDetector (connection string)
            ("azure_storage_account_key", re.compile(r"(?i)\bAccountKey=(?P<VAL>[A-Za-z0-9+/=]{30,})"), "VAL"),
            ("azure_conn_string", re.compile(r"(?i)\bDefaultEndpointsProtocol=\w+;AccountName=\w+;AccountKey=(?P<VAL>[A-Za-z0-9+/=]{30,})"), "VAL"),

            # Base64HighEntropyString — 정규식으로 직접 잡기보다는 엔트로피가 담당(아래)

            # BasicAuthDetector: scheme://user:pass@host
            ("basic_auth_creds", re.compile(r"(?i)\b(?:https?|ftp|ssh)://(?P<CREDS>[^:@\s/]+:[^@\s/]+)@"), "CREDS"),

            # CloudantDetector: https://user:pass@<account>.cloudant.com
            ("cloudant_creds", re.compile(r"(?i)https?://(?P<CREDS>[^:@\s/]+:[^@\s/]+)@[^/\s]*\.cloudant\.com"), "CREDS"),

            # DiscordBotTokenDetector
            ("discord_bot_token", re.compile(r"\b(?P<VAL>[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27})\b"), "VAL"),

            # GitHubTokenDetector (classic/pat 등)
            ("github_token", re.compile(r"\b(?P<VAL>(?:ghp|gho|ghu|ghs|ghr)[-_][A-Za-z0-9]{16,})\b"), "VAL"),

            # MailchimpDetector (키 형태: 32 hex + -usN)
            ("mailchimp_api_key", re.compile(r"\b(?P<VAL>[0-9a-f]{32}-us\d{1,2})\b"), "VAL"),

            # SlackDetector
            ("slack_token", re.compile(r"\b(?P<VAL>xox[abprs]-[A-Za-z0-9-]{10,})\b"), "VAL"),
            ("slack_webhook_path", re.compile(r"(?i)https://hooks\.slack\.com/services/(?P<VAL>T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+)"), "VAL"),

            # StripeDetector
            ("stripe_secret", re.compile(r"\b(?P<VAL>sk_(?:live|test)_[A-Za-z0-9]{16,})\b"), "VAL"),
            ("stripe_publishable", re.compile(r"\b(?P<VAL>pk_(?:live|test)_[A-Za-z0-9]{16,})\b"), "VAL"),

            # TwilioKeyDetector
            ("twilio_account_sid", re.compile(r"\b(?P<VAL>AC[0-9a-fA-F]{32})\b"), "VAL"),
            ("twilio_auth_token", re.compile(r"(?<![A-Za-z0-9])(?P<VAL>[0-9a-fA-F]{32})(?![A-Za-z0-9])"), "VAL"),

            # KeywordDetector (일반 할당형)
            ("kv_quoted", re.compile(rf'(?i)\b{key_kv}\b{sep}"(?P<VAL>[^"\r\n]{{6,}})"'), "VAL"),
            ("kv_single_quoted", re.compile(rf"(?i)\b{key_kv}\b{sep}'(?P<VAL>[^'\r\n]{{6,}})'"), "VAL"),
            ("kv_bare", re.compile(rf"(?i)\b{key_kv}\b{sep}(?P<VAL>[^\s\"'`]{{8,}})"), "VAL"),

            # OpenAI/Custom-like
            ("openai_like", re.compile(r"\b(?P<VAL>sk-[A-Za-z0-9]{16,})\b"), "VAL"),
            # 사내/커스텀 접두(예: ak-, tk- ... -dev/-test 꼬리)
            ("ak_tk_token", re.compile(r"\b(?P<VAL>(?:ak|tk)-[a-f0-9]{16,}(?:-(?:dev|test)[a-z0-9]*)?)\b"), "VAL"),
        ]

        # ---------- 엔트로피 후보/도우미 ----------
        self.re_candidate = re.compile(r"[A-Za-z0-9+/=._\-]{16,}")  # 후보 토큰(완화)
        self.re_b64_shape = re.compile(r"^[A-Za-z0-9+/=]+$")
        self.re_hex_shape = re.compile(r"^[A-Fa-f0-9]+$")
        '''
        
        pass
    
    class Valves(BaseModel):
        pipelines: List[str] = Field(default_factory=lambda: ["*"], description="적용 파이프라인('*'=전체)")
        priority: int = Field(default=0, description="필터 실행 우선순위(높을수록 먼저)")
        enabled: bool = Field(default=True, description="필터 ON/OFF")
        log_to_console: bool = Field(default=True, description="콘솔 로그 출력")
        mask_char: str = Field(default=MASK_DEFAULT, description="치환 문자열")
        # 엔트로피/길이 임계치(완화값: 너무 높다는 피드백 반영)
        min_len_b64: int = Field(default=20, description="Base64 모양 최소 길이(기본 20)")
        min_len_hex: int = Field(default=28, description="Hex 모양 최소 길이(기본 28)")
        min_len_mixed: int = Field(default=20, description="혼합 문자군 최소 길이(기본 20)")
        thr_b64: float = Field(default=4.0, description="Base64 모양 엔트로피 임계치(기본 4.0)")
        thr_hex: float = Field(default=3.0, description="Hex 모양 엔트로피 임계치(기본 3.0)")
        thr_mixed: float = Field(default=3.8, description="혼합 모양 엔트로피 임계치(기본 3.8)")
        # 프리픽스 완화(사내 토큰 접두 등)
        prefix_relax: bool = Field(default=True, description="특정 접두 토큰(ak-, tk-, ghp-/_) 완화 룰 적용")
        
        
        ############ 2차 모델 시연을 위한 임시 소스 추가
        # OpenSearch 설정
        os_enabled: bool = True
        os_url: str = "https://vax-opensearch:9200"
        os_index: str = "regex_filter"
        os_user: Optional[str] = "admin"
        os_pass: Optional[str] = "Sniper123!@#"
        os_insecure: bool = True
        os_timeout: int = 3
        
        # 저장 옵션 => TODO: 미사용 옵션으로 보이며, 사용 출처 불분명
        store_response_text: bool = True          # 응답 전문 저장 여부
        response_max_bytes: int = 200_000         # 응답 텍스트 최대 바이트(UTF-8 기준)
        hash_only: bool = False                   # 전문 대신 해시만 저장
        include_filters_meta: bool = True         # body["_filters"] 저장
        include_usage: bool = True                # 토큰/지연 등 사용량 저장
        pass

        
    ########################################### public
    
    # ---------- 파이프라인 엔트리 ----------
    async def inlet(self, body: Dict[str, Any], __user__: Optional[dict] = None, dictExtParameter:dict = None, dictOuputResponse:dict = None, __request__: Optional[Request] = None) -> Dict[str, Any]:
        
        '''
        TODO: 기존 형상은 가급적 유지
        
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
        
        "user:
        {
            "name" : "khan",
            "email" : "ghahn@wins21.co.kr"
        }
        
        TODO: 예외처리는 raise 로 대체.
            
        '''
        
        #chat completion을 통해 호출시, 예외처리
        if None == dictOuputResponse:
            dictOuputResponse = {}  
        
        #기본적인 응답 처리, action필드를 기본값으로 설정, TODO: 공통화
        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
        
        #설명 문자열, 각 filter마다 추가. 크게 의미는 없다.
        dictOuputResponse[ApiParameterDefine.OUT_DESRIPTION] = f"{self.name} filter 차단을 수행합니다."
        
        if not self.valves.enabled:
            LOG().info("action disabled")
            
            # raise Exception(f"action disabled, id = {self.id}")
            # raise HTTPException(
            #     status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            #     detail=f"action disabled, id = {self.id}")
            
            #body의 전달은, 사이드 이펙트가 우려되어 유지.
            return body

        messages = body.get("messages") or []
        
        last:dict = messages[-1]
        content = last.get("content")
        
        #테스트용 로그 추가
        LOG().debug(f"run detect secret inlet, prompt = {content}")
        
        #detect_secret, 다수 실행되는 현상, 마지막만 읽어들인다.
        # last:dict = messages[-1]
        # content = last.get("content")
        
        if not isinstance(messages, list):
            # LOG().error(f"invalid messages, {messages}")
            raise Exception(f"invalid messages format, id = {self.id}, message = {messages}")
            # raise HTTPException(
            #     status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            #     detail=f"invalid messages format, id = {self.id}, message = {messages}")
            
            # return body
            
        #TODO: 순환참조 우려로, 함수내 import (import 구문의 singleton 패턴 방식에 의지)
        from block_filter_modules.filter_pattern.filter_pattern_manager import FilterPatternManager
        from block_filter_modules.filter_pattern.helper.detect_secret_filter_pattern import DetectSecretFilterPattern
        detectSecretFilterPattern:DetectSecretFilterPattern = self.GetFilterPatternModule(FilterPatternManager.PATTERN_FILTER_DETECT_SECRET)


        #우선 아래와 같이 수정한다. 기존 구조 유지
        messages = messages[-1:]

        #message, 다수 구조를 고려한다.=> 이렇게 되면 문제가 된다.
        for msg in messages:
            
            if msg.get("role") != "user":
                continue
            
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                # LOG().error(f"invalid content, {content}")
                
                raise Exception(f"invalid content format, id = {self.id}, content = {content}")
            
                # raise HTTPException(
                #     status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                #     detail=f"invalid content format, id = {self.id}, content = {content}")
                
                # continue

            #TODO: detect span 기능, 통째로 이관
            # spans, counts = self.__detect_spans(content)
            
            #TODO: 구조 변경 필요, valve 클래스, 참조가 어려운 문제
            valves = self.valves
            (spans, counts, dictDetectedRule) = detectSecretFilterPattern.DetectPattern(content, valves)
            
            LOG().info(f"Masked: {counts}, len = {len(spans)}")
            
            if spans:
                
                #TODO: 우선 개발, counts의 필드에 따른 분기, 우선 수정후 2차 리펙토링시 개선한다.                
                # nAcceptCount = counts.get("accept")
                nBlockCount = counts.get("block")
                nMaskingCount = counts.get("masking")
                
                strBlockMessage:str = self.__customBlockMessage()
                
                #block 먼저 체크
                if 0 < nBlockCount:
                
                    # masked = self.__maskSpans(content, spans)
                    # msg["content"] = masked
                    
                    #TODO: action 값, 정수로 바꾼다. testRule은 문자를 유지한다. (백엔드 에서 사용이 우려)
                    dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
                    dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_BLOCK
                    
                    #TODO: maskinig 이든, block 이든 masking 처리 한다.
                    masked = self.__maskSpans(content, spans)
                    msg["content"] = masked
                    dictOuputResponse[ApiParameterDefine.OUT_MASKED_CONTENTS] = masked
                    
                    #strBlockMessage:str = self.__customBlockMessage()
                    dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = strBlockMessage
                    
                elif 0 < nMaskingCount:
                    
                    dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_MASKING
                    dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_MASKING
                    
                    masked = self.__maskSpans(content, spans)
                    # msg["content"] = masked
                    dictOuputResponse[ApiParameterDefine.OUT_MASKED_CONTENTS] = masked

                    #TODO: 여기서부터는 협의 필요                    
                    dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = strBlockMessage
                    
                    
                #테스트, LLM으로 변조된 메시지를 보내는게 주요 기능이다.
                # ★ LLM에게 안내문을 '그대로 출력'하도록 지시
                # block_notice = "[AIVAX] 민감정보의 유출이 감지되어 차단되었습니다. 개인정보를 제외하고 다시 시도해주세요."
                last = (body.get("messages") or [])[-1]
                # last["content"] = (
                #     "다음 문장을 사용자에게 그대로 출력하세요(추가 설명/수정/확장/사과문/이모지 금지):\n"
                #     f"{strBlockMessage}"
                # )
                
                
            else:
                LOG().info("No secrets detected (regex+entropy).")
                # self.logger.info("No secrets detected (regex+entropy).")
                
                # dictOuputResponse["action"] = "allow"
                dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
                dictOuputResponse[ApiParameterDefine.OUT_ACTION_CODE] = PipelineFilterDefine.CODE_ALLOW
                
                dictOuputResponse[ApiParameterDefine.OUT_MASKED_CONTENTS] = ""

                #TODO: 여기서부터는 협의 필요                    
                dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = ""
                
            # TODO: helper 생성 필요
            # 우선 테스트.
            std_action = dictOuputResponse.get(ApiParameterDefine.OUT_ACTION)
            
            meta = body.get("metadata") or {}
            user_id = (__user__ or {}).get("name") if isinstance(__user__, dict) else None
            user_email = (__user__ or {}).get("email") if isinstance(__user__, dict) else None
            msg_id = meta.get("message_id")
            sess_id = meta.get("session_id")
            client_ip = __request__.client.host

            os_doc_final = {
                "@timestamp": ts_isoz(),
                "filter" : self.id,
                "filter_name": self.name,
                "content": content,
                "message":msg,
                
                "request": {"id": msg_id},
                "session": {"id": sess_id},
                "user": {"id": user_id, "email": user_email},

                # "event":   {"id": msg_id, "type": "detect"},
                # "request": {"id": msg_id},
                # "session": {"id": sess_id},
                # "user":    {"id": user_id},
                "stage":   "detect_secrets",
                # "detection": detection_status,
                "should_block": (std_action == "block"),
                "mode": std_action,
                
                "policy_id" : dictDetectedRule.get("id", ""),
                "policy_name" : dictDetectedRule.get("name", ""),
                "src":     {"ip": client_ip},
                
                "pii": {
                    "types": "API Key",
                    "samples": "reasons: API 키의 탐지, 기밀 정보, 민감정보, 세부 지침 사항, 이모지 금지",
                    "confidence": 1.0
                },
                
                #masked contents 추가
                "masked_contents" : dictOuputResponse.get(ApiParameterDefine.OUT_MASKED_CONTENTS)
                
                # "final_action": fa_internal,
            }

            # self._index_opensearch(os_doc_final)
            self.AddLogData(LOG_INDEX_DEFINE.KEY_REGEX_FILTER, os_doc_final)

            '''
            #2025.11.15 2단계 모델에 반영되었으나, 3단계 모델에서는 ssl proxy로 전달되지 않아 주석 처리
            if std_action == "block":
                block_message = f"🚫 보안 정책에 의해 차단되었습니다. 메시지에 민감정보가 포함되어 있으니 해당 정보를 제거한 후 다시 시도해 주세요." 
                raise Exception(block_message)
            '''

        return body
    
    #룰 테스트 메소드 추가
    async def testRule(self, strPrompt:str, strRule:str, strAction:str, dictOuputResponse:dict, request:Request):
        
        '''
        TODO: 우선 개발후, 2차 리펙토링 필수
        TODO: 사용자 정보는 현재는 사용하지 않는다. (향후 사용자에 대한 식별 정리후 사용)
        '''
        
        #TODO: 순환참조 우려로, 함수내 import (import 구문의 singleton 패턴 방식에 의지)
        from block_filter_modules.filter_pattern.filter_pattern_manager import FilterPatternManager
        from block_filter_modules.filter_pattern.helper.detect_secret_filter_pattern import DetectSecretFilterPattern
        detectSecretFilterPattern:DetectSecretFilterPattern = self.GetFilterPatternModule(FilterPatternManager.PATTERN_FILTER_DETECT_SECRET)
        
        (spans, counts, dictDetectedRule) = detectSecretFilterPattern.TestRulePattern(strPrompt, strRule, strAction)
        
        #TODO: 반드시 다시 개발
        #사양변경, content 필드를 활용, masking일때는 masking 된 데이터를 보여주고
        #차단일때는 임의의 해당 문구를 추가
        #[AIVAX] 요청하신 문의 사항은 정책을 위반한 사항으로 차단 되었습니다.
        
        if spans:
                
            #TODO: 우선 개발, counts의 필드에 따른 분기, 우선 수정후 2차 리펙토링시 개선한다.                
            # nAcceptCount = counts.get("accept")
            nBlockCount = counts.get("block")
            nMaskingCount = counts.get("masking")
            
            #block 먼저 체크
            if 0 < nBlockCount:
            
                # masked = self.__maskSpans(content, spans)
                # msg["content"] = masked
                
                dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK
                
                #TODO: maskinig 이든, block 이든 masking 처리 한다.
                masked = self.__maskSpans(strPrompt, spans)     
                
                strCustomContent:str = '''
                허용되지 않은 프롬프트가 포함되어 요청이 차단되었습니다. 
                '''
                           
                dictOuputResponse[ApiParameterDefine.OUT_CONTENT] = strCustomContent
                
                strBlockMessage:str = self.__customBlockMessage()
                dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = strBlockMessage
                
            elif 0 < nMaskingCount:
                
                dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_MASKING
                
                masked = self.__maskSpans(strPrompt, spans)                
                dictOuputResponse[ApiParameterDefine.OUT_CONTENT] = masked

                #TODO: 여기서부터는 협의 필요                    
                strBlockMessage:str = self.__customBlockMessage()
                dictOuputResponse[ApiParameterDefine.OUT_BLOCK_MESSAGE] = strBlockMessage
            
        else:
            LOG().info("No secrets detected (regex+entropy).")
            # self.logger.info("No secrets detected (regex+entropy).")
            
            # dictOuputResponse["action"] = "allow"
            dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW
        
        return ERR_OK 

    async def outlet(self, body: Dict[str, Any], __event_emitter__=None, __user__: Optional[dict] = None) -> Dict[str, Any]:
        return body
    
    
    ############################################################ private
    
    #TODO: 이 함수는 detect secret으로 유지한다. 이름만 변경
    def __maskSpans(self, text: str, spans: List[Tuple[int, int]]) -> str:
        
        '''
        '''
        
        if not spans:
            return text
        
        spans = sorted(spans, key=lambda x: x[0])
        merged: List[Tuple[int, int]] = []
        
        for s, e in spans:
            if not merged or s > merged[-1][1]:
                merged.append((s, e))
            else:
                ps, pe = merged[-1]
                merged[-1] = (ps, max(pe, e))
        
        out = []
        last = 0
        
        mask = self.valves.mask_char or MASK_DEFAULT
        
        for s, e in merged:
            out.append(text[last:s])
            out.append(mask)
            last = e
            
        out.append(text[last:])
        
        return "".join(out)
    
    #차단 메시지, 우선 하드코딩, 향후 ouput 데이터의 처리 모듈을 개발한다.
    def __customBlockMessage(self, ) -> str:
        
        '''
        시연용 하드코딩
        '''
        
        strBlockMessage:str = '''[AIVAX] 프롬프트 차단
AIVAX 정책에 의해 민감정보가 프롬프트에 포함된 것으로 탐지되었습니다.
❌탐지 유형은 'API 키의 탐지' 입니다.
민감 정보를 전송할 경우, 기밀 정보 또는 개인 정보 유출등의 피해가 발생할 수 있으니 각별한 주의를 부탁드려요
요청하신 프롬프트는 AIVAX에 의해서 요청이 차단되었습니다.
세부 지침 사항은 관리자에게 문의해주세요
        '''
        
        return strBlockMessage
    
    
    # #opensearch 저장, 과거 소스도 유지, 옵션으로 저장 방식을 선택하는 방향으로 개선한다.
    # def _index_opensearch(self, doc: Dict[str, Any]) -> bool:
        
    #     import base64
    #     import ssl
        
    #     v = self.valves
    #     if not v.os_enabled:
    #         return False

    #     url = f"{v.os_url.rstrip('/')}/{v.os_index}/_doc"
    #     payload = json.dumps(doc, ensure_ascii=False).encode("utf-8")

    #     # 1) requests 우선
    #     try:
    #         import requests
    #         auth = (v.os_user, v.os_pass) if v.os_user else None
    #         verify = not v.os_insecure
    #         r = requests.post(
    #             url,
    #             data=payload,
    #             headers={"Content-Type": "application/json"},
    #             auth=auth,
    #             verify=verify,
    #             timeout=v.os_timeout,
    #         )
    #         ok = r.status_code in (200, 201)
            
    #         if not ok:                
    #             # self.logger.warning("[response->OS] status=%s body=%s", r.status_code, r.text[:400])
    #             LOG().info(f"[response->OS] status={r.status_code} body={r.text[:400]}")
                
    #         return ok
    #     except Exception as e:
    #         # self.logger.debug("[response->OS] requests failed: %r -> fallback to urllib", e)
    #         LOG().debug(f"[response->OS] requests failed: {e} -> fallback to urllib")
    #         LOG().error(traceback.format_exc())

    #     # 2) urllib 폴백
    #     try:
    #         from urllib.request import Request, urlopen
    #         headers = {"Content-Type": "application/json"}
    #         if v.os_user:
    #             token = base64.b64encode(f"{v.os_user}:{v.os_pass or ''}".encode()).decode()
    #             headers["Authorization"] = f"Basic {token}"

    #         req = Request(url, data=payload, headers=headers, method="POST")
    #         ctx = None
    #         if url.startswith("https://") and v.os_insecure:
    #             ctx = ssl._create_unverified_context()

    #         with urlopen(req, timeout=v.os_timeout, context=ctx) as resp:
    #             status = getattr(resp, "status", 200)
    #             ok = status in (200, 201)
    #             if not ok:
    #                 body = resp.read(512).decode("utf-8", "ignore")
    #                 # self.logger.warning("[response->OS] urllib bad status=%s body=%s", status, body)
    #                 LOG().info(f"[response->OS] urllib bad status={status} body={body}")
    #             return ok
    #     except Exception as e:
    #         # self.logger.warning("[response->OS] urllib failed: %r", e)
    #         LOG().error(traceback.format_exc())
    #         return False

        
    
    
