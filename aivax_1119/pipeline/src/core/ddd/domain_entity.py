"""도메인 객체가 지켜야 할 공통 검증 계약."""

from abc import ABC, abstractmethod


class DomainEntity(ABC):
    """생성 직후 스스로의 불변식을 검증하는 도메인 객체 인터페이스."""

    @abstractmethod
    def validate(self) -> None:
        """도메인 불변식을 위반하면 ValueError를 발생시킨다."""
