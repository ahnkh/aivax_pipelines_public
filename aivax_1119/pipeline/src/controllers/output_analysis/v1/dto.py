"""V1 출력 분석 요청 계약."""

from pydantic import BaseModel, Field, model_validator

from src.domain.analysis_log_delivery.models.user_consent import is_user_consent_on


class GeneratedFileItem(BaseModel):
    """세션 다운로드로 확보한 생성 이미지. 관리 화면은 업로드와 같은 첨부 목록으로 본다."""

    file_name: str = Field(default="", description="저장된 파일 이름")
    file_backup_path: str = Field(default="", description="백업 경로")
    mime_type: str = Field(default="", description="MIME")
    size: int = Field(default=0, description="바이트 크기")


class OutputFilterItem(BaseModel):
    llm_output: str = Field(default="", description="llm응답 결과")
    generated_only: bool = Field(
        default=False,
        description="이미 저장된 분석 로그에 생성 파일만 추가",
    )
    output_revision: bool = Field(
        default=False,
        description="같은 메시지의 응답이 길어져 저장된 본문을 교체",
    )
    logged_at: str = Field(
        default="",
        description="처음 저장된 분석 로그의 UTC 시각. 본문 교체 때 이 시각을 유지한다",
    )
    generated_files: list[GeneratedFileItem] = Field(
        default_factory=list,
        description="이번 턴에서 생성된 이미지",
    )

    user_id: str | None = Field(default="", description="사용자ID")
    email: str | None = Field(default="", description="email")
    ai_service: int | None = Field(
        default=0, description="AI 서비스 ID. Management의 Valkey 카탈로그 기준"
    )
    client_host: str | None = Field(default="", description="사용자 host, ip")
    session_id: str | None = Field(default="", description="session id")

    message_id: str = Field(default="", description="message id")
    model: str | None = Field(
        default="", description="어댑터가 본 모델 표시명. 없으면 빈 문자열"
    )
    user_consent: str | bool | None = Field(
        default="", description="on when updating a blocked request after user consent"
    )

    debug: bool | None = Field(default=False, description="debug mode")
    # pass

    @model_validator(mode="after")
    def require_consent_message_id(self) -> "OutputFilterItem":
        if is_user_consent_on(self.user_consent) and not self.message_id.strip():
            raise ValueError("user_consent requires the original message_id")
        return self
