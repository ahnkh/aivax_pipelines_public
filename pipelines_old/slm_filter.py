
import os

import uuid
from datetime import datetime, timezone

from lib_include import *

from type_hint import *

'''
title: PII Masking Inlet Filter
author: wins-tech
version: 1.0.1
license: MIT
description: Masks user input via a local FastAPI service before sending to the model.
requirements: requests
'''


def _has_model_dump() -> bool:
    return hasattr(BaseModel, "model_dump")

class Pipeline(PipelineBase):
    """
    Filter-style pipeline:
      - inlet():  user → (mask) → model
      - outlet(): model → (pass-through) → user
    """

    def __init__(self):
        super().__init__()
        self.type = "filter"
        self.id = "pii_mask_inlet_filter"
        self.name = "SLM Filter"

        class Valves(BaseModel):
            pipelines: List[str] = Field(default_factory=lambda: ["*"], description="적용 파이프라인('*'=전체)")
            priority: int = Field(default=0, description="필터 실행 우선순위(높을수록 먼저)")
            enabled: bool = Field(default=True, description="필터 ON/OFF")

            #PII_API_URL: str = os.getenv("PII_API_URL", "http://host.docker.internal:9292/mask")
            PII_API_URL: str = os.getenv("PII_API_URL", "http://vax-pipelines:9292/mask")
            TIMEOUT_SECONDS: int = int(os.getenv("PII_TIMEOUT", "10"))
            ENABLE_LOG: bool = False
            FALLBACK_ON_ERROR: bool = True

            # OpenSearch 설정
            os_enabled: bool = True
            os_url: str = "https://vax-opensearch:9200"
            os_index: str = "regex_filter"
            os_user: Optional[str] = "admin"
            os_pass: Optional[str] = "Sniper123!@#"
            os_insecure: bool = True
            os_timeout: int = 3

            # pydantic v1 호환: BaseModel.dict()를 model_dump 이름으로 노출
            if not _has_model_dump():
                def model_dump(self, *args, **kwargs):  # type: ignore
                    return self.dict(*args, **kwargs)
        self.valves = Valves()
        
    # ---------- 파이프라인 엔트리 ----------    
    async def inlet(self, body: Dict[str, Any], __user__: Optional[dict] = None, dictExtParameter:dict = None, dictOuputResponse:dict = None, __request__: Optional[Request] = None) -> Dict[str, Any]:
        """
        - 마지막 user 메시지를 로컬 PII API로 마스킹
        - os_doc_final JSON 구성
        - (옵션) OpenSearch 인덱스에 저장
        - 마스킹된 텍스트로 messages 갱신 후 반환
        """
        # print(f"test ##1")
        
        # disabled 시 그대로 통과
        
        # if not getattr(self.valves, "enabled", True):
        #     LOG().info("action disabled")
        #     return body
    
        api_url   = self.valves.PII_API_URL
        timeout   = self.valves.TIMEOUT_SECONDS
        log_on    = self.valves.ENABLE_LOG
        fallback  = self.valves.FALLBACK_ON_ERROR
    
        # # OpenSearch 밸브(옵션)
        # enable_os = bool(getattr(self.valves, "ENABLE_OS", False))
        # os_url    = getattr(self.valves, "OS_URL", "")
        # os_index  = getattr(self.valves, "OS_INDEX", "")
        # os_user   = getattr(self.valves, "OS_USER", "")
        # os_pass   = getattr(self.valves, "OS_PASS", "")
    
        # print(f"test ##3")
        try:
            messages: List[Dict[str, Any]] = body.get("messages", [])
            if not messages:
                return body
    
            # print(f"test ##4")
            # ===== 메타 수집 (우선순위 반영) =====
            meta = body.get("metadata") or {}
    
            # __user__, __request__ 가 글로벌로 존재할 가능성 대응
            __user__ = globals().get("__user__")
            __request__ = globals().get("__request__")
    
            # user id/email: user(param) → __user__ → 기본값
            user_id = None
            user_email = None
            
            if isinstance(__user__, dict):
                user_id = __user__.get("id") or __user__.get("name")
                user_email = __user__.get("email")
                
            if user_id is None or user_email is None:
                if isinstance(__user__, dict):
                    user_id = user_id or __user__.get("id") or __user__.get("name")
                    user_email = user_email or __user__.get("email")
            user_id = user_id or ""
            user_email = user_email or ""
    
            # print(f"test ##5")
            # message/session id: metadata 최우선 → body/대체
            msg_id  = meta.get("message_id") or body.get("id") or str(uuid.uuid4())
            sess_id = meta.get("session_id") or body.get("conversation_id") or user_id or f"sess-{uuid.uuid4()}"
    
            # client ip: metadata → __request__ → user
            client_ip = meta.get("client_ip")
            if not client_ip and __request__ is not None:
                try:
                    client_ip = __request__.client.host
                except Exception:
                    pass
            if not client_ip and isinstance(__user__, dict):
                client_ip = __user__.get("ip")
            client_ip = client_ip or ""
    
            # ===== 마지막 user 메시지 찾아 마스킹 =====
            masked_text = None
            original_text = None
    
            # print(f"test ##6")
            for i in range(len(messages) - 1, -1, -1):
                # print(f"test ##6-1")
                if messages[i].get("role") == "user":
                    original_text = messages[i].get("content", "")
                    # print(api_url)
                    # print(original_text)
                    
                    #TODO: pii 요청, ssl proxy의 성능 저하 가능성 (개선 필요)
                    resp = requests.post(api_url, json={"text": original_text}, timeout=timeout)
                    resp.raise_for_status()
                    
                    data:dict = resp.json()
                    # print(f"test ##6-3")
                    # print(data)
    
                    # 응답 키 후보
                    masked_text = (
                        data.get("masked_text")
                        or data.get("text")
                        or data.get("result")
                        or original_text
                    )
                    # print(f"test ##6-4")
                    messages[i]["content"] = masked_text
                    if log_on:
                        # print(f"[PII-MASK] original={repr(original_text)[:200]} -> masked={repr(masked_text)[:200]}")
                        LOG().info(f"[PII-MASK] original={repr(original_text)[:200]} -> masked={repr(masked_text)[:200]}")
    
                    # print(f"test ##6-5")
                    # 정책/탐지 부가정보(있으면 수용)
                    policy = data.get("policy", {}) if isinstance(data.get("policy"), dict) else {}
                    dictDetectedRule = {
                        "id":   policy.get("id",   ""),
                        "name": policy.get("name", "")
                    }
    
                    # print(f"test ##6-6")
                    std_action   = data.get("mode", "allow")
                    should_block = bool(data.get("should_block", std_action == "block"))
    
                    # print(f"test ##6-7")
                    pii_block = data.get("pii") if isinstance(data.get("pii"), dict) else None
                    if not pii_block:
                        pii_block = {
                            "types": "API Key",
                            "samples": "reasons: API 키의 탐지, 기밀 정보, 민감정보, 세부 지침 사항, 이모지 금지",
                            "confidence": 1.0
                        }
    
                    # print(f"test ##6-8")
                    # ===== OpenSearch 문서 구성 =====
                    os_doc_final = {
                        "@timestamp": ts_isoz(),
                        "filter": self.id,
                        "filter_name": self.name,
                        "content": masked_text,          # 마스킹 결과
                        "message": original_text,        # 원문
    
                        "request": {"id": msg_id},
                        "session": {"id": sess_id},
                        "user": {"id": user_id, "email": user_email},
    
                        "stage": "slm_filter",
                        "should_block": should_block,
                        "mode": std_action,
    
                        "policy_id": dictDetectedRule.get("id", ""),
                        "policy_name": dictDetectedRule.get("name", ""),
                        "src": {"ip": client_ip},
    
                        "pii": pii_block,
    
                        # masked contents 추가
                        "masked_contents": masked_text,
                        # "final_action": fa_internal,
                    }
                    
                    #debug 로그 추가
                    # LOG().info(f"log = {os_doc_final}")

                    # print(os_doc_final)
                    # print(f"test ##6-9")
                    self.AddLogData(LOG_INDEX_DEFINE.KEY_REGEX_FILTER, os_doc_final)
    
                    break  # 마지막 user 메시지 하나만 처리
    
            # print(f"test ##7")
            if masked_text is None and original_text is None and log_on:
                print("[PII-MASK][INFO] no user message found; pass-through")
    
            # print(f"test ##8")
            body["messages"] = messages

            #TODO: ssl inspection 에서 호출이 필요할경우, 메시지 구조 개선 필요
            #우선은 현재 구조를 유지한다.
            # if std_action == "block":
            #     block_message = f"🚫 보안 정책에 의해 차단되었습니다. 메시지에 민감정보가 포함되어 있으니 해당 정보를 제거한 후 다시 시도해 주세요." 
            #     raise Exception(block_message)

            # return body
            
            return ERR_OK
    
        except Exception as e:
            
            LOG().error(traceback.format_exc())
            
            # #TODO: fallback, 무슨 기능인지 확인
            # if fallback:
            #     # print(f"[PII-MASK][WARN] masking failed: {e}")
            #     return body

    async def outlet(self, body: Dict[str, Any], user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return body

