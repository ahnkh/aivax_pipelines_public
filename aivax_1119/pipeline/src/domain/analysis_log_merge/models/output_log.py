"""출력 로그 처리에 사용하는 HTTP와 독립적인 데이터 계약."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class OutputLogInput:
    """LLM 출력 또는 차단 메시지와 병합에 필요한 요청 식별 정보."""

    message_id: str
    llm_output: str
    session_id: str | None = ""
    user_id: str | None = ""
    email: str | None = ""
    ai_service: int | None = 0
    client_host: str | None = ""
    user_consent: bool = False
    generated_only: bool = False
    output_revision: bool = False
    logged_at: str = ""
    model: str = ""
    generated_files: tuple[dict[str, Any], ...] = field(default_factory=tuple)
