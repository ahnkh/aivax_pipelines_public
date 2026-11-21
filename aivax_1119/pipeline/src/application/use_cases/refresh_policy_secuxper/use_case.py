from src.domain.policy_secuxper.models import SecuxperPolicyRefreshResult
from src.domain.policy_secuxper.ports import SecuxperPolicyPort


class RefreshSecuxperPolicyUseCase:
    def __init__(self, policy: SecuxperPolicyPort):
        self._policy = policy

    def execute(self) -> SecuxperPolicyRefreshResult:
        return self._policy.refresh()
