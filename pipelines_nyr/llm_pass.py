from typing import Dict, Any, Optional, List, Tuple
from pydantic import BaseModel, Field
from datetime import datetime
import re, json, time, os
import base64, urllib.request, ssl
import requests, urllib3
import unicodedata
from dataclasses import dataclass


# ----- LLM 차단 예외(선택적 사용) -----
class LLMBlockedError(RuntimeError):
    """LLM 정책 차단 시 발생(옵션에 따라 raise)"""
    pass


# ===== 0. 라인별 분석 결과 =====
@dataclass
class LineAnalysis:
    line: int
    risk: str  # "safe" or "risky"
    original: str
    reasons: List[str]
    confidence: float


# ===== 1. LLM 문맥 판정 =====
class LLMContextFilter:
    def __init__(self, model: str, base_url: str = "http://vax-ollama:11434", timeout: int = 180, log_file: Optional[str] = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.log_file = log_file

    # ---------- 문자열 정리/추출 유틸 ----------
    def _strip_code_fences(self, s: str) -> str:
        s = s.strip()
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE | re.DOTALL)
        return s.strip()

    def _strip_all_fences_and_labels(self, s: str) -> str:
        s = re.sub(r"```.*?```", "", s, flags=re.DOTALL)
        s = re.sub(r"(?im)^\s*json\s*:\s*", "", s)
        s = re.sub(r"(?i)\bjson\s*:\s*", "", s)
        return s.strip()

    def _extract_last_json_array(self, s: str) -> str:
        """JSON 배열 추출"""
        txt = self._strip_code_fences(s)
        # 배열 찾기
        start_idx = txt.find('[')
        if start_idx == -1:
            return txt
        
        depth = 0
        in_string = False
        esc = False
        
        for i in range(start_idx, len(txt)):
            c = txt[i]
            if in_string:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c == '[':
                    depth += 1
                elif c == ']':
                    depth -= 1
                    if depth == 0:
                        return txt[start_idx:i+1]
        return txt[start_idx:]

    # ---------- JSON 파싱 보강 ----------
    def _safe_loads_array(self, s: str) -> List[Dict]:
        """JSON 배열 파싱"""
        import json as _json

        if not s or not s.strip():
            raise _json.JSONDecodeError("Empty response", s, 0)

        txt = self._strip_all_fences_and_labels(s)
        txt = txt.replace("\ufeff", "")

        # 기본 파싱 시도
        try:
            result = _json.loads(self._strip_code_fences(txt))
            if isinstance(result, list):
                return result
        except _json.JSONDecodeError:
            pass

        # 배열 추출 후 파싱
        try:
            arr = self._extract_last_json_array(txt)
            result = _json.loads(arr)
            if isinstance(result, list):
                return result
        except _json.JSONDecodeError:
            pass

        # 문자 정리 후 재시도
        cleaned = re.sub(r",\s*([}\]])", r"\1", txt)
        cleaned = cleaned.replace(""", '"').replace(""", '"').replace("'", "'")
        cleaned = self._strip_code_fences(cleaned)
        
        try:
            arr = self._extract_last_json_array(cleaned)
            result = _json.loads(arr)
            if isinstance(result, list):
                return result
        except _json.JSONDecodeError:
            pass

        raise _json.JSONDecodeError("Could not parse JSON array", s, 0)

    def _build_prompt(self, text: str) -> str:
        """LLM의 추론 능력을 활용한 라인별 탐지 프롬프트"""
        return (
            "당신은 개인정보 유출 방지 보안 전문가입니다.\n"
            "사용자가 입력한 텍스트를 한 줄씩 검사하여 각 라인에 개인정보가 포함되었는지 판단하세요.\n\n"
            
            "탐지 대상:\n"
            "- 전화번호\n"
            "- 이메일 주소\n"
            "- 계좌번호, 카드번호\n"
            "- 주민등록번호\n"
            "- 정확한 주소\n"
            "- 실명과 식별정보 조합\n\n"
            
            "우회 기법 탐지:\n"
            "사용자는 탐지를 피하기 위해 다양한 방법을 사용합니다.\n"
            "- 공백이나 특수문자 삽입\n"
            "- 한글 숫자 표기\n"
            "- 영어 숫자 표기\n"
            "- 언어 혼용\n"
            "- 단어 치환\n"
            "- 정보 분할\n"
            "- 인코딩\n\n"
            
            "판단 기준:\n"
            "1. 해당 라인의 텍스트를 재조합하면 개인정보가 되는가\n"
            "2. 비정상적인 표기 방식을 사용했는가\n"
            "3. 이전 라인이나 다음 라인과 합치면 개인정보가 완성되는가\n"
            "4. 불확실하면 위험하다고 판단하세요\n\n"
            
            "분석 방법:\n"
            "- 각 라인을 독립적으로 검사\n"
            "- 앞뒤 라인의 맥락 고려\n"
            "- 의도적인 우회 시도인지 추론\n"
            "- 새로운 우회 기법도 논리적으로 탐지\n\n"
            
            "출력 형식:\n"
            "JSON 배열로 각 라인의 결과를 반환하세요.\n"
            "[\n"
            "  {\n"
            "    \"line\": 1,\n"
            "    \"risk\": \"safe\",\n"
            "    \"original\": \"원본 라인 텍스트\",\n"
            "    \"reasons\": [],\n"
            "    \"confidence\": 1.0\n"
            "  },\n"
            "  {\n"
            "    \"line\": 2,\n"
            "    \"risk\": \"risky\",\n"
            "    \"original\": \"원본 라인 텍스트\",\n"
            "    \"reasons\": [\"발견한 이유를 자유롭게 작성\"],\n"
            "    \"confidence\": 0.95\n"
            "  }\n"
            "]\n\n"
            
            "reasons 작성 지침:\n"
            "- 왜 이 라인이 위험한지 구체적으로 설명\n"
            "- 발견한 우회 기법이나 패턴을 자유롭게 명명\n"
            "- 여러 이유가 있으면 모두 나열\n\n"
            
            "중요:\n"
            "- JSON 배열만 출력하세요\n"
            "- 모든 라인을 빠짐없이 분석\n"
            "- 라인 번호는 1부터 시작\n"
            "- original 필드에 해당 라인의 원본 텍스트를 그대로 포함\n"
            "- risk가 risky인 라인은 차단 대상입니다\n\n"
            
            "입력 텍스트:\n"
            f"{text}\n\n"
            
            "분석 결과:"
        )

    # ---------- 요청/파싱 ----------
    def classify(self, text: str) -> Dict:
        """텍스트를 라인별로 분석"""
        import requests
        url = f"{self.base_url}/api/generate"

        payload = {
            "model": self.model,
            "prompt": self._build_prompt(text),
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0, "num_ctx": 16384},
        }

        def _log_raw(tag: str, raw: str):
            if not self.log_file:
                return
            try:
                os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
                with open(self.log_file + ".raw", "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "ts": datetime.utcnow().isoformat(),
                        "tag": tag,
                        "raw": raw
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass

        try:
            r = requests.post(url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            raw = (data.get("response") or "").strip()
            _log_raw("line_analysis", raw)

            if not raw:
                raise RuntimeError("empty_response")

            # JSON 배열 파싱
            try:
                line_results = self._safe_loads_array(raw)
            except json.JSONDecodeError as e:
                # 파싱 실패 시 전체를 risky로
                lines = text.split('\n')
                line_results = [
                    {
                        "line": i+1,
                        "risk": "risky",
                        "original": line,
                        "reasons": ["json_parse_error"],
                        "confidence": 0.5
                    }
                    for i, line in enumerate(lines)
                ]

            # 전체 판정: 하나라도 risky면 block
            has_risky = any(lr.get("risk") == "risky" for lr in line_results)
            overall_decision = "block" if has_risky else "allow"

            # risky 라인들의 reasons 수집
            all_reasons = []
            risky_lines = []
            for lr in line_results:
                if lr.get("risk") == "risky":
                    all_reasons.extend(lr.get("reasons", []))
                    risky_lines.append(lr.get("line"))

            return {
                "decision": overall_decision,
                "reasons": sorted(set(all_reasons)),
                "line_analysis": line_results,
                "risky_lines": risky_lines,  # 추가: risky 라인 번호 목록
            }

        except requests.exceptions.Timeout:
            lines = text.split('\n')
            return {
                "decision": "block",
                "reasons": ["llm_timeout"],
                "line_analysis": [
                    {
                        "line": i+1,
                        "risk": "risky",
                        "original": line,
                        "reasons": ["llm_timeout"],
                        "confidence": 0.0
                    }
                    for i, line in enumerate(lines)
                ],
                "risky_lines": list(range(1, len(lines) + 1)),
            }
        except Exception as e:
            lines = text.split('\n')
            return {
                "decision": "block",
                "reasons": ["llm_error"],
                "line_analysis": [
                    {
                        "line": i+1,
                        "risk": "risky",
                        "original": line,
                        "reasons": ["llm_error"],
                        "confidence": 0.0
                    }
                    for i, line in enumerate(lines)
                ],
                "risky_lines": list(range(1, len(lines) + 1)),
            }


# ===== 2. LLM 가드 =====
class PIIGuardLLM:
    def __init__(self, model: str, base_url: str, log_file: Optional[str] = None):
        self.llm = LLMContextFilter(model=model, base_url=base_url, timeout=180, log_file=log_file)
        self.log_file = log_file

    def _log(self, entry: Dict):
        if not self.log_file:
            return
        try:
            os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def process(self, text: str) -> Dict:
        """텍스트를 라인별로 분석하고 결과 반환"""
        t0 = time.time()
        llm_out = self.llm.classify(text)
        action = "ALLOW" if llm_out["decision"] == "allow" else "BLOCK"
        
        result = {
            "stage": "llm",
            "action": action,
            "reasons": sorted(set(llm_out.get("reasons", []))),
            "line_analysis": llm_out.get("line_analysis", []),
            "risky_lines": llm_out.get("risky_lines", []),
            "original_text": text,
            "latency_ms": int((time.time() - t0) * 1000),
        }
        
        self._log({"ts": datetime.utcnow().isoformat(), "input": text, "result": result})
        return result

    def mask_risky_lines(self, text: str, line_analysis: List[Dict], mask_placeholder: str = "[MASKED]") -> str:
        """risky 라인만 마스킹 처리"""
        lines = text.split('\n')
        masked_lines = []
        
        for i, line in enumerate(lines):
            line_num = i + 1
            # 해당 라인의 분석 결과 찾기
            analysis = next((la for la in line_analysis if la.get("line") == line_num), None)
            
            if analysis and analysis.get("risk") == "risky":
                # risky 라인은 마스킹
                masked_lines.append(mask_placeholder)
            else:
                # safe 라인은 원본 유지
                masked_lines.append(line)
        
        return '\n'.join(masked_lines)


# ===== 3. OpenWebUI 필터 플러그인 클래스 =====
class Pipeline:
    def __init__(self):
        self.type = "filter"
        self.id = "llm_filter"
        self.name = "llm filter"

        class Valves(BaseModel):
            pipelines: List[str] = Field(default_factory=lambda: ["*"])
            enabled: bool = True
            priority: int = 3

            # ===== LLM Block/Masking Options =====
            llm_on_block_policy: str = Field(
                "masking",
                description="LLM 판정 BLOCK 시 처리 정책",
                json_schema_extra={"enum": ["block", "masking", "allow"]},
            )
            llm_blocked_placeholder: str = "[BLOCKED BY LLM POLICY]"
            llm_mask_placeholder: str = "[MASKED]"  # 추가: 라인 마스킹용
            llm_block_raise: bool = False

            # ── BLOCK 메시지 구성 옵션  ──
            llm_block_guide: str = (
                "요청이 정책에 의해 제한되었습니다."
            )
            llm_block_show_reasons: bool = True
            llm_block_reason_label: str = "차단 사유"
            llm_block_show_ids: bool = False
            llm_block_ids_label: str = "참고"

            # ── 마스킹 안내 메시지 옵션 ──
            llm_mask_guide_enabled: bool = True
            llm_mask_guide_text: str = "개인정보가 포함된 라인을 자동으로 마스킹했습니다."

            force_korean_on_llm_block_mask: bool = True
            korean_system_prompt: str = "다음 지침을 엄격히 따르세요: 모든 답변은 한국어로만 작성하세요."

            # ===== LLM 설정 =====
            model: str = "Gemma3:1b"
            base_url: str = "http://vax-ollama:11434"
            log_file: Optional[str] = None

            # 결과 주입 옵션
            annotate_result: bool = True
            keep_original_copy: bool = True  # 원본 보관

            # ===== OpenSearch =====
            os_enabled: bool = True
            os_url: str = "https://vax-opensearch:9200"
            os_index: str = "llm_filter"
            os_user: Optional[str] = "admin"
            os_pass: Optional[str] = "Sniper123!@#"
            os_insecure: bool = True
            os_timeout: int = 3
            os_idempotent_final: bool = True

        self.Valves = Valves
        self.valves = Valves()
        self._build_guard()

    def _build_guard(self):
        self.guard = PIIGuardLLM(
            model=self.valves.model,
            base_url=self.valves.base_url,
            log_file=self.valves.log_file,
        )

    def _index_opensearch(self, doc: Dict[str, Any], doc_id: Optional[str] = None) -> bool:
        """OpenSearch에 문서 저장"""
        v = self.valves
        if not getattr(v, "os_enabled", False):
            return False

        base = f"{v.os_url.rstrip('/')}/{v.os_index}/_doc"
        url = f"{base}/{doc_id}" if doc_id else base

        def _default(o):
            from datetime import datetime as _dt
            return o.isoformat() + "Z" if isinstance(o, _dt) else str(o)

        payload = json.dumps(doc, ensure_ascii=False, default=_default).encode("utf-8")

        try:
            auth = (v.os_user, v.os_pass) if getattr(v, "os_user", None) else None
            verify = (not getattr(v, "os_insecure", False))
            timeout = getattr(v, "os_timeout", 5)
            method = "PUT" if doc_id else "POST"
            r = requests.request(method, url, data=payload,
                                 headers={"Content-Type": "application/json"},
                                 auth=auth, verify=verify, timeout=timeout)
            if r.status_code not in (200, 201):
                print(f"[{self.id}] OS status={r.status_code} body={r.text[:400]}")
                return False
            return True
        except Exception as e:
            print(f"[{self.id}] OpenSearch index error: {e}")
            return False

    async def on_startup(self):
        print(f"[{self.id}] on_startup")

    async def on_shutdown(self):
        print(f"[{self.id}] on_shutdown")

    async def on_valves_updated(self):
        self._build_guard()
        print(f"[{self.id}] valves updated")

    async def inlet(self, body: Dict[str, Any], user: Optional[dict] = None) -> Dict[str, Any]:
        if not self.valves.enabled:
            return body

        msgs = body.get("messages") or []
        if not msgs:
            return body
        last = msgs[-1]
        content = last.get("content")
        if not isinstance(content, str) or not content:
            return body

        # LLM 판정 (라인별 분석)
        result = self.guard.process(content)

        # === 이전 필터에서 block 여부 확인 ===
        upstream_block = False
        upstream_filter_id = None

        try:
            for fid, meta in (body.get("_filters") or {}).items():
                if not isinstance(meta, dict):
                    continue
                prev_action = (
                    meta.get("final_action")
                    or meta.get("action")
                    or meta.get("action_raw")
                    or ""
                ).strip().lower()
                prev_should_block = bool(meta.get("should_block") is True)

                is_blockish = prev_action.startswith("block")
                if prev_should_block and is_blockish:
                    upstream_block = True
                    upstream_filter_id = fid
                    break

            if not upstream_block:
                if (body.get("action") or "").strip().lower() == "block" and bool(body.get("should_block") is True):
                    upstream_block = True
                    upstream_filter_id = upstream_filter_id or "top_level"
        except Exception:
            upstream_block = False

        # 기본 메타(주입)
        if self.valves.annotate_result:
            body.setdefault("_filters", {})[self.id] = {
                "action_raw": result.get("action", "ALLOW"),
                "final_action": "allow",
                "stage": result.get("stage", "llm"),
                "latency_ms": result.get("latency_ms"),
                "reasons": result.get("reasons", []),
                "risky_lines": result.get("risky_lines", []),
                "line_analysis": result.get("line_analysis", []),
                "llm_policy": getattr(self.valves, "llm_on_block_policy", "masking"),
                "masked": False,
                "should_block": False,
                "skipped_due_to_prior_block": upstream_block,
                "skipped_by_filter": upstream_filter_id if upstream_block else None,
                "mode": "allow",
            }

        # 원본 백업
        if self.valves.keep_original_copy and "_original_last_message" not in body:
            body["_original_last_message"] = {"content": content}

        # ===== 정책 적용 =====
        v = self.valves
        masked = False
        final_action_internal = "allow"

        if not upstream_block:
            if result.get("action") == "BLOCK":
                policy = (v.llm_on_block_policy or "masking").lower()

                # 공통 정보
                meta = body.get("metadata") or {}
                msg_id = meta.get("message_id")
                sess_id = meta.get("session_id")
                user_id = (user or {}).get("name") if isinstance(user, dict) else None

                if policy == "block":
                    # 전체 차단
                    reasons = sorted(set(result.get("reasons") or []))
                    parts = []
                    guide_part = (v.llm_block_guide or "").strip() or "요청이 정책에 의해 제한되었습니다."
                    parts.append(guide_part)
                    
                    if v.llm_block_show_reasons and reasons:
                        label = v.llm_block_reason_label or "차단 사유"
                        parts.append(f"{label}: {', '.join(reasons)}")

                    if v.llm_block_show_ids:
                        ids = []
                        if msg_id: ids.append(f"message_id={msg_id}")
                        if sess_id: ids.append(f"session_id={sess_id}")
                        if user_id: ids.append(f"user_id={user_id}")
                        if ids:
                            label = v.llm_block_ids_label or "참고"
                            parts.append(f"{label}: " + ", ".join(ids))

                    block_msg = "\n\n".join(parts).strip() or (v.llm_blocked_placeholder or "[BLOCKED BY LLM POLICY]")
                    last["content"] = block_msg
                    final_action_internal = "block"

                elif policy == "masking":
                    # 라인별 마스킹
                    line_analysis = result.get("line_analysis", [])
                    masked_content = self.guard.mask_risky_lines(
                        content, 
                        line_analysis, 
                        v.llm_mask_placeholder or "[MASKED]"
                    )
                    
                    # 안내 메시지 추가
                    parts = []
                    if getattr(v, "llm_mask_guide_enabled", False):
                        guide = (v.llm_mask_guide_text or "").strip()
                        if guide:
                            parts.append(guide)
                    
                    risky_lines = result.get("risky_lines", [])
                    if risky_lines:
                        parts.append(f"마스킹된 라인: {', '.join(map(str, risky_lines))}")
                    
                    # 안내 메시지 + 마스킹된 내용
                    if parts:
                        last["content"] = "\n".join(parts) + "\n\n" + masked_content
                    else:
                        last["content"] = masked_content

                    masked = True
                    final_action_internal = "masking"

                else:  # allow
                    final_action_internal = "allow"
            else:
                final_action_internal = "allow"
        else:
            # 업스트림 차단이면 패스
            final_action_internal = "allow"

        # 메타 업데이트
        if self.valves.annotate_result:
            body["_filters"][self.id].update({
                "final_action": final_action_internal,
                "masked": masked,
                "should_block": bool(upstream_block or (final_action_internal in ("block", "masking"))),
                "mode": final_action_internal,
            })

        body["action"] = final_action_internal
        body["mode"] = final_action_internal
        body["should_block"] = bool(upstream_block or (final_action_internal in ("block", "masking")))

        # OpenSearch 저장
        try:
            if self.valves.os_enabled:
                meta = body.get("metadata") or {}
                msg_id = meta.get("message_id")
                sess_id = meta.get("session_id")
                user_id = (user or {}).get("name") if isinstance(user, dict) else None

                detection_status = "hit" if result.get("action") == "BLOCK" else "pass"
                should_block_final = bool(upstream_block or (final_action_internal in ("block", "masking")))

                reasons_list = result.get("reasons", [])
                risky_lines = result.get("risky_lines", [])

                os_doc = {
                    "@timestamp": datetime.utcnow().isoformat() + "Z",
                    "event": {"id": msg_id, "type": "detect"},
                    "request": {"id": msg_id},
                    "session": {"id": sess_id},
                    "user": {"id": user_id},
                    "stage": "llm_line_by_line",
                    "detection": detection_status,
                    "should_block": should_block_final,
                    "mode": final_action_internal,
                    "final_action": final_action_internal,
                }

                if detection_status == "hit":
                    os_doc["pii"] = {
                        "types": reasons_list,
                        "risky_lines": risky_lines,
                        "confidence": 1.0
                    }

                if getattr(self.valves, "os_idempotent_final", True):
                    stable_id = f"{sess_id or 'sess'}::{msg_id or 'msg'}::llm_line::detect::{self.id}"
                    self._index_opensearch(os_doc, doc_id=stable_id)
                else:
                    self._index_opensearch(os_doc)
        except Exception as e:
            print(f"[{self.id}] OpenSearch error: {e}")

        # 한국어 강제 프롬프트
        try:
            if final_action_internal == "masking" and self.valves.force_korean_on_llm_block_mask:
                msgs2 = body.get("messages") or []
                ko_prompt = (self.valves.korean_system_prompt or "").strip()
                if ko_prompt:
                    already = any(
                        (m.get("role") == "system" and "한국어" in (m.get("content") or ""))
                        for m in msgs2
                    )
                    if not already:
                        msgs2.insert(0, {"role": "system", "content": ko_prompt})
                        body["messages"] = msgs2
        except Exception:
            pass

        return body