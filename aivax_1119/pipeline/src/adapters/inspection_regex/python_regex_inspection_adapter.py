"""Python ``re`` 기반 정규식 정책 검사 adapter."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from src.domain.inspection_regex.models import (
    RegexEvidence,
    RegexInspectionRequest,
    RegexInspectionResult,
    RegexMatchedPolicy,
)
from src.domain.inspection_regex.ports import RegexInspectionPort
from src.domain.policy_regex.models import RegexPolicyRule, RegexPolicySnapshot
from src.domain.policy_regex.ports import RegexPolicyProviderPort


@dataclass(frozen=True, slots=True)
class _CompiledRule:
    rule: RegexPolicyRule
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class _CompiledRegexPlan:
    rules_by_scope: dict[str, tuple[_CompiledRule, ...]]
    group_members: dict[str, frozenset[str]]


class PythonRegexInspectionAdapter(RegexInspectionPort):
    """정책 공급자의 현재 정책을 Python ``re``로 검사한다."""

    _SCOPE_ORDER = ("user", "service", "group", "default")
    _ACTION_PRIORITY = ("block", "masking", "accept", "")

    def __init__(self, policy_provider: RegexPolicyProviderPort) -> None:
        self._policy_provider = policy_provider
        self._compiled_plan = _CompiledRegexPlan(
            rules_by_scope={scope: () for scope in self._SCOPE_ORDER},
            group_members={},
        )
        self._compiled_policy: RegexPolicySnapshot | None = None
        self._compile_lock = threading.Lock()

    def inspect(self, request: RegexInspectionRequest) -> RegexInspectionResult:
        # rule test 요청은 운영 cache를 교체하지 않고 전달된 임시 정책만 검사한다.
        plan = (
            self._compile(request.policy)
            if request.policy is not None
            else self._current_plan()
        )
        evidence: list[RegexEvidence] = []
        counts: dict[str, int] = {}
        selected_by_action: dict[str, RegexMatchedPolicy] = {}

        for scope in self._SCOPE_ORDER:
            matched = self._inspect_scope(
                scope,
                plan.rules_by_scope[scope],
                request,
                evidence,
                counts,
                selected_by_action,
                plan.group_members,
            )

            # 상위 scope에서 match되면 하위 scope는 검사 x
            if matched:
                break

        action, selected_policy = self._select_action(selected_by_action)
        return RegexInspectionResult(
            action=action,
            selected_policy=selected_policy,
            spans=tuple((item.start, item.end) for item in evidence),
            evidence=tuple(evidence),
            counts=counts,
        )

    def _compile(self, snapshot: RegexPolicySnapshot) -> _CompiledRegexPlan:
        rules_by_scope: dict[str, list[_CompiledRule]] = {
            scope: [] for scope in self._SCOPE_ORDER
        }

        for rule in snapshot.rules:
            if rule.scope not in rules_by_scope:
                continue
            if not rule.expression:
                continue
            try:
                pattern = re.compile(rule.expression)
            except (re.error, TypeError, ValueError):
                # 정책 adapter가 검증하지만 custom provider 구현에도 안전하게 대응한다.
                continue
            rules_by_scope[rule.scope].append(_CompiledRule(rule, pattern))

        return _CompiledRegexPlan(
            rules_by_scope={
                scope: tuple(rules) for scope, rules in rules_by_scope.items()
            },
            group_members={
                group_id: frozenset(emails)
                for group_id, emails in snapshot.group_members
            },
        )

    def _current_plan(self) -> _CompiledRegexPlan:
        snapshot = self._policy_provider.get_policy()
        if snapshot is None:
            return self._compiled_plan

        if snapshot is self._compiled_policy:
            return self._compiled_plan

        with self._compile_lock:
            if snapshot is not self._compiled_policy:
                self._compiled_plan = self._compile(snapshot)
                self._compiled_policy = snapshot
            return self._compiled_plan

    def _inspect_scope(
        self,
        scope: str,
        compiled_rules: tuple[_CompiledRule, ...],
        request: RegexInspectionRequest,
        evidence: list[RegexEvidence],
        counts: dict[str, int],
        selected_by_action: dict[str, RegexMatchedPolicy],
        group_members: dict[str, frozenset[str]],
    ) -> bool:
        matched_any_rule = False
        for compiled_rule in compiled_rules:
            if not self._matches_subject(
                compiled_rule.rule,
                request,
                group_members,
            ):
                continue
            policy = RegexMatchedPolicy(
                id=compiled_rule.rule.id,
                name=compiled_rule.rule.name,
                action=compiled_rule.rule.action,
                targets="prompt",
                category=compiled_rule.rule.category,
                scope=scope,
            )
            rule_matched = False
            for match in compiled_rule.pattern.finditer(request.contents):
                start, end = match.span(0)
                if start == end:
                    continue
                evidence.append(
                    RegexEvidence(
                        policy=policy,
                        matched_text=request.contents[start:end],
                        start=start,
                        end=end,
                    )
                )
                counts[policy.action] = counts.get(policy.action, 0) + 1
                selected_by_action.setdefault(policy.action, policy)
                rule_matched = True
            if rule_matched:
                matched_any_rule = True

        return matched_any_rule

    @staticmethod
    def _matches_subject(
        rule: RegexPolicyRule,
        request: RegexInspectionRequest,
        group_members: dict[str, frozenset[str]],
    ) -> bool:
        if rule.scope == "default":
            return True
        if rule.scope == "user":
            return bool(request.email.strip()) and (
                request.email.strip().lower() == rule.subject_id.strip().lower()
            )
        if rule.scope == "service":
            return str(request.ai_service_type).strip() == rule.subject_id.strip()
        if rule.scope == "group":
            return request.email.strip().lower() in group_members.get(
                rule.subject_id,
                frozenset(),
            )
        return False

    def _select_action(
        self,
        selected_by_action: dict[str, RegexMatchedPolicy],
    ) -> tuple[str, RegexMatchedPolicy | None]:
        for action in self._ACTION_PRIORITY:
            if action in selected_by_action:
                return action, selected_by_action[action]
        if selected_by_action:
            action, policy = next(iter(selected_by_action.items()))
            return action, policy
        return "", None
