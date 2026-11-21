"""Regex 정책 cache를 갱신하는 유스케이스."""

from src.domain.policy_regex.models import RegexPolicyRefreshResult
from src.domain.policy_regex.ports import RegexPolicyPort


class RefreshRegexPolicyUseCase:
    def __init__(self, policy: RegexPolicyPort) -> None:
        self._policy = policy

    def execute(self) -> RegexPolicyRefreshResult:
        return self._policy.refresh()
