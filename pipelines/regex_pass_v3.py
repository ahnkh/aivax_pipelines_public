from typing import List, Optional, Dict, Any, Union, Tuple
from pydantic import BaseModel, Field
import re
import json
from dataclasses import dataclass
from datetime import datetime as _dt
import base64, ssl

from lib_include import *

from type_hint import *
from datetime import datetime as _dt

class RegexBlockedError(RuntimeError):
    """이전 버전 호환용(현재는 예외를 던지지 않음)"""
    pass

# ----------------------------
# 간단한 Regex 스캐너(자체 구현)
# ----------------------------
@dataclass
class ScanResult:
    decision: str = ""
    score: float = 0.0
    reason: str = ""

class SimpleRegexScanner:
    def __init__(self) -> None:
        self._patterns: List[re.Pattern] = []

    def add_pattern(self, pattern: re.Pattern) -> None:
        if isinstance(pattern, re.Pattern):
            self._patterns.append(pattern)

    async def scan(self, text: str) -> ScanResult:
        if not self._patterns:
            return ScanResult(decision="ALLOW", score=0.0, reason="no pattern")
        for pat in self._patterns:
            if pat.search(text):
                return ScanResult(decision="FLAG", score=1.0, reason=f"pattern matched: {pat.pattern}")
        return ScanResult(decision="ALLOW", score=0.0, reason="no match")

# ----------------------------
# 유틸 함수
# ----------------------------
def _luhn_check(number: str) -> bool:
    digits = re.sub(r"\D", "", number or "")
    if not digits:
        return False
    s = 0
    rev = digits[::-1]
    for i, ch in enumerate(rev):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        s += d
    return (s % 10) == 0

def _to_upper_decision(decision: Any) -> str:
    if decision is None:
        return ""
    if isinstance(decision, str):
        return decision.upper()
    name = getattr(decision, "name", None)
    if isinstance(name, str):
        return name.upper()
    value = getattr(decision, "value", None)
    if isinstance(value, str):
        return value.upper()
    try:
        return str(decision).upper()
    except Exception:
        return ""

# ----------------------------
# 빠른 PII 탐지기 (주소 패턴 추가)
# ----------------------------
class _QuickPII:
    EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?=$|\s|[<>,;:)\]]|[^\x00-\x7F])")
    KR_MOBILE = re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b")
    KR_PHONE  = re.compile(r"\b0\d{1,2}-\d{3,4}-\d{4}\b")
    CARD_CAND = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
    KR_RRN    = re.compile(r"\b(\d{2})(\d{2})(\d{2})-?([1-4])(\d{6})\b")
    PASSPORT_KR = re.compile(r"\b[MSR]\d{8}\b", re.I)
    IP_ADDR = re.compile(
        r"\b(?:(?:\d{1,3}\.){3}\d{1,3}"
        r"|"
        r"(?:[A-F0-9]{1,4}:){7}[A-F0-9]{1,4})\b",
        re.I,
    )

    DRIVER_LICENSE_KR = re.compile(r"\b\d{2}-?\d{2}-?\d{6}\b")
    FOREIGN_RRN = re.compile(r"\b(\d{2})(\d{2})(\d{2})-?([5-8])(\d{6})\b")
    BUSINESS_REG = re.compile(r"\b\d{3}-\d{2}-\d{5}\b")
    HEALTH_INSURANCE = re.compile(r"(?:건강보험(?:증|번호)?[:\s\-]*|NHIS[:\s\-]*)[0-9\-]{6,15}", re.I)
    TAX_TIN = re.compile(r"(?:\bTIN\b|\bTax ID\b|납세자번호|세금번호)[:\s\-]*[0-9\-]{5,20}", re.I)
    EIN = re.compile(r"\b\d{2}-\d{7}\b")
    
    KR_ADDRESS_ROAD = re.compile(
        r"(?:"
        r"[가-힣]{2,}(?:특별시|광역시|특별자치시|시|도|군|구)\s+"
        r"[가-힣0-9\s]*(?:구|시|군)\s+"
        r"[가-힣0-9\s]*(?:로|길)\s+\d+(?:-\d+)?"
        r"(?:\s*,?\s*\d+층|동|호)?"
        r"|"
	    r"[가-힣]{2,}(?:특별시|광역시|특별자치시|시|도|군|구)\s+"
        r"[가-힣0-9\s]*(?:구|시|군)\s+"
        r"[가-힣]+동\s+\d+(?:-\d+)?(?:번지|번)"
        r")",
        re.MULTILINE
    )
    
    KR_ADDRESS_POSTAL = re.compile(
        r"(?:\(?\d{5}\)?\s*)"
        r"[가-힣]{2,}(?:특별시|광역시|특별자치시|시|도|군|구)\s+"
        r"[가-힣0-9\s]*(?:구|시|군)\s+"
        r"[가-힣0-9\s\-,]*"
        r"(?:\d+(?:-\d+)?(?:번지|번|호|층)?)",
        re.MULTILINE
    )
    
    KR_ADDRESS_SIMPLE = re.compile(
        r"(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)"
        r"(?:특별시|광역시|특별자치시|시|도)?\s+"
        r"[가-힣]+(?:구|시|군)\s+"
        r"[가-힣0-9\s]*(?:로|길|동)\s*\d+",
        re.MULTILINE
    )
    
    KR_ADDRESS_BUILDING = re.compile(
        r"[가-힣]{2,}(?:특별시|광역시|특별자치시|시|도|군|구)\s+"
        r"[가-힣0-9\s]*(?:구|시|군)\s+"
        r"[가-힣]+동\s+\d+(?:-\d+)?\s+"
        r"[가-힣0-9\s]*(?:아파트|빌딩|타워|오피스텔|빌라|주택|상가)\s*"
        r"(?:\d+동\s*)?(?:\d+호)?",
        re.MULTILINE
    )

    @classmethod
    def hits_with_values(cls, text: Any, check_card_luhn: bool = True, max_per_type: int = 5) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        if not isinstance(text, str):
            try:
                text = str(text)
            except Exception:
                return out
        if not text:
            return out

        def add(tag: str, value: str):
            arr = out.setdefault(tag, [])
            if value not in arr and len(arr) < max_per_type:
                arr.append(value)


        for m in cls.EMAIL.finditer(text): add("email", m.group(0))
        for m in cls.KR_MOBILE.finditer(text): add("phone", m.group(0))
        for m in cls.KR_PHONE.finditer(text): add("phone", m.group(0))
        for m in cls.PASSPORT_KR.finditer(text): add("passport", m.group(0))
        for m in cls.KR_RRN.finditer(text): add("kr_rrn", m.group(0))
        for m in cls.IP_ADDR.finditer(text): add("ip", m.group(0))
        for m in cls.DRIVER_LICENSE_KR.finditer(text): add("driver_license", m.group(0))
        for m in cls.FOREIGN_RRN.finditer(text): add("foreign_rrn", m.group(0))
        for m in cls.BUSINESS_REG.finditer(text): add("business_reg", m.group(0))
        for m in cls.HEALTH_INSURANCE.finditer(text): add("health_insurance", m.group(0))
        for m in cls.TAX_TIN.finditer(text): add("tax_info", m.group(0))
        for m in cls.EIN.finditer(text): add("ein", m.group(0))
        
        for m in cls.KR_ADDRESS_ROAD.finditer(text): add("address", m.group(0).strip())
        for m in cls.KR_ADDRESS_POSTAL.finditer(text): add("address", m.group(0).strip())
        for m in cls.KR_ADDRESS_SIMPLE.finditer(text): add("address", m.group(0).strip())
        for m in cls.KR_ADDRESS_BUILDING.finditer(text): add("address", m.group(0).strip())

        saw_candidate = False
        for m in cls.CARD_CAND.finditer(text):
            raw = m.group(0)
            digits = re.sub(r"\D", "", raw)
            if 13 <= len(digits) <= 19:
                saw_candidate = True
                if (not check_card_luhn) or _luhn_check(digits):
                    add("credit_card", raw)
                    break
        if saw_candidate and "credit_card" not in out:
            for m in cls.CARD_CAND.finditer(text):
                raw = m.group(0)
                digits = re.sub(r"\D", "", raw)
                if 13 <= len(digits) <= 19:
                    add("credit_card_candidate", raw)
        return out

# ----------------------------
# 마스킹 유틸 (로그용: 가독 마스킹) - 주소 마스킹 함수 추가
# ----------------------------
def _mask_email(val: str) -> str:
    return "[이메일]"

def _mask_phone(val: str) -> str:
    return "[전화번호]"

def _mask_credit_card_preserve_seps(val: str) -> str:
    return "[신용카드번호]"

def _mask_rrn(val: str) -> str:
    return "[주민등록번호]"

def _mask_passport(val: str) -> str:
    return "[여권번호]"

def _mask_ip(val: str) -> str:
    return "[IP주소]"

def _mask_generic(val: str) -> str:
    return "[개인정보]"

def _mask_driver_license(val: str) -> str:
    return "[운전면허번호]"

def _mask_foreign_rrn(val: str) -> str:
    return "[외국인등록번호]"

def _mask_business_reg(val: str) -> str:
    return "[사업자등록번호]"

def _mask_health_insurance(val: str) -> str:
    return "[건강보험번호]"

def _mask_tax_info(val: str) -> str:
    return "[세금정보]"

def _mask_address(val: str) -> str:
    return "[주소]"

def _mask_military_id(_: str) -> str: return "[군번]"

def _mask_pii_map(pii_map: Dict[str, List[str]]) -> Dict[str, List[str]]:
    masked: Dict[str, List[str]] = {}
    for k, arr in (pii_map or {}).items():
        out = []
        for v in arr:
            if k == "email":
                out.append(_mask_email(v))
            elif k == "phone":
                out.append(_mask_phone(v))
            elif k == "credit_card":
                out.append(_mask_credit_card_preserve_seps(v))
            elif k == "credit_card_candidate":
                out.append(_mask_generic(v))
            elif k == "kr_rrn":
                out.append(_mask_rrn(v))
            elif k == "passport":
                out.append(_mask_passport(v))
            elif k == "ip":
                out.append(_mask_ip(v))
            elif k == "driver_license":
                out.append(_mask_driver_license(v))
            elif k == "foreign_rrn":
                out.append(_mask_foreign_rrn(v))
            elif k == "business_reg":
                out.append(_mask_business_reg(v))
            elif k == "health_insurance":
                out.append(_mask_health_insurance(v))
            elif k in ("tax_info", "ein"):
                out.append(_mask_tax_info(v))
            elif k == "address":  
                out.append(_mask_address(v))
            elif k == "military_id":
                out.append(_mask_military_id(v))
            else:
                out.append(_mask_generic(v))
        masked[k] = out
    return masked

def _full_redact_text_selected(text: str, patterns: List[re.Pattern]) -> str:
    """우선순위 기반 스팬 치환으로 겹침 방지"""
    if not isinstance(text, str) or not text:
        return text

    pattern_masks = {
        _QuickPII.EMAIL: "[이메일]",
        _QuickPII.KR_MOBILE: "[전화번호]",
        _QuickPII.KR_PHONE: "[전화번호]",
        _QuickPII.KR_RRN: "[주민등록번호]",
        _QuickPII.PASSPORT_KR: "[여권번호]",
        _QuickPII.IP_ADDR: "[IP주소]",
        _QuickPII.CARD_CAND: "[신용카드번호]",
        _QuickPII.DRIVER_LICENSE_KR: "[운전면허번호]",
        _QuickPII.FOREIGN_RRN: "[외국인등록번호]",
        _QuickPII.BUSINESS_REG: "[사업자등록번호]",
        _QuickPII.HEALTH_INSURANCE: "[건강보험번호]",
        _QuickPII.TAX_TIN: "[세금정보]",
        _QuickPII.EIN: "[세금정보]",
        _QuickPII.MILITARY_ID: "[군번]",
        _QuickPII.KR_ADDRESS_ROAD: "[주소]",
        _QuickPII.KR_ADDRESS_POSTAL: "[주소]",
        _QuickPII.KR_ADDRESS_SIMPLE: "[주소]",
        _QuickPII.KR_ADDRESS_BUILDING: "[주소]",
    }

    spans: List[Tuple[int, int, str]] = []

    def overlaps(s: int, e: int) -> bool:
        for ps, pe, _ in spans:
            if not (e <= ps or s >= pe):
                return True
        return False

    for pat in patterns:
        mask_text = pattern_masks.get(pat, "[개인정보]")
        for m in pat.finditer(text):
            s, e = m.span()
            if overlaps(s, e):
                continue
            spans.append((s, e, mask_text))

    if not spans:
        return text

    spans.sort(key=lambda x: x[0])
    out = []
    last = 0
    for s, e, mask in spans:
        out.append(text[last:s])
        out.append(mask)
        last = e
    out.append(text[last:])
    return "".join(out)

# ----------------------------
# OpenSearch 인덱싱 
# ----------------------------
# def _index_opensearch(self, doc: Dict[str, Any], doc_id: Optional[str] = None) -> bool:
#     v = self.valves
#     if not getattr(v, "os_enabled", False):
#         return False

#     base = f"{v.os_url.rstrip('/')}/{v.os_index}/_doc"
#     url = f"{base}/{doc_id}?op_type=create" if doc_id else base

#     payload = json.dumps(doc, ensure_ascii=False).encode("utf-8")

#     # 1) requests 경로
#     try:
#         import requests
#         auth = (v.os_user, v.os_pass) if getattr(v, "os_user", None) else None
#         verify = (not getattr(v, "os_insecure", False))
#         method = requests.put if doc_id else requests.post
#         r = method(url, data=payload,
#                    headers={"Content-Type": "application/json"},
#                    auth=auth, verify=verify, timeout=v.os_timeout)
#         if r.status_code in (200, 201, 409):
#             return True
#         return False
#     except Exception:
#         pass

#     # 2) urllib 폴백
#     try:
#         from urllib.request import Request, urlopen
#         from urllib.error import HTTPError
#         headers = {"Content-Type": "application/json"}
#         if getattr(v, "os_user", None):
#             token = base64.b64encode(f"{v.os_user}:{v.os_pass or ''}".encode()).decode()
#             headers["Authorization"] = f"Basic {token}"

#         req = Request(url, data=payload, headers=headers, method=("PUT" if doc_id else "POST"))
#         ctx = None
#         if url.startswith("https://") and getattr(v, "os_insecure", False):
#             ctx = ssl._create_unverified_context()

#         with urlopen(req, timeout=v.os_timeout, context=ctx) as resp:
#             status = getattr(resp, "status", 200)
#             return status in (200, 201)
#     except Exception:
#         return False

# ----------------------------
# 파이프라인
# ----------------------------
class Pipeline(PipelineBase):
    
    def __init__(self):
        
        '''
        '''
        
        super().__init__()
        
        self.type = "filter"
        self.id = "regex_filter"
        self.name = "Regex Filter"
        
        class Valves(BaseModel):
            # 공통
            pipelines: List[str] = Field(default_factory=lambda: ["*"])
            priority: int = 2
            enabled: bool = True

            # 차단 판단 관련(관리자 선택 가능)
            block_on_match: bool = True

            # Preflight(PII 기본 탐지)
            preflight_enabled: bool = True
            preflight_luhn: bool = True

            # ── PII 타입 토글 ──
            enable_email: bool = True
            enable_phone: bool = True
            enable_ip: bool = True
            enable_kr_rrn: bool = True
            enable_passport: bool = True
            enable_credit_card: bool = True
            enable_credit_card_candidate: bool = True
            enable_driver_license: bool = True
            enable_foreign_rrn: bool = True
            enable_business_reg: bool = True
            enable_health_insurance: bool = True
            enable_tax_info: bool = True
            enable_address: bool = True  
            enable_military_id: bool = True  # 군번

            # ── 정책 드롭다운 ──
            on_block_policy: str = Field("masking", description="PII 감지 시 처리 정책 ", json_schema_extra={"enum": ["masking", "block", "allow"]},)
            
            on_mask_response_mode: str = Field("redact", description="UI에 보여줄 안내문 형식", json_schema_extra={"enum": ["redact", "message_only", "message_with_mask_info"]},)
            
            blocked_placeholder: str = "[BLOCKED BY REGEX FILTER]"

            # ── BLOCK 시 에러 메시지 옵션 ──
            block_error_show_reason: bool = False
            block_error_show_types: bool = False
            block_error_show_ids: bool = False

            # 사용자 안내문(가이드 문구)
            block_error_guide: str = (
                "개인정보(이메일/전화번호/주소/계좌 등)가 감지되었습니다. 대답은 하지말고 차단메세지를 1줄로 만들어주세요"
            )

            # 마스킹 안내문 구성
            mask_info_include_types: bool = True
            mask_info_include_samples: bool = True
            mask_info_header: str = "개인정보유출 경고메세지를 맨앞에 추가해주세요"
            mask_info_type_label: str = "탐지 유형"
            mask_info_sample_label: str = "마스킹"

            # ── OpenSearch ──
            os_enabled: bool = True
            os_url: str = "https://vax-opensearch:9200"
            os_index: str = "regex_filter"
            os_user: Optional[str] = "admin"
            os_pass: Optional[str] = "Sniper123!@#"
            os_insecure: bool = True
            os_timeout: int = 3

            # ── 추가 패턴 ──
            extra_patterns: List[Union[str, Any]] = Field(default_factory=list, description="Custom regex patterns (str or compiled)")

            force_korean_on_masking: bool = True
            korean_system_prompt: str = "다음 지침을 엄격히 따르세요: 모든 답변은 한국어로만 작성하세요. 영어/혼용 금지."

            redact_downstream_on_pii: bool = True
        
        #TODO: 리펙토링 필요. 우선 유지
        self.Valves = Valves
        self.valves = Valves()
        self._scanner: Optional[SimpleRegexScanner] = None                
        pass
        
    ############################################## public
    
    async def on_startup(self):
        self._scanner = SimpleRegexScanner()
        if self.valves.extra_patterns:
            for pat in self.valves.extra_patterns:
                try:
                    if isinstance(pat, str):
                        compiled = re.compile(pat, re.I)
                        self._scanner.add_pattern(compiled)
                    else:
                        self._scanner.add_pattern(pat)
                except AttributeError:
                    pass

    async def on_shutdown(self):
        self._scanner = None

    async def on_valves_updated(self):
        pass
   
    
    async def inlet(self, body: Dict[str, Any], user: Optional[dict] = None, dictExtParameter:dict = None, dictOuputResponse:dict = None, __request__: Optional[Request] = None) -> Dict[str, Any]:
        
        '''
        '''
        
        if not self.valves.enabled:
            return body
        
        # /chat/completion 예외 처리
        if dictOuputResponse is None:
            dictOuputResponse = {} 
            
        #기본적인 응답 처리, action필드를 기본값으로 설정, TODO: 공통화
        dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_ALLOW

        # --- 1) 입력 추출 ---
        messages = body.get("messages") or []
        
        if not messages:
            # return body
            raise Exception(f"invalid messages format, id = {self.id}, message = {messages}")
            
        last:dict = messages[-1]
        content = last.get("content")

        #TODO: 이 코드는 넘어간다.
        if not isinstance(content, str):
            
            try:
                content = str(content)
            except Exception:
                raise Exception(f"invalid content format, id = {self.id}, content = {content}")

        if not content:
            raise Exception(f"invalid content format, id = {self.id}, content = {content}")

        # --- 2) Preflight: PII 탐지 ---
        preflight_map: Dict[str, List[str]] = {}
        preflight_hits: List[str] = []
        
        if self.valves.preflight_enabled:
            
            all_hits = _QuickPII.hits_with_values(
                content, check_card_luhn=self.valves.preflight_luhn
            )
            enabled = self._enabled_types()
            preflight_map = {k: v for k, v in (all_hits or {}).items() if enabled.get(k, False)}
            preflight_hits = list(preflight_map.keys())

        # --- 3) 스캐너 실행(추가 패턴) ---
        assert self._scanner is not None, "RegexScanner not initialized"
        result = await self._scanner.scan(content)

        decision = _to_upper_decision(getattr(result, "decision", None))
        
        try:
            score = float(getattr(result, "score", 0.0) or 0.0)
        except Exception:
            score = 0.0
            
        reason_text = getattr(result, "reason", "") or "pattern matched"

        # --- 4) 차단 판단 계산 ---
        should_block = False
        
        if self.valves.block_on_match and preflight_hits:
            should_block = True
        if decision in {"FLAG", "BLOCK"}:
            should_block = True
        if not self.valves.block_on_match:
            should_block = False

        # --- 5) (옵션) body 메타만 주입 (로그/인덱싱 없음) ---
        if preflight_hits:
            masked = _mask_pii_map(preflight_map)
            meta = {
                "pii_detected": True,
                "types": preflight_hits,
                "matches_masked": masked,
                "match_counts": {k: len(v) for k, v in preflight_map.items() },
                "decision": decision,
                "score": score,
                "reason": reason_text,
            }
            body.setdefault("_filters", {})[self.id] = meta
            
            #TODO: output 처리, 우선 동일하게..            
            dictOuputResponse.setdefault("_filters", {})[self.id] = meta

        # --- 6) 관리자 정책 적용: block | masking | allow ---
        final_action = "ALLOW"
        
        if preflight_hits:
            policy = (self.valves.on_block_policy or "masking").lower()
            mode = (self.valves.on_mask_response_mode or "redact").lower()
            if mode == "message":
                mode = "message_only"
            if mode == "masking_info":
                mode = "message_with_mask_info"

            body.setdefault("_filters", {}).setdefault(self.id, {})
            filt = body["_filters"][self.id]
            filt.update({
                "should_block": should_block,
                "final_policy": policy,
                "final_action": None,
            })
            
            #TODO: output 처리, 우선 동일하게..    
            dictOuputResponse["_filters"] = {
                "should_block": should_block,
                "final_policy": policy,
                "final_action": None,
            }

#            dictOuputResponse.update("_filters", {
#                "should_block": should_block,
#                "final_policy": policy,
#                "final_action": None,
#                })

            # ── UI 전용 안내문 빌드 ──
            def _build_ui_notice() -> str:
                
                if mode == "message_only":
                    return self.valves.blocked_placeholder or "[BLOCKED]"
                
                elif mode == "message_with_mask_info":
                    masked_local = _mask_pii_map(preflight_map)
                    parts = [self.valves.mask_info_header or ""]
                    
                    if self.valves.mask_info_include_types:
                        parts.append(f"{self.valves.mask_info_type_label or 'Types'}: {', '.join(preflight_hits)}")
                        
                    if self.valves.mask_info_include_samples:
                        sample_items = []
                        for t in preflight_hits:
                            vals = (masked_local.get(t) or [])
                            if vals:
                                sample_items.append(f"{t}={vals[0]}")
                                
                        if sample_items:
                            parts.append(f"{self.valves.mask_info_sample_label or 'Samples'}: {', '.join(sample_items)}")
                    return "\n".join([p for p in parts if p])
                
                else:
                    return ""

            if policy == "block" and should_block:
                
                final_action = "BLOCK_MSG"
                filt["final_action"] = final_action

                # (선택) 한국어 시스템 프롬프트 주입
                try:
                    if self.valves.force_korean_on_masking:
                        
                        msgs2 = body.get("messages") or []
                        already = any((m.get("role")=="system" and "한국어" in (m.get("content") or "")) for m in msgs2)
                        if not already:
                            msgs2.insert(0, {"role":"system","content": self.valves.korean_system_prompt or "모든 답변은 한국어로만 작성하세요."})
                            body["messages"] = msgs2
                except Exception:
                    pass

                # ★ LLM에게 안내문을 '그대로 출력'하도록 지시
                block_notice = "개인정보 유출이 감지되어 차단되었습니다. 개인정보를 제외하고 다시 시도해주세요."
                last = (body.get("messages") or [])[-1]
                # last["content"] = (
                #     "다음 문장을 사용자에게 그대로 출력하세요(추가 설명/수정/확장/사과문/이모지 금지):\n"
                #     f"{block_notice}"
                # )
                
                dictOuputResponse[ApiParameterDefine.OUT_ACTION] = PipelineFilterDefine.ACTION_BLOCK

                body["action"] = "block"
                body["should_block"] = True
                # 즉시 return 하지 않고, 아래 공통 '최종 os_doc' 저장 로직을 타게 둡니다.

            elif policy == "allow":
                
                final_action = "ALLOW"
                filt["final_action"] = final_action
                ui_notice = _build_ui_notice()
                if ui_notice:
                    filt["user_notice"] = {"mode": mode, "text": ui_notice}
                    
            else:
                # ── 마스킹 정책 ──
                if self.valves.redact_downstream_on_pii:
                    body["_original_last_message"] = {"content": content}
                    selected_patterns = self._patterns_for_selected()
                    redacted_content = _full_redact_text_selected(content, selected_patterns)
                    (body.get("messages") or [])[-1]["content"] = redacted_content
                    final_action = "MASKING_REDACT"
                    filt["final_action"] = final_action
                    
                else:
                    final_action = "ALLOW"
                    filt["final_action"] = final_action

                ui_notice = _build_ui_notice()
                
                if ui_notice:
                    filt["user_notice"] = {"mode": mode, "text": ui_notice}

        # --- 7) (한국어 강제 주입) PII 감지 시, 차단이 아닌 경우에만 시스템 프롬프트 삽입 ---
        try:
            
            if preflight_hits:
                fa = body.get("_filters", {}).get(self.id, {}).get("final_action")
                if fa and fa not in {"BLOCK_MSG"} and self.valves.force_korean_on_masking:
                    ko_prompt = self.valves.korean_system_prompt.strip() or \
                                "다음 지침을 엄격히 따르세요: 모든 답변은 한국어로만 작성하세요. 영어/혼용 금지."
                    msgs3 = body.get("messages") or []
                    already = any(
                        (m.get("role") == "system" and "한국어" in (m.get("content") or "")) for m in msgs3
                    )
                    if not already:
                        msgs3.insert(0, {"role": "system", "content": ko_prompt})
                        body["messages"] = msgs3
        except Exception:
            pass

        # --- 8) 최종 액션 정규화 및 ★최종 os_doc 저장(단 1회) ---
        try:
            fa_internal = body.get("_filters", {}).get(self.id, {}).get("final_action", final_action)

            def _fa_to_std_action(fa: str) -> str:
                fa = (fa or "").upper()
                if fa.startswith("MASK"):
                    return "masking"
                if fa.startswith("BLOCK"):
                    return "block"
                return "allow"

            std_action = _fa_to_std_action(fa_internal)
            body["action"] = std_action
            body["should_block"] = (std_action == "block")

            body["mode"] = std_action
            
            if "_filters" in body and self.id in body["_filters"]:
                body["_filters"][self.id]["mode"] = std_action
                body["_filters"][self.id]["final_action"] = fa_internal
                
            if self.valves.os_enabled:
                meta = body.get("metadata") or {}
                msg_id = meta.get("message_id")
                sess_id = meta.get("session_id")
                user_id = (user or {}).get("name") if isinstance(user, dict) else None
                user_email = (user or {}).get("email") if isinstance(user, dict) else None
                client_ip = __request__.client.host

                detection_status = "hit" if preflight_hits else "pass"

                os_doc_final = {
                    "@timestamp": _dt.utcnow().isoformat() + "Z",
                    "event":   {"id": msg_id, "type": "detect"},
                    "request": {"id": msg_id},
                    "session": {"id": sess_id},
                    # "user":    {"id": user_id},
                    "user": {"id": user_id, "email": user_email},

                    "stage":   "regex",
                    "detection": detection_status,
                    "should_block": (std_action == "block"),
                    "mode": std_action,
                    "final_action": fa_internal,
                    "src": {"ip": client_ip}

                }

                if preflight_hits:
                    masked_local = _mask_pii_map(preflight_map)
                    samples_list = []
                    
                    for t in preflight_hits:
                        vals = masked_local.get(t) or []
                        if vals:
                            samples_list.append(vals[0])
                    samples_str = ", ".join(samples_list) if samples_list else None

                    os_doc_final["pii"] = {
                        "types": preflight_hits,
                        "samples": samples_str,
                        "confidence": 1.0
                    }

                doc_id = f"{msg_id}:final" if msg_id else None
                
                
                # _index_opensearch(self, os_doc_final, doc_id=doc_id)
                self.AddLogData(LOG_INDEX_DEFINE.KEY_REGEX_FILTER, os_doc_final)
                

        except Exception:
            LOG().error(traceback.format_exc())
            pass

        return body
    
    
    ###################################### protected

    def _enabled_types(self) -> Dict[str, bool]:
        v = self.valves
        return {
            "email": v.enable_email,
            "phone": v.enable_phone,
            "ip": v.enable_ip,
            "kr_rrn": v.enable_kr_rrn,
            "passport": v.enable_passport,
            "credit_card": v.enable_credit_card,
            "credit_card_candidate": v.enable_credit_card_candidate,
            "driver_license": v.enable_driver_license,
            "foreign_rrn": v.enable_foreign_rrn,
            "business_reg": v.enable_business_reg,
            "health_insurance": v.enable_health_insurance,
            "tax_info": v.enable_tax_info,
            "address": v.enable_address, 
        }

    def _patterns_for_selected(self) -> List[re.Pattern]:
        v = self.valves
        pats: List[re.Pattern] = []
        flags = self._enabled_types()
        if flags["email"]: pats.append(_QuickPII.EMAIL)
        if flags["phone"]: pats.extend([_QuickPII.KR_MOBILE, _QuickPII.KR_PHONE])
        if flags["kr_rrn"]: pats.append(_QuickPII.KR_RRN)
        if flags["passport"]: pats.append(_QuickPII.PASSPORT_KR)
        if flags["ip"]: pats.append(_QuickPII.IP_ADDR)
        if flags["credit_card"] or flags["credit_card_candidate"]:
            pats.append(_QuickPII.CARD_CAND)
        if flags.get("driver_license"): pats.append(_QuickPII.DRIVER_LICENSE_KR)
        if flags.get("foreign_rrn"): pats.append(_QuickPII.FOREIGN_RRN)
        if flags.get("business_reg"): pats.append(_QuickPII.BUSINESS_REG)
        if flags.get("health_insurance"): pats.append(_QuickPII.HEALTH_INSURANCE)
        if flags.get("tax_info"):
            pats.append(_QuickPII.TAX_TIN)
            pats.append(_QuickPII.EIN)
        if flags.get("address"):
            pats.extend([
                _QuickPII.KR_ADDRESS_ROAD,
                _QuickPII.KR_ADDRESS_POSTAL,
                _QuickPII.KR_ADDRESS_SIMPLE,
                _QuickPII.KR_ADDRESS_BUILDING
            ])
            
        # 2) 가장 넓게 걸리는 '신용카드 후보'는 맨 마지막
        if flags["credit_card"] or flags["credit_card_candidate"]:
            pats.append(_QuickPII.CARD_CAND)
            
        return pats

    
    
