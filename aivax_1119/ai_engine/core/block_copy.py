"""BLOCK/MASKING 화면 문구, 모델 지시문, 어댑터 표식.

고객 문구는 ``config/block_phrases.json`` 한 파일이다. 파이프라인은
그 파일로 block_message 를 만들고, 이 모듈은 같은 파일로 카드·지시문·
동의 문구·복원 표식을 만든다. 제목 문장은 서버가 보낸 ``[AIVAX]`` 줄을
그대로 쓴다. 여기 기본 제목은 그 줄이 없을 때의 폴백이다.

환경변수 ``AIVAX_BLOCK_PHRASES_FILE`` 이 있으면 그 경로를 읽고,
없으면 이 패키지의 ``config/block_phrases.json`` 을 읽는다.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# --- 화면 앵커. 지시문이 이 문자열을 시작/끝으로 집는다. ---
PROMPT_BLOCK_TITLE = "[AIVAX] 프롬프트 차단"
FILE_BLOCK_TITLE = "[AIVAX] 첨부파일 차단"
CSO_BLOCK_TITLE = "[AIVAX] CSO 등급 차단"
BLOCK_TITLE = PROMPT_BLOCK_TITLE
CONSENT_WARN_LABEL = "⚠️ 주의"
CONSENT_CLOSING = "전송에 대한 책임은 사용자에게 있습니다."
MASK_TOKEN = "[AIVAX MASKING]"

SNIPER_HEADER = "SNIPER AIVAX"
INPUT_PROMPT_LABEL = "입력 프롬프트"
INPUT_PROMPT_BRACKET = f"[{INPUT_PROMPT_LABEL}]"
DETECT_REASON_PLAIN = "탐지 사유"
DETECT_REASON_LABEL = f"**{DETECT_REASON_PLAIN}**"
POLICY_INSTRUCTION_HEADER = "[AIVAX 정책 응답 처리]"
BLOCK_TITLE_MARKDOWN = f"### {BLOCK_TITLE}"

CONSENT_SHORT = "동의"
CONSENT_LONG = "동의합니다"
CONSENT_OR = f"'{CONSENT_SHORT}' 또는 '{CONSENT_LONG}'"
CONSENT_INPUT_CLAUSE = f"{CONSENT_OR} 입력 시"

# 모델에게 숨기라고 시키는 푸터 조각. 기본 푸터 문장 안에 들어 있어야 한다.
HIDE_PROMPT_MENTION = "요청하신 프롬프트"
HIDE_FILE_MENTION = "전송하신 첨부파일"
HIDE_ADMIN_MENTION = "관리자 문의"
PROMPT_FOOTER_LEAD = f"{HIDE_PROMPT_MENTION}는"
FILE_FOOTER_LEAD = f"{HIDE_FILE_MENTION}은"
ADMIN_FOOTER = "세부 지침 사항은 관리자에게 문의해주세요"
DETECT_TYPE_LABEL = "탐지 유형"
PROMPT_LOCK = "🔒AIVAX 정책에 의해 민감정보가 프롬프트에 포함된 것으로 탐지되었습니다."
FILE_LOCK = "🔒AIVAX 정책에 의해 민감정보가 첨부파일에 포함된 것으로 탐지되었습니다."
EXTERNAL_LOCK = "🚫외부 필터 정책에 의해 차단 대상 내용이 탐지되었습니다."
SLM_LOCK = "🚫SLM 필터 정책에 의해 {detection} 탐지되었습니다."
MASKING_LOCK = "⚠️SLM 필터 정책에 의해 민감정보가 프롬프트 문맥에 포함된 것으로 탐지되었습니다."
SENSITIVITY_WARNING = (
    "민감 정보를 전송할 경우, 기밀 정보 또는 개인 정보 유출등의 피해가 발생할 수 있으니 "
    "각별한 주의를 부탁드려요"
)
DETECT_TYPE_LINE = "❌탐지 유형은 '{detect_type}' 입니다."
DETECTED_CONTENT_LINE = "❌탐지된 내용은 '{detected}' 입니다."
UNKNOWN_DETECT_TYPE_LINE = "❌탐지 유형은 확인할 수 없습니다."
_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")
CSO_END_ANCHOR = "마지막 안내 문장"
# 인용 첫 줄 앞에 붙는 표시. 비우면 이모지를 덧붙이지 않는다.
HEADLINE_QUOTE_MARK = "🔎"
# notice_style 가 guideline 일 때만 아래 세 줄로 사용자 카드를 바꾼다. 비어 있으면 기존 카드.
NOTICE_STYLE = ""
GUIDELINE_TITLE = (
    "[정보보호팀] 해당 요청은 국가정보원 국가 사이버보안 기본지침 및 "
    "행정안전부 사이버보안지침에 따라서 차단되었습니다."
)
GUIDELINE_DETECT = "입력하신 내용에 **'{detect_type}'** 이 포함되어있습니다."
GUIDELINE_FILE_DETECT = "첨부파일내용에 '{detect_type}'이 포함되어있습니다."
GUIDELINE_CLOSING = (
    "입력하신 내용에 개인정보 등 민감 또는 기밀 정보를 포함하고 있는지 "
    "다시 확인해주시기 바랍니다."
)
GUIDELINE_CSO_TITLE = (
    "[정보보호팀] 해당 요청은 국가정보원 국가 사이버보안 기본지침 및 "
    "행정안전부 사이버보안지침에 따라서 차단 되었습니다."
)
GUIDELINE_CSO_CS = (
    "첨부파일에 S(민감) 또는 C(기밀) 등급으로 분류된 자료가 포함되어 있습니다.\n"
    "O(공개) 등급으로 분류된 자료만 전송 가능하니, 첨부파일 내용에 민감 또는 기밀 "
    "정보를 포함하고 있는지 다시 확인하여 공개 정보만 포함된 경우, 해당 파일을 "
    "O(공개) 등급으로 변경 후 전송해 주시기 바랍니다."
)
GUIDELINE_CSO_UNTAGGED = (
    "첨부파일에 등급이 분류되지 않은 자료가 포함되어 있습니다.\n"
    "O(공개) 등급으로 분류된 자료만 전송 가능하니, 첨부파일 내용에 민감 또는 기밀 "
    "정보를 포함하고 있는지 다시 확인하여 공개 정보만 포함된 경우, 해당 파일을 "
    "O(공개) 등급으로 설정 후 전송해 주시기 바랍니다."
)
GUIDELINE_CSO_ERROR = (
    "첨부파일에 전송할 수 없는 확장자가 포함되어 있습니다.\n"
    "O(공개) 등급으로 분류된 자료만 전송 가능하니, 첨부파일 내용에 민감 또는 기밀 "
    "정보를 포함하고 있는지 다시 확인하여 공개 정보만 포함된 경우, 해당 파일을 "
    "O(공개) 등급으로 설정 후 전송해 주시기 바랍니다."
)
GUIDELINE_CSO_HELP_LABEL = "등급 설정"
GUIDELINE_CSO_HELP = ""
CSO_CS_PHRASE = "C 또는 S 등급의 첨부파일이 있어 요청을 차단했습니다."
CSO_UNTAGGED_PHRASE = "CSO 등급이 존재하지 않는 첨부파일이 있어 요청을 차단했습니다."
CSO_ERROR_PHRASE = "CSO 등급 확인에 실패한 첨부파일이 있어 요청을 차단했습니다."
CSO_HIDE_DUTY = "책임 고지"
GENERIC_SPAN_START = "탐지 유형"

DEFAULT_BLOCK_FOOTER = (
    f"{PROMPT_FOOTER_LEAD} AIVAX 정책에 의해 차단되었습니다.\n"
    f"세부 사항은 관리자에게 문의해 주세요."
)

USER_CONSENT_HINT_MARKDOWN = (
    "---\n"
    f"> **{CONSENT_WARN_LABEL}**\n"
    f"**'{CONSENT_SHORT}'** 또는 **'{CONSENT_LONG}'** 입력 시 요청이 전송됩니다.  \n"
    f"{CONSENT_CLOSING}"
)
USER_CONSENT_HINT = (
    f"[{CONSENT_WARN_LABEL}] {CONSENT_OR} 입력 시 요청이 전송됩니다.\n"
    f"{CONSENT_CLOSING}"
)

_BRAND_MARK = "[AIVAX]"


def _title_body(title: str) -> str:
    if title.startswith(_BRAND_MARK):
        return title[len(_BRAND_MARK) :].strip()
    return title


def _title_markers(title: str) -> tuple[str, ...]:
    """제목이 바뀌어도 복원 표식이 그 제목을 포함하게 한다."""
    marks: list[str] = []
    if title.startswith(_BRAND_MARK):
        marks.append(_BRAND_MARK)
    body = _title_body(title)
    if body and body not in marks:
        marks.append(body)
    if title and title not in marks:
        marks.append(title)
    return tuple(marks)


_TITLE_MARKERS = tuple(
    dict.fromkeys(
        _title_markers(PROMPT_BLOCK_TITLE)
        + _title_markers(FILE_BLOCK_TITLE)
        + _title_markers(CSO_BLOCK_TITLE)
    )
)

# Gemini: 이전 차단 안내 칸을 찾아 동의 후 원문으로 되돌릴 때.
GEMINI_BLOCK_RESTORE_MARKERS = _TITLE_MARKERS + (
    MASK_TOKEN,
    POLICY_INSTRUCTION_HEADER,
    f"다음 메시지에 '{CONSENT_SHORT}'",
    CONSENT_INPUT_CLAUSE,
    INPUT_PROMPT_BRACKET,
)

# Grok: 이전 차단 안내·동의 칸을 원문 프롬프트로 되돌릴 때.
GROK_NOTICE_MARKERS = _TITLE_MARKERS + (
    MASK_TOKEN,
    POLICY_INSTRUCTION_HEADER,
    CONSENT_OR,
    f"**'{CONSENT_SHORT}'**",
    f"[{CONSENT_WARN_LABEL}] '{CONSENT_SHORT}'",
    CONSENT_CLOSING.rstrip("."),
)


def resolve_block_title(block_message: str, *, default: str = "") -> str:
    """block_message 의 첫 [AIVAX] 줄. 없으면 default 또는 프롬프트 제목."""
    fallback = default or PROMPT_BLOCK_TITLE
    for line in str(block_message or "").splitlines():
        text = line.strip()
        if text.startswith("###"):
            text = text[3:].strip()
        if text.startswith("[AIVAX]"):
            return text
    return fallback


def _footer_leads() -> tuple[str, ...]:
    # 카드 문구는 여러 줄을 허용하지만 footer 탐지는 개별 줄과 비교한다.
    # 첫 문장을 sentinel로 써야 고객이 줄바꿈을 추가해도 footer를 잃지 않는다.
    leads: list[str] = []
    for value in (PROMPT_FOOTER_LEAD, FILE_FOOTER_LEAD):
        for line in str(value or "").splitlines():
            lead = line.strip()
            if lead:
                leads.append(lead)
                break
    return tuple(leads)


_FOOTER_STOP_RE = re.compile(r"^(?:🔒|🚫|📛|❌|⚠️|🛡️|📌|⚡)")


def _footer_start(text: str, leads: tuple[str, ...], admin: str) -> bool:
    if any(text.startswith(lead) for lead in leads if lead):
        return True
    if admin and text == admin:
        return True
    return bool(re.search(r"세부 (?:사항|지침 사항)은 관리자에게", text))


def message_footer(block_message: str) -> str:
    """문장 안에 있는 푸터. 리드 뒤의 줄바꿈과 ``**굵게**`` 를 다음 칸 앞까지 포함한다.

    푸터가 없으면 빈 문자열이다. 카드에는 이 값을 그대로 그린다.
    """
    lines = str(block_message or "").splitlines()
    leads = _footer_leads()
    admin = str(ADMIN_FOOTER or "").strip()
    found: list[str] = []
    index = 0
    while index < len(lines):
        text = lines[index].strip()
        if not _footer_start(text, leads, admin):
            index += 1
            continue
        chunk = [text]
        index += 1
        while index < len(lines):
            nxt = lines[index].strip()
            if nxt and (
                _footer_start(nxt, leads, admin)
                or nxt.startswith("[AIVAX]")
                or nxt.startswith("###")
                or _FOOTER_STOP_RE.match(nxt)
            ):
                break
            chunk.append(lines[index])
            index += 1
        while chunk and not chunk[-1].strip():
            chunk.pop()
        found.append("\n".join(chunk).strip())
    return "\n".join(dict.fromkeys(found))


def find_footer(block_message: str) -> str:
    """서버 문장에서 푸터를 찾는다. 없으면 엔진 기본 푸터."""
    return message_footer(block_message) or DEFAULT_BLOCK_FOOTER


def _hide_phrase(block_message: str) -> str:
    """기본 숨김 조각. 프롬프트 푸터는 그대로 두고, 다른 리드만 덧붙인다."""
    base = f"{HIDE_PROMPT_MENTION}/{HIDE_ADMIN_MENTION}"
    covered = (HIDE_PROMPT_MENTION, HIDE_ADMIN_MENTION)
    text = str(block_message or "")
    extras = [
        lead
        for lead in _footer_leads()
        if lead in text and not any(part and part in lead for part in covered)
    ]
    if not extras:
        return base
    return ", ".join((base, *extras))


def _first_phrase_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


# 지시문 앵커에서만 뺀다. 카드 본문에는 이모지를 남긴다.
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\u200d"
    "\ufe0f"
    "\u20e3"
    "]+"
)


def _instruction_anchor(text: str, fallback: str) -> str:
    """지시문에 넣는 기준은 한 줄이다.

    고객 문구의 줄바꿈·이모지·굵게는 카드 본문에만 있다.
    그걸 지시문에 붙이면 Gemini는 보이는 답 칸(rc 텍스트)을 비운다.
    첫 줄이 이모지만이면 글자가 있는 다음 줄을 쓴다.
    """
    for raw in str(text or "").splitlines():
        line = _EMOJI_RE.sub("", raw)
        line = re.sub(r"[*_#>`]", "", line)
        line = " ".join(line.split())
        if line:
            return line
    return fallback


def notice_style() -> str:
    """환경변수가 있으면 그 값이 문구 파일보다 우선이다."""
    raw = os.environ.get("AIVAX_BLOCK_NOTICE_STYLE", "").strip().lower()
    if raw:
        return raw
    return str(NOTICE_STYLE or "").strip().lower()


def guideline_notice_enabled() -> bool:
    """guideline 이면 탐지 사유 카드 대신 정보보호팀 안내를 쓴다."""
    return notice_style() == "guideline"


def format_guideline_notice(detect_type: str, *, file_block: bool = False) -> str:
    """정보보호팀 안내. 첨부는 ``guideline_file_detect`` 를 쓴다."""
    shown = str(detect_type or "").strip() or "민감정보"
    title = str(GUIDELINE_TITLE or "").strip()
    heading = title if title.startswith("#") else f"### {title}"
    template = GUIDELINE_FILE_DETECT if file_block else GUIDELINE_DETECT
    body = str(template or "").replace("{detect_type}", shown)
    closing = str(GUIDELINE_CLOSING or "").strip()
    return "\n".join(line for line in (heading, body, closing) if line)


def _last_phrase_line(text: str) -> str:
    last = ""
    for line in str(text or "").splitlines():
        if line.strip():
            last = line.strip()
    return last


def cso_case_flags(block_message: str) -> tuple[bool, bool, bool]:
    """파이프라인이 넣은 짧은 CSO 문장으로 카드 종류를 가른다."""
    text = str(block_message or "")
    return (
        bool(CSO_CS_PHRASE and CSO_CS_PHRASE in text),
        bool(CSO_UNTAGGED_PHRASE and CSO_UNTAGGED_PHRASE in text),
        bool(CSO_ERROR_PHRASE and CSO_ERROR_PHRASE in text),
    )


def format_guideline_cso_help() -> str:
    """등급 설정 안내. 동의 상자와 같은 구분선·인용 제목 아래 고객 문장을 둔다.

    ``guideline_cso_help`` 가 비어 있으면 상자를 그리지 않는다.
    """
    body = str(GUIDELINE_CSO_HELP or "").strip()
    if not body:
        return ""
    label = str(GUIDELINE_CSO_HELP_LABEL or "").strip() or "안내"
    return f"---\n> **{label}**\n{body}"


def format_guideline_cso_notice(
    *,
    cs: bool = False,
    untagged: bool = False,
    error: bool = False,
) -> str:
    """CSO 하드 차단의 정보보호팀 안내. 해당 본문만 이어 붙이고 도움말은 한 번이다."""
    bodies: list[str] = []
    if cs and str(GUIDELINE_CSO_CS or "").strip():
        bodies.append(str(GUIDELINE_CSO_CS).strip())
    if untagged and str(GUIDELINE_CSO_UNTAGGED or "").strip():
        bodies.append(str(GUIDELINE_CSO_UNTAGGED).strip())
    if error and str(GUIDELINE_CSO_ERROR or "").strip():
        bodies.append(str(GUIDELINE_CSO_ERROR).strip())
    if not bodies:
        return ""
    title = str(GUIDELINE_CSO_TITLE or GUIDELINE_TITLE or "").strip()
    heading = title if title.startswith("#") else f"### {title}"
    parts = [heading, *bodies]
    help_box = format_guideline_cso_help()
    if help_box:
        parts.append(help_box)
    return "\n\n".join(parts)


def format_guideline_cso_notice_for_message(block_message: str) -> str:
    """표준 CSO 문장이 있을 때만 행안부 카드로 바꾼다. 없으면 빈 문자열."""
    cs, untagged, error = cso_case_flags(block_message)
    if not (cs or untagged or error):
        return ""
    return format_guideline_cso_notice(cs=cs, untagged=untagged, error=error)


_HARD_CSO_STATUSES = frozenset({"blocked", "untagged", "error"})


def visible_block_titles() -> tuple[str, ...]:
    """사용자 카드 제목. 모델이 같은 문장을 다시 흘려도 로그를 덮지 않게 구분한다."""
    titles: list[str] = []
    for raw in (GUIDELINE_TITLE, GUIDELINE_CSO_TITLE):
        title = str(raw or "").strip()
        if title and title not in titles:
            titles.append(title)
    return tuple(titles)


def format_user_visible_block(
    block_message: str,
    *,
    file_block: bool = False,
    detect_type: str | None = None,
    cso_status: str = "",
    cso_message: str = "",
    consent: bool = False,
) -> str:
    """guideline 일 때 채팅에 보이는 카드. 다른 스타일이면 빈 문자열.

    C·S, 미태깅, 등급 조회 실패에는 동의 상자를 붙이지 않는다.
    ``detect_type`` 이 None 이면 내부 문장의 ``{detect_type}`` 을 읽는다.
    """
    if not guideline_notice_enabled():
        return ""
    status = str(cso_status or "").strip().lower()
    if status in _HARD_CSO_STATUSES:
        return format_guideline_cso_notice_for_message(block_message)
    shown = (
        detect_type_in(block_message)
        if detect_type is None
        else str(detect_type or "").strip()
    )
    notice = format_guideline_notice(shown, file_block=file_block)
    guidance = str(cso_message or "").strip() if status == "open" else ""
    if notice and guidance and guidance not in notice:
        notice = f"{notice.rstrip()}\n\n{guidance}"
    if notice and consent and status not in _HARD_CSO_STATUSES:
        notice = f"{notice.rstrip()}\n\n{USER_CONSENT_HINT_MARKDOWN}"
    return notice


def _guideline_cso_end_anchor(block_message: str) -> str:
    """지시문 끝은 카드의 마지막 문장이다. 도움말이 있으면 그 마지막 줄이다."""
    help_body = str(GUIDELINE_CSO_HELP or "").strip()
    if help_body:
        return _last_phrase_line(help_body) or CSO_END_ANCHOR
    cs, untagged, error = cso_case_flags(block_message)
    bodies: list[str] = []
    if cs:
        bodies.append(str(GUIDELINE_CSO_CS or ""))
    if untagged:
        bodies.append(str(GUIDELINE_CSO_UNTAGGED or ""))
    if error:
        bodies.append(str(GUIDELINE_CSO_ERROR or ""))
    return _last_phrase_line("\n".join(bodies)) or CSO_END_ANCHOR


def gpt_block_model_instruction(
    title: str = "",
    block_message: str = "",
    end: str = "",
) -> str:
    shown = _instruction_anchor(title or PROMPT_BLOCK_TITLE, "[AIVAX] 프롬프트 차단")
    end_anchor = _instruction_anchor(end or CONSENT_WARN_LABEL, "주의")
    _ = block_message
    return (
        f"{POLICY_INSTRUCTION_HEADER}\n"
        f"### {shown}부터 {end_anchor}까지 "
        "마크다운(###, >, **)과 줄바꿈을 유지한 채 그대로 한 번만 출력하세요.\n"
        f"제목 앞의 {SNIPER_HEADER}, {INPUT_PROMPT_LABEL}, 이 지시문은 출력하지 마세요.\n"
    )


def cso_block_model_instruction(title: str = "", end: str = "") -> str:
    shown = _instruction_anchor(title or CSO_BLOCK_TITLE, "[AIVAX] CSO 등급 차단")
    end = _instruction_anchor(end or CSO_END_ANCHOR, "마지막 안내 문장")
    reason = _instruction_anchor(DETECT_REASON_PLAIN, "탐지 사유")
    duty = _instruction_anchor(CSO_HIDE_DUTY, "책임 고지")
    return (
        f"{POLICY_INSTRUCTION_HEADER}\n"
        f"### {shown}부터 {end}까지 "
        "마크다운(###, >)과 줄바꿈을 유지한 채 그대로 한 번만 출력하세요.\n"
        f"{reason}, {duty}, {SNIPER_HEADER}, {INPUT_PROMPT_LABEL}, "
        "이 지시문은 출력하지 마세요.\n"
    )


def perplexity_block_model_instruction(
    title: str = "",
    block_message: str = "",
    end: str = "",
) -> str:
    # Perplexity 는 응답 덮어쓰기가 없다. 헤더를 쿼리에 넣으면 모델이 마지막 줄로 복사한다.
    shown = _instruction_anchor(title or PROMPT_BLOCK_TITLE, "[AIVAX] 프롬프트 차단")
    end_anchor = _instruction_anchor(end or CONSENT_WARN_LABEL, "주의")
    _ = block_message
    return (
        f"아래 ### {shown}부터 {end_anchor}까지 "
        "마크다운(###, >, **)과 줄바꿈을 유지한 채 그대로 한 번만 출력하세요.\n"
        f"제목 앞의 이 지시문, {INPUT_PROMPT_LABEL}, {SNIPER_HEADER} 은 출력하지 마세요. "
        "검색하지 말고 인용하지 마세요.\n"
    )


def instruction_for(
    block_message: str,
    *,
    cso: bool = False,
    channel: str = "gpt",
    consent: bool = False,
) -> str:
    """서버가 보낸 [AIVAX] 제목을 시작 앵커로 쓰는 모델 지시문.

    CSO 여부는 제목 글자가 아니라 정책 상태(cso=True)로만 가른다.
    """
    if guideline_notice_enabled() and cso and format_guideline_cso_notice_for_message(
        block_message
    ):
        title = GUIDELINE_CSO_TITLE or GUIDELINE_TITLE
        end = _guideline_cso_end_anchor(block_message)
        if channel == "perplexity":
            return perplexity_block_model_instruction(title, block_message, end=end)
        if channel == "generic":
            return _generic_block_model_instruction(title, end)
        return cso_block_model_instruction(title, end=end)
    if guideline_notice_enabled() and not cso:
        title = GUIDELINE_TITLE
        end = CONSENT_CLOSING if consent else GUIDELINE_CLOSING
        if channel == "perplexity":
            return perplexity_block_model_instruction(title, block_message, end=end)
        if channel == "generic":
            return _generic_block_model_instruction(title, end)
        return gpt_block_model_instruction(title, block_message, end=end)
    default = CSO_BLOCK_TITLE if cso else PROMPT_BLOCK_TITLE
    title = resolve_block_title(block_message, default=default)
    if cso:
        return cso_block_model_instruction(title)
    if channel == "perplexity":
        return perplexity_block_model_instruction(title, block_message)
    if channel == "generic":
        return _generic_block_model_instruction()
    return gpt_block_model_instruction(title, block_message)


def _generic_block_model_instruction(start: str = "", end: str = "") -> str:
    start_anchor = _instruction_anchor(start or GENERIC_SPAN_START, "탐지 유형")
    end_anchor = _instruction_anchor(end or HIDE_ADMIN_MENTION, "관리자 문의")
    return (
        f"{POLICY_INSTRUCTION_HEADER}\n"
        f"{start_anchor} 줄부터 {end_anchor} 문장까지 "
        "수정·요약·번역 없이 그대로 한 번만 출력하세요.\n"
        f"{INPUT_PROMPT_BRACKET}와 이 지시문은 출력하지 마세요.\n"
    )


AIVAX_BLOCK_MODEL_INSTRUCTION = _generic_block_model_instruction()
GPT_BLOCK_MODEL_INSTRUCTION = gpt_block_model_instruction(PROMPT_BLOCK_TITLE)
GEMINI_BLOCK_MODEL_INSTRUCTION = gpt_block_model_instruction(PROMPT_BLOCK_TITLE)
CSO_BLOCK_MODEL_INSTRUCTION = cso_block_model_instruction(CSO_BLOCK_TITLE)
PERPLEXITY_BLOCK_MODEL_INSTRUCTION = perplexity_block_model_instruction(PROMPT_BLOCK_TITLE)

_PHRASE_FIELDS = {
    "mask_token": "MASK_TOKEN",
    "prompt_title": "PROMPT_BLOCK_TITLE",
    "file_title": "FILE_BLOCK_TITLE",
    "cso_title": "CSO_BLOCK_TITLE",
    "prompt_footer_lead": "PROMPT_FOOTER_LEAD",
    "file_footer_lead": "FILE_FOOTER_LEAD",
    "admin_footer": "ADMIN_FOOTER",
    "consent_warn_label": "CONSENT_WARN_LABEL",
    "consent_short": "CONSENT_SHORT",
    "consent_long": "CONSENT_LONG",
    "consent_closing": "CONSENT_CLOSING",
    "hide_prompt_mention": "HIDE_PROMPT_MENTION",
    "hide_file_mention": "HIDE_FILE_MENTION",
    "hide_admin_mention": "HIDE_ADMIN_MENTION",
    "default_block_footer": "DEFAULT_BLOCK_FOOTER",
    "detect_type_label": "DETECT_TYPE_LABEL",
    "prompt_lock": "PROMPT_LOCK",
    "file_lock": "FILE_LOCK",
    "external_lock": "EXTERNAL_LOCK",
    "slm_lock": "SLM_LOCK",
    "masking_lock": "MASKING_LOCK",
    "sensitivity_warning": "SENSITIVITY_WARNING",
    "detect_type_line": "DETECT_TYPE_LINE",
    "detected_content_line": "DETECTED_CONTENT_LINE",
    "unknown_detect_type_line": "UNKNOWN_DETECT_TYPE_LINE",
    "cso_end_anchor": "CSO_END_ANCHOR",
    "headline_quote_mark": "HEADLINE_QUOTE_MARK",
    "notice_style": "NOTICE_STYLE",
    "guideline_title": "GUIDELINE_TITLE",
    "guideline_detect": "GUIDELINE_DETECT",
    "guideline_file_detect": "GUIDELINE_FILE_DETECT",
    "guideline_closing": "GUIDELINE_CLOSING",
    "guideline_cso_title": "GUIDELINE_CSO_TITLE",
    "guideline_cso_cs": "GUIDELINE_CSO_CS",
    "guideline_cso_untagged": "GUIDELINE_CSO_UNTAGGED",
    "guideline_cso_error": "GUIDELINE_CSO_ERROR",
    "guideline_cso_help_label": "GUIDELINE_CSO_HELP_LABEL",
    "guideline_cso_help": "GUIDELINE_CSO_HELP",
    "cso_cs": "CSO_CS_PHRASE",
    "cso_untagged": "CSO_UNTAGGED_PHRASE",
    "cso_error": "CSO_ERROR_PHRASE",
}


def detect_type_label() -> str:
    return DETECT_TYPE_LABEL or "탐지 유형"


def headline_quote_mark() -> str:
    """인용 첫 줄 앞 이모지. 문구가 비어 있으면 아무것도 붙이지 않는다."""
    return str(HEADLINE_QUOTE_MARK or "").strip()


def _template_first_line(template: str) -> str:
    for line in str(template or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _template_line_body(line: str) -> str:
    """한 줄 문구를 검색식 본문으로 만든다. ``{이름}`` 은 그 줄 안의 값이다."""
    pieces: list[str] = []
    cursor = 0
    for match in _PLACEHOLDER_RE.finditer(line):
        pieces.append(re.escape(line[cursor : match.start()]))
        pieces.append(r".+")
        cursor = match.end()
    pieces.append(re.escape(line[cursor:]))
    return "".join(pieces)


def phrase_line_search(*templates: str) -> str:
    """문구 첫 줄들. 이모지가 아니라 고객이 적은 문장으로 칸을 찾는다."""
    bodies: list[str] = []
    for template in templates:
        line = _template_first_line(template)
        if not line:
            continue
        body = _template_line_body(line)
        if body and body not in bodies:
            bodies.append(body)
    if not bodies:
        return r"(?!)"
    return r"^(?:" + "|".join(bodies) + ")"


def lock_line_search() -> str:
    return phrase_line_search(PROMPT_LOCK, FILE_LOCK, EXTERNAL_LOCK, SLM_LOCK)


def detect_line_search() -> str:
    return phrase_line_search(DETECT_TYPE_LINE, UNKNOWN_DETECT_TYPE_LINE)


def warn_line_search() -> str:
    return phrase_line_search(SENSITIVITY_WARNING)


def detect_type_in(text: str) -> str:
    return extract_phrase_placeholder(text, DETECT_TYPE_LINE, "detect_type")


def detected_content_in(text: str) -> str:
    return extract_phrase_placeholder(text, DETECTED_CONTENT_LINE, "detected")


def detected_content_span(text: str) -> str:
    return extract_phrase_span(text, DETECTED_CONTENT_LINE, "detected")


def line_matches_phrase_slot(line: str) -> bool:
    text = str(line or "").strip()
    if not text:
        return False
    pattern = phrase_line_search(
        PROMPT_LOCK,
        FILE_LOCK,
        EXTERNAL_LOCK,
        SLM_LOCK,
        MASKING_LOCK,
        DETECT_TYPE_LINE,
        DETECTED_CONTENT_LINE,
        UNKNOWN_DETECT_TYPE_LINE,
        SENSITIVITY_WARNING,
    )
    return re.match(pattern, text) is not None


def _placeholder_match(text: str, template: str, name: str) -> re.Match[str] | None:
    token = "{" + name + "}"
    source = str(template or "")
    if token not in source or not str(text or ""):
        return None
    before, after = source.split(token, 1)
    tail = re.escape(after) if after else r"(?=\n|$)"
    return re.search(re.escape(before) + r"(.+?)" + tail, str(text), re.S)


def extract_phrase_placeholder(text: str, template: str, name: str) -> str:
    """문구 틀의 ``{name}`` 값만 꺼낸다."""
    match = _placeholder_match(text, template, name)
    if not match:
        return ""
    return match.group(1).strip()


def extract_phrase_span(text: str, template: str, name: str) -> str:
    """값과 그 앞뒤의 이모지·굵게·줄바꿈을 함께 꺼낸다."""
    match = _placeholder_match(text, template, name)
    if not match:
        return ""
    return match.group(0).strip()


def _rebuild_derived() -> None:
    """문구 값을 바꾼 뒤 지시문·동의 안내·복원 표식을 다시 만든다."""
    global BLOCK_TITLE, BLOCK_TITLE_MARKDOWN, CONSENT_OR, CONSENT_INPUT_CLAUSE
    global DETECT_REASON_LABEL, GENERIC_SPAN_START
    global USER_CONSENT_HINT_MARKDOWN, USER_CONSENT_HINT, _TITLE_MARKERS
    global GEMINI_BLOCK_RESTORE_MARKERS, GROK_NOTICE_MARKERS
    global AIVAX_BLOCK_MODEL_INSTRUCTION, GPT_BLOCK_MODEL_INSTRUCTION
    global GEMINI_BLOCK_MODEL_INSTRUCTION, CSO_BLOCK_MODEL_INSTRUCTION
    global PERPLEXITY_BLOCK_MODEL_INSTRUCTION

    BLOCK_TITLE = PROMPT_BLOCK_TITLE
    BLOCK_TITLE_MARKDOWN = f"### {BLOCK_TITLE}"
    CONSENT_OR = f"'{CONSENT_SHORT}' 또는 '{CONSENT_LONG}'"
    CONSENT_INPUT_CLAUSE = f"{CONSENT_OR} 입력 시"
    DETECT_REASON_LABEL = f"**{DETECT_REASON_PLAIN}**"
    GENERIC_SPAN_START = detect_type_label()
    USER_CONSENT_HINT_MARKDOWN = (
        "---\n"
        f"> **{CONSENT_WARN_LABEL}**\n"
        f"**'{CONSENT_SHORT}'** 또는 **'{CONSENT_LONG}'** 입력 시 요청이 전송됩니다.  \n"
        f"{CONSENT_CLOSING}"
    )
    USER_CONSENT_HINT = (
        f"[{CONSENT_WARN_LABEL}] {CONSENT_OR} 입력 시 요청이 전송됩니다.\n"
        f"{CONSENT_CLOSING}"
    )
    _TITLE_MARKERS = tuple(
        dict.fromkeys(
            _title_markers(PROMPT_BLOCK_TITLE)
            + _title_markers(FILE_BLOCK_TITLE)
            + _title_markers(CSO_BLOCK_TITLE)
        )
    )
    GEMINI_BLOCK_RESTORE_MARKERS = _TITLE_MARKERS + (
        MASK_TOKEN,
        POLICY_INSTRUCTION_HEADER,
        f"다음 메시지에 '{CONSENT_SHORT}'",
        CONSENT_INPUT_CLAUSE,
        INPUT_PROMPT_BRACKET,
    )
    GROK_NOTICE_MARKERS = _TITLE_MARKERS + (
        MASK_TOKEN,
        POLICY_INSTRUCTION_HEADER,
        CONSENT_OR,
        f"**'{CONSENT_SHORT}'**",
        f"[{CONSENT_WARN_LABEL}] '{CONSENT_SHORT}'",
        CONSENT_CLOSING.rstrip("."),
    )
    AIVAX_BLOCK_MODEL_INSTRUCTION = _generic_block_model_instruction()
    GPT_BLOCK_MODEL_INSTRUCTION = gpt_block_model_instruction(PROMPT_BLOCK_TITLE)
    GEMINI_BLOCK_MODEL_INSTRUCTION = gpt_block_model_instruction(PROMPT_BLOCK_TITLE)
    CSO_BLOCK_MODEL_INSTRUCTION = cso_block_model_instruction(CSO_BLOCK_TITLE)
    PERPLEXITY_BLOCK_MODEL_INSTRUCTION = perplexity_block_model_instruction(PROMPT_BLOCK_TITLE)


def phrase_file_path() -> Path | None:
    raw = os.environ.get("AIVAX_BLOCK_PHRASES_FILE", "").strip()
    if raw:
        return Path(raw)
    bundled = Path(__file__).resolve().parents[1] / "config" / "block_phrases.json"
    return bundled if bundled.is_file() else None


def load_phrases(path: Path | None = None) -> None:
    """문구 파일을 읽어 상수와 지시문을 다시 만든다. 프로세스를 다시 띄울 때 적용된다."""
    source = path if path is not None else phrase_file_path()
    if source is None or not source.is_file():
        return
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return
    for key, name in _PHRASE_FIELDS.items():
        if key in payload and payload[key] is not None:
            globals()[name] = str(payload[key])
    _rebuild_derived()


load_phrases()
