"""SLM 검사에 배포할 정책 snapshot."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SlmPolicyRule:
    id: str
    name: str
    action: str
    detection_kind: str = ""
    detection_type: str | None = None
    scope: str = "default"
    category: str = ""
    subject_id: str = ""


@dataclass(frozen=True, slots=True)
class SlmPolicySnapshot:
    """SLM rule과 group membership을 함께 교체하는 불변 snapshot."""

    rules: tuple[SlmPolicyRule, ...] = ()
    group_members: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def default_rule(self) -> SlmPolicyRule | None:
        return next((rule for rule in self.rules if rule.scope == "default"), None)


@dataclass(frozen=True, slots=True)
class SlmPolicyRefreshResult:
    active_rule_count: int
