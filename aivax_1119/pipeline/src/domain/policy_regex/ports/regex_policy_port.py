"""배포된 regex 정책의 조회·cache 갱신 port."""

from abc import ABC, abstractmethod

from src.domain.policy_regex.models import (
    RegexPolicyRefreshResult,
    RegexPolicySnapshot,
)


class RegexPolicyProviderPort(ABC):
    @abstractmethod
    def get_policy(self) -> RegexPolicySnapshot | None:
        """rule과 group membership이 포함된 실행 snapshot을 반환한다."""
        raise NotImplementedError


class RegexPolicyPort(RegexPolicyProviderPort):
    @abstractmethod
    def refresh(self) -> RegexPolicyRefreshResult:
        """Regex projection 하나를 읽어 실행 cache를 교체한다."""
        raise NotImplementedError
