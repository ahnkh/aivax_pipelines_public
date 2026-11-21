"""V2 output_filter 요청 DTO. 성공 응답은 본문 없는 HTTP 204다."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OutputAnalysisRequestV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, description="INPUT과 같은 연결 ID. 공백 제거")
    llm_output: str = Field(description="실제 AI 응답. 빈 문자열 허용")
    user_id: str | None = Field(default="", description="출력 로그 사용자 ID")
    email: str | None = Field(default="", description="출력 로그 이메일")
    ai_service: int | None = Field(
        default=0, description="AI 서비스 ID. Management의 Valkey 카탈로그 기준"
    )
    model: str | None = Field(
        default="", description="어댑터가 본 모델 표시명. 없으면 빈 문자열"
    )
    client_host: str | None = Field(default="", description="출력 로그 사용자 IP 주소")
    user_consent: str | bool | None = Field(
        default="", description="사용자 동의 출력. true 또는 on 등 기존 값 지원"
    )

    @field_validator("message_id")
    @classmethod
    def require_message_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message_id must not be blank")
        return value
