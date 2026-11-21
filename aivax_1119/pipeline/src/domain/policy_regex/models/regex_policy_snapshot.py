"""정규식 검사에 배포할 정책 snapshot."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegexPolicyRule:
    id: str
    name: str
    expression: str
    action: str
    scope: str = "default"
    category: str = ""
    subject_id: str = ""


@dataclass(frozen=True, slots=True)
class RegexPolicySnapshot:
    """Regex adapter 하나로 실행 가능한 rule과 group membership snapshot."""

    rules: tuple[RegexPolicyRule, ...]
    group_members: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class RegexPolicyRefreshResult:
    """정책 cache 갱신 결과.

    검사 구현체의 compile 상세는 외부로 새지 않고, 정책 lifecycle에 필요한
    상태와 집계만 노출한다.
    """

    active_rule_count: int
    rejected_rule_count: int = 0
