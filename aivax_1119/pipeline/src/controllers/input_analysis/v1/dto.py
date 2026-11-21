"""``/v1/filter/multiple_filter`` HTTP 요청 계약."""

from pydantic import BaseModel, Field, model_validator

from src.domain.analysis_log_delivery.models.user_consent import is_user_consent_on


class FileAttachItem(BaseModel):
    id: str | None = Field(default="", description="file id")
    size: int | None = Field(default=0, description="file size")
    name: str | None = Field(default="", description="file name")
    mime_type: str | None = Field(default="", description="mime type")


class VariantFilterForm(BaseModel):
    """V1 multiple_filter의 공개 요청 계약.

    filter_list : 차단 필터 리스트

    - secret_filter : 정규표현식 기반 필터
    - file_block_filter : 파일 분석 필터
    - input_filter : 프롬프트 로그 저장
    - slm_filter : SLM 필터
    - secuxper_filter : SecuXper 필터 (전역 enabled 설정 필요)
    """

    filter_list: list[str] | None = [
        "input_filter",
        "secret_filter",
        "file_block_filter",
        "slm_filter",
        "secuxper_filter",
    ]
    prompt: str = (
        "내 API key는 API_key=sk-1234567-0000-abdcdef 인데 "
        "이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요"
    )
    encoding: bool | None = Field(default=False, description="프롬프트 인코딩 여부")
    user_id: str | None = Field(default="", description="사용자ID")
    user_name: str | None = Field(default="", description="사용자 이름")
    email: str | None = Field(default="", description="email")
    ai_service: int | None = Field(
        default=0,
        description="AI 서비스 ID. Management의 Valkey 카탈로그 기준",
    )
    client_host: str | None = Field(default="", description="사용자 host, ip")
    session_id: str | None = Field(default="", description="session id")
    attachments: list[FileAttachItem] | None = Field(
        default_factory=list,
        description="첨부 파일 리스트",
    )
    message_id: str = Field(default="", description="message id")
    model: str | None = Field(
        default="", description="어댑터가 본 모델 표시명. 없으면 빈 문자열"
    )
    event_time: str | None = Field(
        default="", description="packet/user event time (ISO)"
    )
    user_consent: str | bool | None = Field(
        default="", description="true or on to allow after CSO without analysis filters"
    )
    debug: bool | None = Field(default=False, description="debug mode")

    @model_validator(mode="after")
    def require_consent_message_id(self) -> "VariantFilterForm":
        if is_user_consent_on(self.user_consent) and not self.message_id.strip():
            raise ValueError("user_consent requires the original message_id")
        return self


# HTTP 공개 모델의 이름은 기존 OpenAPI 계약을 유지한다. 내부에서는 의미가 드러나는
# 별칭을 사용해 controller 코드가 legacy 이름에 묶이지 않게 한다.
InputAttachmentItem = FileAttachItem
InputAnalysisItem = VariantFilterForm
