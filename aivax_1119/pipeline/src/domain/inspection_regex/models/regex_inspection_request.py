"""정규식 정책 검사 요청."""

from dataclasses import dataclass

from src.domain.policy_regex.models import RegexPolicySnapshot


@dataclass(frozen=True, slots=True)
class RegexInspectionRequest:
    contents: str
    email: str = ""
    ai_service_type: int = 0
    policy: RegexPolicySnapshot | None = None
