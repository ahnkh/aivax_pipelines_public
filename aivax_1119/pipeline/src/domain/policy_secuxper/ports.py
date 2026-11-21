from abc import ABC, abstractmethod

from src.domain.policy_secuxper.models import (
    SecuxperPolicyRefreshResult,
    SecuxperPolicySnapshot,
)


class SecuxperPolicyPort(ABC):
    @abstractmethod
    def get_policy(self) -> SecuxperPolicySnapshot: ...
    @abstractmethod
    def refresh(self) -> SecuxperPolicyRefreshResult: ...
