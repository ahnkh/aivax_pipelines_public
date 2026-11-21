"""차단 로그의 output_text 를 채팅 카드와 같게 만든다.

OpenSearch 필드 이름은 바꾸지 않는다. API 가 엔진에 주는 block_message 도
내부 문장 그대로 둔다. 행안부 카드와 기본 카드 모두 엔진이 화면에 만드는
함수로 한 번만 조립한다.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from src.domain.inspection_input.models.block_phrases import get_block_phrases
from src.domain.inspection_input.models.cso_grade_result import summarize_cso_grades
from src.domain.inspection_input.models.input_analysis_request import InputInspectorType
from src.domain.inspection_input.models.input_analysis_result import (
    InputAnalysisResult,
    InputInspectionAction,
)

_DEFAULT_DYN_POLICY_FILE = "/home1/aivax/data_resource/policy_signal/aivax_policy.json"
_CONSENT_ON = frozenset({"on", "1", "true", "yes", "enable", "enabled"})
_HARD_CSO = frozenset({"blocked", "untagged", "error"})
_POLICY_CACHE: tuple[str, float, bool] | None = None
_BLOCK_COPY = None
_ENGINE = None
# 이 서비스는 기본 모드에서 제목·인용·탐지 사유 카드를 본다.
_CARD_SERVICES = frozenset({1, 2, 3, 4, 5})


def _block_copy():
    """엔진 카드 조립 모듈. 파이프라인이 문장을 다시 적지 않는다."""
    global _BLOCK_COPY
    if _BLOCK_COPY is not None:
        return _BLOCK_COPY
    for parent in Path(__file__).resolve().parents:
        core = parent / "ai_engine" / "core"
        if (core / "block_copy.py").is_file():
            folder = str(core)
            if folder not in sys.path:
                sys.path.append(folder)
            import block_copy

            _BLOCK_COPY = block_copy
            return block_copy
    return None


def _engine():
    """채팅 카드를 만드는 엔진 함수. 프로세스가 뜨면 한 번만 읽는다."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    if _block_copy() is None:
        return None
    import ai_policy_engine

    _ENGINE = ai_policy_engine
    return ai_policy_engine


def engine_consent_enabled() -> bool:
    """엔진이 동의 상자를 붙이는 스위치. 같은 정책 파일을 본다."""
    global _POLICY_CACHE
    path = os.environ.get("AIVAX_DYN_POLICY_FILE", "").strip() or _DEFAULT_DYN_POLICY_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    if _POLICY_CACHE is not None and _POLICY_CACHE[0] == path and _POLICY_CACHE[1] == mtime:
        return _POLICY_CACHE[2]
    enabled = False
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            raw = str(payload.get("user_consent") or "").strip().lower()
            enabled = raw in _CONSENT_ON
    except (OSError, ValueError):
        enabled = False
    _POLICY_CACHE = (path, mtime, enabled)
    return enabled


def _consent_phrase(prompt: str) -> bool:
    module = _block_copy()
    phrases = {
        str(getattr(module, "CONSENT_SHORT", "") or "동의"),
        str(getattr(module, "CONSENT_LONG", "") or "동의합니다"),
    }
    value = str(prompt or "").strip().strip(" \t.。!！?？~…,，").strip()
    return value in phrases


def _file_block(result: InputAnalysisResult, block_message: str) -> bool:
    title = get_block_phrases().file_title
    if title and title in block_message:
        return True
    for execution in result.executions:
        if (
            execution.action is InputInspectionAction.BLOCK
            and execution.inspector_type is InputInspectorType.MULTIMODAL
            and execution.block_message
            and execution.block_message in block_message
        ):
            return True
    return False


def _default_visible(
    block_message: str,
    *,
    status: str,
    cso_message: str,
    consent: bool,
    masked_contents: str,
    ai_service_id: int,
) -> str:
    """기본 모드에서 채팅에 나가는 문장. 분석시간은 넣지 않는다.

    분석시간은 응답이 엔진에 돌아온 뒤에만 알 수 있다. 그 줄을 넣으면
    로그와 채팅이 서로 다른 숫자가 된다.
    """
    engine = _engine()
    if engine is None:
        return ""
    if status in _HARD_CSO:
        return engine._cso_blocked_visible_response(block_message, cso_message)
    guidance = cso_message if status == "open" else ""
    if ai_service_id in _CARD_SERVICES or ai_service_id == 0:
        return engine._gpt_gemini_visible_response(
            block_message,
            consent_hint=consent,
            analysis_ms=None,
            detect_filter="",
            cso_message=guidance,
        )
    return engine._block_display_text(
        masked_contents,
        block_message,
        consent_hint=consent,
        cso_message=guidance,
    )


def logged_block_output(
    block_message: str,
    *,
    prompt: str = "",
    file_block: bool = False,
    cso_status: str = "",
    cso_message: str = "",
    masked_contents: str = "",
    ai_service_id: int = 0,
) -> str:
    """저장할 output_text. 행안부와 기본 모드 모두 채팅 문장이다."""
    raw = str(block_message or "")
    module = _block_copy()
    if module is None:
        return raw
    status = str(cso_status or "").strip().lower()
    consent = (
        engine_consent_enabled()
        and status not in _HARD_CSO
        and not _consent_phrase(prompt)
    )
    if module.guideline_notice_enabled():
        visible = module.format_user_visible_block(
            raw,
            file_block=file_block,
            cso_status=status,
            cso_message=cso_message if status == "open" else "",
            consent=consent,
        )
        return visible or raw
    visible = _default_visible(
        raw,
        status=status,
        cso_message=cso_message,
        consent=consent,
        masked_contents=masked_contents,
        ai_service_id=ai_service_id,
    )
    return visible or raw


def logged_block_output_for_result(
    result: InputAnalysisResult,
    prompt: str = "",
    ai_service_id: int = 0,
) -> str:
    """분석 결과 한 건의 로그 문장. 엔진에 돌려주는 block_message 는 바꾸지 않는다."""
    message = result.cso_block_message or result.final_decision.block_message
    status = summarize_cso_grades(result.cso_grades)
    status_value = status.value if status is not None else ""
    guidance = ""
    if status_value:
        guidance = get_block_phrases().cso_guidance(status_value, blocked=True)
    return logged_block_output(
        message,
        prompt=prompt,
        file_block=_file_block(result, message),
        cso_status=status_value,
        cso_message=guidance,
        masked_contents=result.final_decision.masked_prompt,
        ai_service_id=ai_service_id,
    )
