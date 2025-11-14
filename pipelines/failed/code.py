import re
import math
from typing import List, Tuple, Dict

MASK = "[MASKING]"

# ------------------------
# 1) 멀티라인/블록 패턴
# ------------------------
RE_PEM_BLOCK = re.compile(
    r"-----BEGIN (?P<K>[^-\r\n]+?) KEY-----[\s\S]+?-----END (?P=K) KEY-----",
    re.MULTILINE,
)
RE_JWT = re.compile(r"\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")

# ------------------------
# 2) 알려진 API 키 / 토큰 정규식 목록 (값 캡처 그룹 이름: VAL 권장)
#    필요에 맞게 추가/조정 가능
# ------------------------
KNOWN_TOKEN_REGEXES: List[Tuple[str, re.Pattern]] = [
    # Google API Key
    ("google_api_key", re.compile(r"\b(?P<VAL>AIza[0-9A-Za-z_\-]{35})\b")),
    # GitHub tokens (classic/pat/ghp_)
    ("github_token", re.compile(r"\b(?P<VAL>(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,255})\b")),
    # Slack tokens
    ("slack_token", re.compile(r"\b(?P<VAL>xox[abprs]-[A-Za-z0-9-]{10,})\b")),
    # Discord bot/user tokens (간단화)
    ("discord_token", re.compile(r"\b(?P<VAL>[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27})\b")),
    # Discord Webhook pattern도 종종 문자열로 존재
    ("discord_like", re.compile(r"\b(?P<VAL>DISCORD[_-]TOKEN[_A-Za-z0-9\-]*)\b")),
    # Stripe keys
    ("stripe_key", re.compile(r"\b(?P<VAL>sk_(?:live|test)_[A-Za-z0-9]{16,})\b")),
    # Twilio API Key
    ("twilio_api", re.compile(r"\b(?P<VAL>SK[0-9a-fA-F]{32})\b")),
    # SendGrid API Key
    ("sendgrid_api", re.compile(r"\b(?P<VAL>SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{20,})\b")),
    # AWS Access Key ID
    ("aws_access_key_id", re.compile(r"\b(?P<VAL>(?:AKIA|ASIA|ANPA|ABIA)[0-9A-Z]{16})\b")),
    # AWS Secret Access Key (대략적인 40자 base64ish)
    ("aws_secret_access_key", re.compile(r"(?<![A-Za-z0-9/+=])(?P<VAL>[A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])")),
    # OpenAI-style tokens
    ("openai_like", re.compile(r"\b(?P<VAL>sk-[A-Za-z0-9]{16,})\b")),
    # Generic header/kv forms
    ("kv_quoted", re.compile(r'(?i)\b(?:api[_-]?key|x-api-key|api[_-]?token|x-api-token|auth[_-]?token|password|secret)\b\s*[:=]\s*["\'](?P<VAL>[^"\r\n]{6,})["\']')),
    ("kv_bare", re.compile(r'(?i)\b(?:api[_-]?key|x-api-key|api[_-]?token|x-api-token|auth[_-]?token|password|secret)\b\s*[:=]\s*(?P<VAL>[^\s"\'`]{8,})')),
]

# ------------------------
# 3) 고엔트로피 후보 추출 + 엔트로피 판정
# ------------------------
# 후보: 흔한 토큰 문자군 (베이스64/헥스/일반 혼합) — 최소 길이 상향으로 오탐 완화
CANDIDATE_RE = re.compile(r"[A-Za-z0-9+/=._\-]{20,}")

B64_SHAPE_RE = re.compile(r"^[A-Za-z0-9+/=]+$")
HEX_SHAPE_RE = re.compile(r"^[A-Fa-f0-9]+$")

def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: Dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())

def looks_like_base64(s: str) -> bool:
    return bool(B64_SHAPE_RE.match(s))

def looks_like_hex(s: str) -> bool:
    return bool(HEX_SHAPE_RE.match(s))

def high_entropy_hits(text: str) -> List[Tuple[int, int]]:
    """
    텍스트에서 고엔트로피 후보를 찾아 start, end 인덱스 목록 반환.
    임계치(보수적):
      - base64 shape: H >= 4.3, len >= 24
      - hex shape:    H >= 3.2, len >= 32
      - mixed:        H >= 4.0, len >= 24
    추가 필터:
      - 동일문자 반복/단일 문자군 과도 비율은 자동으로 엔트로피가 낮아져 탈락
    """
    hits: List[Tuple[int, int]] = []
    for m in CANDIDATE_RE.finditer(text):
        s = m.group(0)
        L = len(s)
        if L < 20:
            continue
        H = shannon_entropy(s)

        if looks_like_base64(s):
            if L >= 24 and H >= 4.3:
                hits.append((m.start(), m.end()))
        elif looks_like_hex(s):
            if L >= 32 and H >= 3.2:
                hits.append((m.start(), m.end()))
        else:
            if L >= 24 and H >= 4.0:
                hits.append((m.start(), m.end()))
    return hits

# ------------------------
# 4) 마스킹 유틸
# ------------------------
def add_span(spans: List[Tuple[int, int]], start: int, end: int):
    if start < end:
        spans.append((start, end))

def mask_spans(text: str, spans: List[Tuple[int, int]]) -> str:
    if not spans:
        return text
    # 겹침/중복 처리: 뒤에서 앞으로 치환
    spans = sorted(spans, key=lambda x: x[0])
    merged: List[Tuple[int, int]] = []
    for s, e in spans:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            # overlap → 확장
            prev_s, prev_e = merged[-1]
            merged[-1] = (prev_s, max(prev_e, e))
    out = []
    last = 0
    for s, e in merged:
        out.append(text[last:s])
        out.append(MASK)
        last = e
    out.append(text[last:])
    return "".join(out)

# ------------------------
# 5) 메인: 알려진 패턴 + 엔트로피 결합 탐지 & 마스킹
# ------------------------
def detect_and_mask(text: str) -> Dict[str, any]:
    """
    반환:
      {
        "masked": <치환된 텍스트>,
        "findings": [{"label": <종류>, "span": (start,end), "match": <샘플>}, ...],
        "counts": {"known": n1, "entropy": n2, "pem": n3, "jwt": n4, "total_spans": k}
      }
    """
    spans: List[Tuple[int, int]] = []
    findings: List[Dict[str, any]] = []
    known_cnt = ent_cnt = pem_cnt = jwt_cnt = 0

    # (A) PEM 블록(멀티라인 전체)
    for m in RE_PEM_BLOCK.finditer(text):
        add_span(spans, m.start(), m.end())
        findings.append({"label": "pem_key_block", "span": (m.start(), m.end()), "match": text[m.start():m.start()+30] + "..."})
        pem_cnt += 1

    # (B) JWT
    for m in RE_JWT.finditer(text):
        add_span(spans, m.start(), m.end())
        findings.append({"label": "jwt", "span": (m.start(), m.end()), "match": text[m.start():m.start()+30] + "..."})
        jwt_cnt += 1

    # (C) 알려진 API 키/토큰 패턴 (값 그룹)
    for label, pat in KNOWN_TOKEN_REGEXES:
        for m in pat.finditer(text):
            if "VAL" in m.groupdict():
                s, e = m.span("VAL")
            else:
                s, e = m.span(0)
            add_span(spans, s, e)
            sample = text[s:e]
            findings.append({"label": label, "span": (s, e), "match": sample[:30] + ("..." if len(sample) > 30 else "")})
            known_cnt += 1

    # (D) 고엔트로피 후보
    for s, e in high_entropy_hits(text):
        add_span(spans, s, e)
        sample = text[s:e]
        findings.append({"label": "high_entropy", "span": (s, e), "match": sample[:30] + ("..." if len(sample) > 30 else "")})
        ent_cnt += 1

    masked_text = mask_spans(text, spans)
    return {
        "masked": masked_text,
        "findings": findings,
        "counts": {
            "known": known_cnt,
            "entropy": ent_cnt,
            "pem": pem_cnt,
            "jwt": jwt_cnt,
            "total_spans": len(spans),
        },
    }

# ------------------------
# 사용 예시
# ------------------------
if __name__ == "__main__":
    test = '''
    api_key="AIzaSyB1234567890abcdefghijklmnopqrstu"
    token: sk-live_1234567890abcdefghijk
    x-api-key: abcd-efgh-ijkl
    SECRET=SG.asdfasdfasdfasdf.asdfasdfasdfasdfasdfasdfasdf
    AWS AKID: AKIA1234567890ABCDE
    jwt: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjMifQ.h4tWqJ8K8QmUuZ7pWn1i4g
    random: QWxhZGRpbjpvcGVuIHNlc2FtZQ==
    ---- BEGIN PRIVATE KEY BLOCK (fake) ----
    -----BEGIN PRIVATE KEY-----
    ABCDEFGHIJKLMNOP
    -----END PRIVATE KEY-----
    '''
    result = detect_and_mask(test)
    print("counts:", result["counts"])
    print("masked:\n", result["masked"])
