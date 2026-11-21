"""SLM 정책 cache를 갱신하는 유스케이스."""

from src.domain.policy_slm.models import SlmPolicyRefreshResult
from src.domain.policy_slm.ports import SlmPolicyPort


class RefreshSlmPolicyUseCase:
    def __init__(self, policy: SlmPolicyPort) -> None:
        self._policy = policy

    def execute(self) -> SlmPolicyRefreshResult:
        return self._policy.refresh()
