"""멀티모달 정책 cache를 갱신하는 유스케이스."""

from src.domain.policy_multimodal.models import MultimodalPolicyRefreshResult
from src.domain.policy_multimodal.ports import MultimodalPolicyPort


class RefreshMultimodalPolicyUseCase:
    def __init__(self, policy: MultimodalPolicyPort) -> None:
        self._policy = policy

    def execute(self) -> MultimodalPolicyRefreshResult:
        return self._policy.refresh()
