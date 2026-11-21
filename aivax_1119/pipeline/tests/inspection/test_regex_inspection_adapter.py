"""정규식 정책 검사 adapter 계약 테스트."""

from src.adapters.inspection_regex import PythonRegexInspectionAdapter
from src.domain.inspection_regex.models import RegexInspectionRequest
from src.domain.policy_regex.models import RegexPolicyRule, RegexPolicySnapshot


class StaticRegexPolicy:
    def __init__(
        self,
        *rules: RegexPolicyRule,
        group_members: tuple[tuple[str, tuple[str, ...]], ...] = (),
    ) -> None:
        self.snapshot = RegexPolicySnapshot(
            rules=rules,
            group_members=group_members,
        )

    def get_policy(self) -> RegexPolicySnapshot:
        return self.snapshot


def rule(
    rule_id: str,
    expression: str,
    action: str = "block",
    scope: str = "default",
    **kwargs,
) -> RegexPolicyRule:
    return RegexPolicyRule(
        id=rule_id,
        name=f"{rule_id} policy",
        expression=expression,
        action=action,
        scope=scope,
        **kwargs,
    )


def inspection(*rules: RegexPolicyRule) -> PythonRegexInspectionAdapter:
    return PythonRegexInspectionAdapter(StaticRegexPolicy(*rules))


def test_inspect_returns_evidence_spans_and_highest_priority_action() -> None:
    adapter = inspection(
        rule("mask", r"password=[A-Za-z0-9_]+", "masking"),
        rule("block", r"api-[a-z0-9]+", "block"),
    )

    result = adapter.inspect(
        RegexInspectionRequest(
            contents="password=abc api-secret",
        )
    )

    assert result.action == "block"
    assert result.selected_policy is not None
    assert result.selected_policy.id == "block"
    assert result.counts == {"masking": 1, "block": 1}
    assert [item.matched_text for item in result.evidence] == [
        "password=abc",
        "api-secret",
    ]


def test_scope_precedence_stops_before_default_scope() -> None:
    adapter = inspection(
        rule(
            "user-mask",
            "secret",
            "masking",
            "user",
            subject_id="user@example.com",
        ),
        rule("default-block", "secret", "block"),
    )

    result = adapter.inspect(
        RegexInspectionRequest(
            contents="secret",
            email="user@example.com",
        )
    )

    assert result.action == "masking"
    assert result.selected_policy is not None
    assert result.selected_policy.id == "user-mask"


def test_user_scope_compares_normalized_email() -> None:
    adapter = inspection(
        rule("email-user", "secret", "block", "user", subject_id="user@example.com"),
    )

    result = adapter.inspect(
        RegexInspectionRequest(
            contents="secret",
            email=" User@Example.com ",
        )
    )

    assert result.action == "block"
    assert result.selected_policy is not None
    assert result.selected_policy.id == "email-user"


def test_default_scope_checks_all_rules_and_uses_whole_match() -> None:
    adapter = inspection(
        rule("token", r"token=(?P<value>[A-Z0-9]+)", "masking"),
        rule("second", r"other", "block"),
    )

    result = adapter.inspect(RegexInspectionRequest(contents="token=ABC other"))

    assert result.action == "block"
    assert result.counts == {"masking": 1, "block": 1}
    assert [item.matched_text for item in result.evidence] == ["token=ABC", "other"]


def test_group_scope_uses_membership_in_regex_snapshot() -> None:
    policy = StaticRegexPolicy(
        rule("group-rule", "secret", "block", "group", subject_id="group-1"),
        group_members=(("group-1", ("user@example.com",)),),
    )
    adapter = PythonRegexInspectionAdapter(policy)

    matched = adapter.inspect(
        RegexInspectionRequest(
            contents="secret",
            email=" USER@example.com ",
        )
    )
    unmatched = adapter.inspect(
        RegexInspectionRequest(
            contents="secret",
            email="other@example.com",
        )
    )

    assert matched.action == "block"
    assert unmatched.action == ""


def test_inspection_uses_the_latest_snapshot_supplied_by_policy() -> None:
    policy = StaticRegexPolicy(rule("existing", "old"))
    adapter = PythonRegexInspectionAdapter(policy)
    assert (
        adapter.inspect(
            RegexInspectionRequest(contents="old"),
        ).selected_policy
        is not None
    )

    policy.snapshot = RegexPolicySnapshot(
        rules=(
            rule("valid", "new"),
            rule("invalid", "("),
        )
    )
    result = adapter.inspect(RegexInspectionRequest(contents="new"))

    assert result.selected_policy is not None
    assert result.selected_policy.id == "valid"
