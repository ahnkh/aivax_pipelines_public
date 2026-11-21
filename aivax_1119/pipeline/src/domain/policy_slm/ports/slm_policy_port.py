"""배포된 SLM 정책의 조회·cache 갱신 port."""

from abc import ABC, abstractmethod

from src.domain.policy_slm.models import SlmPolicyRefreshResult, SlmPolicySnapshot


class SlmPolicyProviderPort(ABC):
    @abstractmethod
    def get_policy(self) -> SlmPolicySnapshot:
        """rule과 group membership이 포함된 snapshot을 반환한다."""
        raise NotImplementedError


class SlmPolicyPort(SlmPolicyProviderPort):
    @abstractmethod
    def refresh(self) -> SlmPolicyRefreshResult:
        """SLM projection 하나를 읽어 실행 cache를 교체한다."""
        raise NotImplementedError
