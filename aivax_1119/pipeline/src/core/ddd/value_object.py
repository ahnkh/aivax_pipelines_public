"""도메인 Value Object의 공통 기반 객체."""

from abc import ABC, abstractmethod


class ValueObject(ABC):
    """생성 직후 자신의 불변식을 검증하는 불변 값 객체 기반 클래스."""

    def __init__(self) -> None:
        self.validate()

    @abstractmethod
    def validate(self) -> None:
        """값 객체 불변식을 위반하면 DomainException을 발생시킨다."""
