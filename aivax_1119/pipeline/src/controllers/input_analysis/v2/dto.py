"""V2 multiple_filter 요청·응답 DTO."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.time import TimeNormalizer
from src.domain.inspection_input.models.cso_grade_result import CsoInspectionStatus

FilterActionV2 = Literal["accept", "block", "masking", "failed", ""]
InputFilterV2 = Literal[
    "secret_filter", "file_block_filter", "slm_filter", "secuxper_filter"
]

_CONSENT_TRUE_VALUES = frozenset({"true", "1", "on", "yes", "enable", "enabled"})
_CONSENT_FALSE_VALUES = frozenset(
    {"false", "0", "off", "no", "disable", "disabled", ""}
)


def _current_event_time() -> str:
    return TimeNormalizer.to_utc_iso(TimeNormalizer.utc_now(), timespec="microseconds")


class AttachmentV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default="", description="Pipeline에서 읽을 파일 경로")
    name: str | None = Field(default="", description="원본 파일 이름")
    size: int | None = Field(default=0, description="파일 크기. CSO 로그 대체값")
    mime_type: str | None = Field(default="", description="MIME 타입. CSO 로그 대체값")


class InputAnalysisRequestV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(description="검사할 원문. 첨부파일만 보내면 빈 문자열")
    message_id: str = Field(
        min_length=1, description="필수 INPUT/OUTPUT 연결 ID. 공백 제거"
    )
    filter_list: list[InputFilterV2] | None = Field(
        default_factory=lambda: [
            "secret_filter",
            "file_block_filter",
            "slm_filter",
            "secuxper_filter",
        ],
        description="검사 요청 목록. 전역 활성화 설정도 적용. 로그는 항상 처리",
    )
    user_name: str | None = Field(default="", description="사용자 이름")
    email: str | None = Field(default="", description="정책 적용·로그에 사용할 이메일")
    ai_service: int | None = Field(
        default=0, description="AI 서비스 ID. Management의 Valkey 카탈로그 기준"
    )
    model: str | None = Field(
        default="", description="어댑터가 본 모델 표시명. 없으면 빈 문자열"
    )
    client_host: str | None = Field(default="", description="사용자 IP 주소")
    attachments: list[AttachmentV2] | None = Field(
        default_factory=list, description="검사할 첨부파일 목록"
    )
    event_time: str = Field(
        default_factory=_current_event_time,
        description=(
            "원본 요청 시각. ISO 8601. 생략·null·빈 문자열이면 UTC 현재 시각. "
            "기본값 형식: YYYY-MM-DDTHH:mm:ss.ssssssZ"
        ),
    )
    user_consent: bool = Field(
        default=False,
        description=(
            "사용자 동의. true: true, 1, true/1/on/yes/enable/enabled 문자열. "
            "false: false, 0, null, 빈 문자열, false/0/off/no/disable/disabled 문자열. "
            "문자열은 앞뒤 공백·대소문자를 무시하며 그 외 값은 422"
        ),
    )

    @field_validator("message_id")
    @classmethod
    def require_message_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message_id must not be blank")
        return value

    @field_validator("event_time", mode="before")
    @classmethod
    def default_event_time(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return _current_event_time()
        return value

    @field_validator(
        "user_consent",
        mode="before",
        json_schema_input_type=bool | Literal[0, 1] | str | None,
    )
    @classmethod
    def normalize_user_consent(cls, value: object) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if type(value) is int and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in _CONSENT_TRUE_VALUES:
                return True
            if normalized in _CONSENT_FALSE_VALUES:
                return False
        raise ValueError(
            "user_consent must be an explicitly supported true/false value"
        )


class FinalDecisionV2(BaseModel):
    action: FilterActionV2 = Field(
        description="최종 판정. 빈 문자열은 미탐지·Mirror 접수"
    )
    masked_contents: str = Field(description="마스킹한 원문. 없으면 빈 문자열")
    block_message: str = Field(description="차단 안내. 없으면 빈 문자열")
    message_id: str = Field(description="OUTPUT 호출에 사용할 INPUT 연결 ID")


class CsoResponseV2(BaseModel):
    status: CsoInspectionStatus = Field(
        description="스킵 정책 적용 후 요청 전체 CSO 상태"
    )
    message: str = Field(description="CSO 안내 문구. 모든 파일을 스킵하면 빈 문자열")


class InputAnalysisResponseV2(BaseModel):
    cso: CsoResponseV2 | None = Field(
        default=None, description="CSO 상태와 안내. 검사를 수행하지 않으면 null"
    )
    final_decision: FinalDecisionV2 = Field(description="호출자가 사용할 최종 판정")
