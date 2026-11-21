"""파이프라인이 만드는 차단 안내 문장.

문장은 ``ai_engine/config/block_phrases.json`` 한 파일이다.
이 모듈은 그 파일의 파이프라인 칸으로 ``block_message`` 를 조립만 한다.
엔진은 같은 파일을 읽어 화면 카드·지시문·동의 문구를 만든다.

환경변수 ``AIVAX_PIPELINE_BLOCK_PHRASES_FILE`` 또는
``AIVAX_BLOCK_PHRASES_FILE`` 이 다른 파일을 가리키면 그 키만 덮어쓴다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True, slots=True)
class BlockPhrases:
    """문구 파일의 칸. 문장 원문은 JSON 에만 있다. ``{이름}`` 은 호출자가 채운다."""

    mask_token: str = ""
    prompt_title: str = ""
    file_title: str = ""
    cso_title: str = ""
    masking_title: str = ""
    secuxper_title: str = ""
    prompt_lock: str = ""
    file_lock: str = ""
    sensitivity_warning: str = ""
    detect_type_line: str = ""
    detected_content_line: str = ""
    unknown_detect_type_line: str = ""
    external_lock: str = ""
    slm_lock: str = ""
    prompt_footer: str = ""
    file_footer: str = ""
    admin_footer: str = ""
    prompt_footer_lead: str = ""
    file_footer_lead: str = ""
    cso_cs: str = ""
    cso_untagged: str = ""
    cso_error: str = ""
    cso_guidance_untagged: str = ""
    cso_guidance_blocked: str = ""
    cso_guidance_error: str = ""
    cso_guidance_open: str = ""
    cso_guidance_open_consent: str = ""
    notice_style: str = ""
    guideline_cso_open: str = ""
    secuxper_body: str = ""
    secuxper_detect: str = ""
    secuxper_unknown_reason: str = ""
    secuxper_retry: str = ""
    secuxper_admin: str = ""
    masking_lock: str = ""

    def _guideline_notice(self) -> bool:
        """환경변수의 안내 스타일이 문구 파일의 notice_style보다 우선한다."""
        raw = os.environ.get("AIVAX_BLOCK_NOTICE_STYLE", "").strip().lower()
        style = raw or str(self.notice_style or "").strip().lower()
        return style == "guideline"

    def detect_type(self, detect_type: str) -> str:
        return self.detect_type_line.format(detect_type=detect_type)

    def detected_content(self, detected: str) -> str:
        return self.detected_content_line.format(detected=detected)

    def prompt_block(self, detect_type: str) -> str:
        """정규식 프롬프트 차단."""
        return _lines(
            self.prompt_title,
            self.prompt_lock,
            self.detect_type(detect_type),
            "",
            self.sensitivity_warning,
            self.prompt_footer,
            self.admin_footer,
        )

    def file_block(self, detect_type: str) -> str:
        """첨부파일 차단."""
        return _lines(
            self.file_title,
            self.file_lock,
            self.detect_type(detect_type),
            "",
            self.sensitivity_warning,
            self.file_footer,
            self.admin_footer,
        )

    def slm_block(self, detection: str, detect_type: str, detected: str) -> str:
        return _lines(
            self.prompt_title,
            self.slm_lock.format(detection=detection),
            self.detect_type(detect_type),
            self.detected_content(detected),
            "",
            self.sensitivity_warning,
            self.prompt_footer,
            self.admin_footer,
        )

    def external_block(self, detect_type: str = "") -> str:
        detect_line = (
            self.detect_type(detect_type)
            if detect_type
            else self.unknown_detect_type_line
        )
        return _lines(
            self.prompt_title,
            self.external_lock,
            detect_line,
            "",
            self.prompt_footer,
            self.admin_footer,
        )

    def secuxper_block(self, reason: str) -> str:
        shown = reason or self.secuxper_unknown_reason
        return (
            f"{self.secuxper_title}\n\n"
            f"{self.secuxper_body}\n"
            f"{self.secuxper_detect.format(reason=shown)}\n\n"
            f"{self.secuxper_retry}\n"
            f"{self.secuxper_admin}\n"
        )

    def masking_notice(self, detect_type: str, detected: str) -> str:
        """정책 테스트 화면의 마스킹 안내. 채팅 차단 경로가 아니다."""
        return _lines(
            self.masking_title,
            self.masking_lock,
            self.detect_type(detect_type),
            self.detected_content(detected),
            "",
            self.admin_footer,
        )

    def cso_guidance(self, status: str, *, blocked: bool) -> str:
        """응답 ``cso.message``. 카드 본문인 ``cso_block`` 과 다른 안내이다."""
        if status == "untagged":
            return self.cso_guidance_untagged
        if status == "blocked":
            return self.cso_guidance_blocked
        if status == "error":
            return self.cso_guidance_error
        if status == "open":
            # 행안부 안내(guideline)일 때만 사후 결재 문구를 쓴다. 기본 문구는 그대로다.
            guideline = self._guideline_notice() and bool(
                self.guideline_cso_open.strip()
            )
            head = (
                self.guideline_cso_open.strip() if guideline else self.cso_guidance_open
            )
            if blocked and self.cso_guidance_open_consent:
                sep = "\n" if guideline else " "
                return f"{head}{sep}{self.cso_guidance_open_consent}"
            return head
        return ""

    def cso_block(self, *, cs: bool, untagged: bool, error: bool) -> str:
        messages = [self.cso_title, ""]
        if cs:
            messages.append(self.cso_cs)
        if untagged:
            messages.append(self.cso_untagged)
        if error:
            messages.append(self.cso_error)
        return "\n".join(messages)


def _lines(*parts: str) -> str:
    return "\n".join(parts) + "\n"


def _configured_phrase_file() -> Path | None:
    raw = (
        os.environ.get("AIVAX_PIPELINE_BLOCK_PHRASES_FILE")
        or os.environ.get("AIVAX_BLOCK_PHRASES_FILE")
        or ""
    ).strip()
    if raw:
        return Path(raw)

    root = Path(__file__).resolve().parents[4]
    env_file = Path(os.environ.get("AIVAX_PIPELINE_ENV_FILE") or ".env")
    if not env_file.is_absolute():
        env_file = root / env_file
    if not env_file.is_file():
        return None
    values = dotenv_values(env_file)
    raw = (
        values.get("AIVAX_PIPELINE_BLOCK_PHRASES_FILE")
        or values.get("AIVAX_BLOCK_PHRASES_FILE")
        or ""
    ).strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else env_file.parent / path


def _canonical_phrase_file() -> Path | None:
    """저장소 옆 또는 라이브 루트의 ``ai_engine/config/block_phrases.json``."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "ai_engine" / "config" / "block_phrases.json"
        if candidate.is_file():
            return candidate
    return None


def _read_phrase_payload(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items() if value is not None}


def load_block_phrases(path: Path | None = None) -> BlockPhrases:
    """문구 파일을 읽는다. 다른 경로가 지정되면 그 키만 기본 파일 위에 덮어쓴다."""
    canonical = _canonical_phrase_file()
    payload = _read_phrase_payload(canonical)
    selected = path if path is not None else _configured_phrase_file()
    if selected is not None and selected.is_file():
        overlay = _read_phrase_payload(selected)
        same = canonical is not None and selected.resolve() == canonical.resolve()
        payload = overlay if same or not payload else {**payload, **overlay}
    known = set(BlockPhrases.__dataclass_fields__)
    updates = {key: payload[key] for key in known if key in payload}
    return replace(BlockPhrases(), **updates)


@lru_cache(maxsize=1)
def get_block_phrases() -> BlockPhrases:
    return load_block_phrases()


def reload_block_phrases() -> BlockPhrases:
    get_block_phrases.cache_clear()
    return get_block_phrases()
