"""AIVAX 실행 설정 조회 port."""

from abc import ABC, abstractmethod

from src.domain.config_aivax.models import AivaxConfig


class AivaxConfigPort(ABC):
    @abstractmethod
    def get_config(self) -> AivaxConfig:
        """현재 프로세스에 적용할 AIVAX 설정을 반환한다."""
        raise NotImplementedError
