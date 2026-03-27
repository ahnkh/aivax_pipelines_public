import re
import math
import logging
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

MASK_DEFAULT = "[MASKING]"

class Pipeline:
    class Valves(BaseModel):
        # 공통
        pipelines: List[str] = Field(default_factory=lambda: ["*"])
        priority: int = Field(default=0)
        enabled: bool = Field(default=True)
        log_to_console: bool = Field(default=True)

        # 엔트로피 파라미터
        min_len_b64: int = Field(default=20)   # Base64 형태로 보이는 문자열을 '후보'로 인정할 최소 길이(정규화 기준)
        min_len_hex: int = Field(default=28)   # Hex(0-9a-f) 형태 문자열을 후보로 인정할 최소 길이
        min_len_mixed: int = Field(default=20) # 그 외(영숫자/기호 혼합) 형태 문자열을 후보로 인정할 최소 길이

        thr_b64: float = Field(default=4.0)    # Base64 후보로 판단할 최소 엔트로피 비트/문자 (높을수록 무작위성↑)
        thr_hex: float = Field(default=3.0)    # Hex 후보로 판단할 최소 엔트로피 임계값
        thr_mixed: float = Field(default=3.8)  # 혼합 형태 후보로 판단할 최소 엔트로피 임계값

        prefix_relax: bool = Field(default=True)  # 접두어 완화 규칙 사용 여부(예: ghp-/ak-/tk- 등으로 시작하면
                                                  # 길이/영문·숫자 혼합/엔트로피 조건을 일부 완화해 후보 채택)

        # 정규표현식 그룹 스위치
        enable_secrets: bool = Field(default=True)        # A. SaaS/API 시크릿류
        enable_pc_info: bool = Field(default=True)          # B. PC 고유/네트워크/OS (KV 라벨)
        enable_values_paths: bool = Field(default=True)   # C. 단독 값/경로
        enable_entropy: bool = Field(default=True)        # 엔트로피 보조 탐지

        # 차단/마스킹 정책
        block_on_match: bool = Field(default=True)        # 매치 발생 시 차단 판단 활성화
        on_block_policy: str = Field(                     # "masking" | "block" | "allow"
            default="masking",
            json_schema_extra={"enum": ["masking", "block", "allow"]}
        )

        # 최소 안내 메시지 (요청: block/masking일 때만 간단 안내)
        block_notice: str = Field(
            default="PC정보/시크릿 정보가 감지되어 차단되었습니다. 민감 정보를 제거하고 다시 시도해주세요."
        )
        masking_notice: str = Field(
            default="[안내] 민감 정보가 감지되어 일부 내용이 마스킹되었습니다."
        )

    def __init__(self):
        self.type = "filter"
        self.id = "secret_filter"
        self.name = "secret_filter"
        self.valves = self.Valves()
        self.toggle = True

        self.logger = logging.getLogger(self.id)

        # ─────────────────────────────────────────
        # 1) 블록/토큰 전용 패턴
        # ─────────────────────────────────────────
        self.re_pem_block = re.compile(
            r"-----BEGIN (?P<K>[^-\r\n]+?) KEY-----[\s\S]+?-----END (?P=K) KEY-----",
            re.MULTILINE,
        )
        self.re_jwt = re.compile(
            r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
        )

        sep = r"\s*[:=]\s*"

        # ─────────────────────────────────────────
        # 2) 정규표현식 그룹 (A/B/C)
        # ─────────────────────────────────────────

        # A) SaaS / API 시크릿류
        self.patterns_secrets: List[Tuple[str, re.Pattern, Optional[str]]] = [
            # AWS
            ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|ANPA|ABIA)[0-9A-Z]{16}\b"), None),
            ("aws_secret_access_key", re.compile(r"(?<![A-Za-z0-9/+=])([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])"), None),

            # Azure
            ("azure_storage_account_key", re.compile(r"(?i)\bAccountKey=(?P<VAL>[A-Za-z0-9+/=]{30,})"), "VAL"),
            ("azure_conn_string", re.compile(r"(?i)\bDefaultEndpointsProtocol=\w+;AccountName=\w+;AccountKey=(?P<VAL>[A-Za-z0-9+/=]{30,})"), "VAL"),

            # BasicAuth/Cloudant
            ("basic_auth_creds", re.compile(r"(?i)\b(?:https?|ftp|ssh)://(?P<CREDS>[^:@\s/]+:[^@\s/]+)@"), "CREDS"),
            ("cloudant_creds", re.compile(r"(?i)https?://(?P<CREDS>[^:@\s/]+:[^@\s/]+)@[^/\s]*\.cloudant\.com"), "CREDS"),

            # Discord/GitHub/Mailchimp
            ("discord_bot_token", re.compile(r"\b(?P<VAL>[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27})\b"), "VAL"),
            ("github_token", re.compile(r"\b(?P<VAL>(?:ghp|gho|ghu|ghs|ghr)[-_][A-Za-z0-9]{16,})\b"), "VAL"),
            ("mailchimp_api_key", re.compile(r"\b(?P<VAL>[0-9a-f]{32}-us\d{1,2})\b"), "VAL"),

            # Slack
            ("slack_token", re.compile(r"\b(?P<VAL>xox[abprs]-[A-Za-z0-9-]{10,})\b"), "VAL"),
            ("slack_webhook_path", re.compile(r"(?i)https://hooks\.slack\.com/services/(?P<VAL>T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+)"), "VAL"),

            # Stripe
            ("stripe_secret", re.compile(r"\b(?P<VAL>sk_(?:live|test)_[A-Za-z0-9]{16,})\b"), "VAL"),
            ("stripe_publishable", re.compile(r"\b(?P<VAL>pk_(?:live|test)_[A-Za-z0-9]{16,})\b"), "VAL"),

            # Twilio
            ("twilio_account_sid", re.compile(r"\b(?P<VAL>AC[0-9a-fA-F]{32})\b"), "VAL"),
            ("twilio_auth_token", re.compile(r"(?<![A-Za-z0-9])(?P<VAL>[0-9a-fA-F]{32})(?![A-Za-z0-9])"), "VAL"),

            # OpenAI/Custom-like
            ("openai_like", re.compile(r"\b(?P<VAL>sk-[A-Za-z0-9]{16,})\b"), "VAL"),
            ("ak_tk_token", re.compile(r"\b(?P<VAL>(?:ak|tk)-[a-f0-9]{16,}(?:-(?:dev|test)[a-z0-9]*)?)\b"), "VAL"),
        ]

        # B) PC 고유/네트워크/OS 정보 (KV 라벨 기반)
        self.patterns_pc_info: List[Tuple[str, re.Pattern, Optional[str]]] = [
            ("serial_number_info", re.compile(
                r"(?i)\b(?:sn|s\/n|serial(?:\s*(?:no\.?|number))?|시리얼\s*(?:넘버|번호)|일련\s*번호)\b"
                r"\s*(?:[:=：\-–—#]?\s*)(?P<VAL>[A-Za-z0-9._\-]{4,64})"
            ), "VAL"),
            ("asset_tag_info", re.compile(r"(?i)\b(?:asset\s*tag|자산\s*태그)\b" + sep + r"(?P<VAL>[A-Z0-9._\-]{3,64})"), "VAL"),
            ("port_info", re.compile(r"(?i)\bport\s*[:=]\s*(?P<VAL>\d{1,5})\b"), "VAL"),
            ("host_info", re.compile(r"(?i)\b(?:host(?:name)?|host\s*명|호스트\s*명)\b" + sep + r"(?P<VAL>[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)\b"), "VAL"),
            ("domain_info", re.compile(r"(?i)\b(?:domain|도메인\s*명)\b" + sep + r"(?P<VAL>(?=.{1,253}\b)(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63})"), "VAL"),
            ("vpn_addr_info", re.compile(
                r"(?i)\b(?:vpn(?:\s*주소)?)\b" + sep +
                r"(?P<VAL>("
                r"(?:(?:(?:25[0-5]|2[0-4]\d|1?\d{1,2})\.){3}(?:25[0-5]|2[0-4]\d|1?\d{1,2}))"
                r"|(?:(?=.{1,253}\b)(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63})"
                r"))"
            ), "VAL"),
            ("driver_version_info", re.compile(r"(?i)\b(?:driver|드라이버)\s*(?:ver(?:sion)?|버전)?\b" + sep + r"(?P<VAL>\d+(?:\.\d+){1,3}(?:-[\w\.-]+)?)"), "VAL"),
            ("kernel_version_info", re.compile(r"(?i)\b(?:kernel|커널)\s*(?:ver(?:sion)?|버전)?\b" + sep + r"(?P<VAL>\d+(?:\.\d+){1,3}(?:-[\w\.-]+)?)"), "VAL"),
            ("os_info_info", re.compile(r"(?i)\b(?:os|os\s*정보|운영체제)\b" + sep + r"(?P<VAL>(?:Windows|Win|Ubuntu|CentOS|RHEL|Red\s*Hat|Debian|macOS|Darwin)\s*[0-9A-Za-z._-]*)"), "VAL"),
            ("internal_path_info", re.compile(r"(?i)\b(?:내부\s*경로|path)\b" + sep + r"(?P<VAL>(?:[A-Za-z]:\\[^\r\n]+|\\\\[^\r\n\\]+\\[^\r\n]+|\/[^\s\/]+(?:\/[^\s\/]+)*))"), "VAL"),
            ("home_path_info", re.compile(r"(?i)\b(?:유저\s*홈\s*디(?:렉|텔)토리\s*경로|user\s*home\s*dir(?:ectory)?|home\s*path)\b" + sep + r"(?P<VAL>(?:C:\\Users\\[A-Za-z0-9._-]+|\/home\/[A-Za-z0-9._-]+))"), "VAL"),
            ("backup_log_path_info", re.compile(r"(?i)\b(?:백업\s*로그\s*경로|backup(?:_path)?|log(?:s|_path)?)\b" + sep + r"(?P<VAL>(?:[A-Za-z]:\\[^\r\n]+|\\\\[^\r\n\\]+\\[^\r\n]+|\/[^\s\/]+(?:\/[^\s\/]+)*))"), "VAL"),
        ]

        # C) 단독 값/경로
        self.patterns_values_paths: List[Tuple[str, re.Pattern, Optional[str]]] = [
            ("uuid_guid", re.compile(r"(?i)\b(?P<VAL>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"), "VAL"),
            ("ipv4", re.compile(r"\b(?P<VAL>(?:(?:25[0-5]|2[0-4]\d|1?\d{1,2})\.){3}(?:25[0-5]|2[0-4]\d|1?\d{1,2}))\b"), "VAL"),
            ("ipv6_any", re.compile(
                r"\b(?P<VAL>(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|"
                r"(?:[0-9A-Fa-f]{1,4}:){1,7}:|"
                r":(?:[0-9A-Fa-f]{1,4}:){1,7}|"
                r"(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|"
                r"(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|"
                r"(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|"
                r"(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|"
                r"(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|"
                r"[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}))\b"
            ), "VAL"),
            ("host_label_value", re.compile(r"\b(?=.{1,63}\b)(?=[A-Za-z0-9\-]*\d|[A-Za-z0-9\-]*-)(?P<VAL>[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)\b"), "VAL"),
            ("win_path_value", re.compile(r"(?P<VAL>\b[A-Za-z]:\\[^\r\n]+)"), "VAL"),
            ("unc_path_value", re.compile(r"(?P<VAL>\\\\[^\r\n\\]+\\[^\r\n]+)"), "VAL"),
            ("unix_path_value", re.compile(r"(?P<VAL>(?:\/[^\s\/]+){2,})"), "VAL"),
            ("mac_colon_hyphen", re.compile(r"\b(?P<VAL>(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})\b"), "VAL"),
            ("mac_cisco", re.compile(r"\b(?P<VAL>[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4})\b"), "VAL"),
        ]

        # ─────────────────────────────────────────
        # 3) 엔트로피 후보
        # ─────────────────────────────────────────
        self.re_candidate = re.compile(r"[A-Za-z0-9+/=._\-]{16,}")
        self.re_b64_shape = re.compile(r"^[A-Za-z0-9+/=]+$")
        self.re_hex_shape = re.compile(r"^[A-Fa-f0-9]+$")

    # ─────────────────────────────────────────
    # 내부 유틸
    # ─────────────────────────────────────────
    @staticmethod
    def _entropy(s: str) -> float:
        if not s:
            return 0.0
        counts: Dict[str, int] = {}
        for ch in s:
            counts[ch] = counts.get(ch, 0) + 1
        n = len(s)
        return -sum((c / n) * math.log2(c / n) for c in counts.values())

    @staticmethod
    def _normalize_for_entropy(s: str) -> str:
        s = re.sub(r"[-_](?:dev|test)[0-9]*$", "", s, flags=re.IGNORECASE)
        return re.sub(r"[^A-Za-z0-9]", "", s)

    def _looks_b64(self, s: str) -> bool:
        return bool(self.re_b64_shape.match(s))

    def _looks_hex(self, s: str) -> bool:
        return bool(self.re_hex_shape.match(s))

    def _high_entropy_hits(self, text: str) -> List[Tuple[int, int]]:
        if not self.valves.enable_entropy:
            return []
        v = self.valves
        hits: List[Tuple[int, int]] = []
        url_spans = [m.span() for m in re.finditer(r"https?://\S+", text)]

        def overlaps_url(s: int, e: int) -> bool:
            for us, ue in url_spans:
                if not (e <= us or s >= ue):
                    return True
            return False

        for m in self.re_candidate.finditer(text):
            s0, e0 = m.start(), m.end()
            if overlaps_url(s0, e0):
                continue

            raw = m.group(0)
            norm = self._normalize_for_entropy(raw)
            L = len(norm)
            if L < 12:
                continue

            H = self._entropy(norm)
            looks_b64 = self._looks_b64(re.sub(r"[^A-Za-z0-9+/=]", "", raw))
            looks_hex = self._looks_hex(norm)

            keep = False
            if looks_b64:
                keep = (L >= v.min_len_b64 and H >= v.thr_b64)
            elif looks_hex:
                keep = (L >= v.min_len_hex and H >= v.thr_hex)
            else:
                keep = (L >= v.min_len_mixed and H >= v.thr_mixed)

            if v.prefix_relax and not keep:
                low = raw.lower()
                if low.startswith(("ak-", "tk-", "ghp-", "ghp_", "gho-", "gho_", "ghu-", "ghu_", "ghs-", "ghs_", "ghr-", "ghr_")):
                    has_digit = any(c.isdigit() for c in norm)
                    has_alpha = any(c.isalpha() for c in norm)
                    if L >= 16 and has_digit and has_alpha and H >= 3.4:
                        keep = True

            if keep:
                hits.append((s0, e0))

        return hits

    def _iter_known_patterns(self):
        v = self.valves
        if v.enable_secrets:
            for item in self.patterns_secrets:
                yield item
        if v.enable_pc_info:
            for item in self.patterns_pc_info:
                yield item
        if v.enable_values_paths:
            for item in self.patterns_values_paths:
                yield item

    @staticmethod
    def _add_span(spans: List[Tuple[int, int]], start: int, end: int):
        if start < end:
            spans.append((start, end))

    def _mask_spans(self, text: str, spans: List[Tuple[int, int]]) -> str:
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
        for s, e in merged:
            out.append(text[last:s])
            out.append(MASK_DEFAULT)  # mask_char 옵션 제거: 고정 마스크 사용
            last = e
        out.append(text[last:])
        return "".join(out)

    # ─────────────────────────────────────────
    # 핵심: 탐지 + 정책 적용(block/masking/allow)
    # ─────────────────────────────────────────
    def _detect_spans(self, text: str) -> Tuple[List[Tuple[int, int]], Dict[str, int]]:
        spans: List[Tuple[int, int]] = []
        counts = {"pem": 0, "jwt": 0, "known": 0, "entropy": 0}

        # PEM 블록
        for m in self.re_pem_block.finditer(text):
            self._add_span(spans, m.start(), m.end())
            counts["pem"] += 1

        # JWT
        for m in self.re_jwt.finditer(text):
            self._add_span(spans, m.start(), m.end())
            counts["jwt"] += 1

        # Known patterns
        for _, pat, grp in self._iter_known_patterns():
            for m in pat.finditer(text):
                if grp and grp in m.groupdict():
                    s, e = m.span(grp)
                else:
                    s, e = m.span(0)
                self._add_span(spans, s, e)
                counts["known"] += 1

        # Entropy
        for s, e in self._high_entropy_hits(text):
            self._add_span(spans, s, e)
            counts["entropy"] += 1

        return spans, counts

    async def inlet(self, body: Dict[str, Any], __user__: Optional[dict] = None) -> Dict[str, Any]:
        if not self.valves.enabled:
            return body

        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            return body

        last = messages[-1]
        if last.get("role") != "user":
            return body

        content = last.get("content")
        if not isinstance(content, str) or not content.strip():
            return body

        spans, counts = self._detect_spans(content)
        has_hits = bool(spans)

        # 정책 결정
        policy = (self.valves.on_block_policy or "masking").lower()
        if policy not in ("masking", "block", "allow"):
            policy = "masking"

        should_block = self.valves.block_on_match and has_hits

        if has_hits:
            if policy == "block" and should_block:
                # 안내 메세지(간단)
                block_notice = self.valves.block_notice.strip() or "차단되었습니다."
                # 예제와 동일한 형식으로 대체 프롬프트 작성
                last["content"] = (
                    "다음 문장을 사용자에게 그대로 출력하세요(추가 설명/수정/확장/사과문/이모지 금지):\n"
                    f"{block_notice}"
                )
                body["action"] = "block"
                body["should_block"] = True
                self.logger.warning(
                    "Blocked: pem=%d, jwt=%d, known=%d, entropy=%d, total_spans=%d",
                    counts["pem"], counts["jwt"], counts["known"], counts["entropy"], len(spans)
                )
                return body

            elif policy == "masking":
                masked = self._mask_spans(content, spans)
                notice = self.valves.masking_notice.strip()
                last["content"] = (notice + "\n" + masked) if notice else masked
                self.logger.warning(
                    "Masked: pem=%d, jwt=%d, known=%d, entropy=%d, total_spans=%d",
                    counts["pem"], counts["jwt"], counts["known"], counts["entropy"], len(spans)
                )
                body["action"] = "masking"
                body["should_block"] = False
                return body

            # policy == "allow"
            self.logger.warning(
                "Detected but allowed: pem=%d, jwt=%d, known=%d, entropy=%d, total_spans=%d",
                counts["pem"], counts["jwt"], counts["known"], counts["entropy"], len(spans)
            )
            body["action"] = "allow"
            body["should_block"] = False
            return body

        # 미탐
        self.logger.info("No secrets/pc-info detected (regex+entropy).")
        body["action"] = "allow"
        body["should_block"] = False
        return body

    async def outlet(self, body: Dict[str, Any], __event_emitter__=None, __user__: Optional[dict] = None) -> Dict[str, Any]:
        return body
