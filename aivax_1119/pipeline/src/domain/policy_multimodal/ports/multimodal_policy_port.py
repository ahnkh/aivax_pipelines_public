"""배포된 멀티모달 정책의 조회·cache 갱신 port."""

from abc import ABC, abstractmethod

from src.domain.policy_multimodal.models import (
    MultimodalPolicyRefreshResult,
    MultimodalPolicySnapshot,
)


class MultimodalPolicyProviderPort(ABC):
    @abstractmethod
    def get_policy(self) -> MultimodalPolicySnapshot:
        """rule, group membership, file 설정이 포함된 snapshot을 반환한다.

        멀티모달 검사는 masking을 지원하지 않으므로 provider는 원본 action이
        masking인 정책을 block으로 변환해서 제공해야 한다.
        """
        raise NotImplementedError


class MultimodalPolicyPort(MultimodalPolicyProviderPort):
    @abstractmethod
    def refresh(self) -> MultimodalPolicyRefreshResult:
        """Multimodal projection 하나를 읽어 실행 cache를 교체한다."""
        raise NotImplementedError
