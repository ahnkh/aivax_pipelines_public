"""입력 분석 유스케이스의 순수 요청 모델."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

INPUT_LOG_REQUEST_NAME = "input_filter"


class InputInspectorType(StrEnum):
    """내부 inspector 종류. value는 기존 JSON filter name을 보존한다."""

    REGEX = "secret_filter"
    MULTIMODAL = "file_block_filter"
    SLM = "slm_filter"
    SECUXPER = "secuxper_filter"


@dataclass(frozen=True, slots=True)
class InputAttachment:
    id: str = ""
    size: int = 0
    name: str = ""
    mime_type: str = ""


@dataclass(frozen=True, slots=True)
class InputAnalysisUser:
    id: str = ""
    uuid: str = ""
    email: str = ""
    ai_service: int = 0
    client_host: str = ""
    name: str = ""


@dataclass(frozen=True, slots=True)
class InputAnalysisRequest:
    # HTTP의 filter_list를 그대로 옮긴 값이다. controller는 inspector 종류를
    # 해석하지 않고, InputAnalysisUseCase가 이 값을 실행 계획으로 해석한다.
    requested_filter_names: tuple[str, ...]
    prompt: str
    user: InputAnalysisUser = field(default_factory=InputAnalysisUser)
    session_id: str = ""
    message_id: str = ""
    event_time: str = ""
    model: str = ""
    attachments: tuple[InputAttachment, ...] = ()
    debug: bool = False
    user_consent: bool = False
