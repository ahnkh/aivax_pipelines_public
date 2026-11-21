"""secuXper 정책의 대상과 우선순위. 외부 정책 본문은 포함하지 않는다."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SecuxperPolicyRule:
    id: str
    name: str
    policy_code: str
    scope: str
    subject_id: str = ""
    targets: str = "prompt"
    category: str = ""


@dataclass(frozen=True, slots=True)
class SecuxperPolicySnapshot:
    version: str = "uninitialized"
    rules: tuple[SecuxperPolicyRule, ...] = ()
    group_members: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def match(
        self, email: str, ai_service_id: int | str
    ) -> tuple[SecuxperPolicyRule, ...]:
        email = email.strip().lower()
        groups = {
            group for group, members in self.group_members if email and email in members
        }
        for scope in ("user", "service", "group", "default"):
            matches = tuple(
                rule
                for rule in self.rules
                if rule.scope == scope
                and (
                    (
                        scope == "user"
                        and bool(email)
                        and rule.subject_id.lower() == email
                    )
                    or (scope == "service" and rule.subject_id == str(ai_service_id))
                    or (scope == "group" and rule.subject_id in groups)
                    or scope == "default"
                )
            )
            if matches:
                seen: set[str] = set()
                unique = []
                for rule in matches:
                    if rule.policy_code not in seen:
                        seen.add(rule.policy_code)
                        unique.append(rule)
                return tuple(unique)
        return ()


@dataclass(frozen=True, slots=True)
class SecuxperPolicyRefreshResult:
    active_rule_count: int
