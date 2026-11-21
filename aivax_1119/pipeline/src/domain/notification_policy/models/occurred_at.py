"""정책 알림 발생 시각 Value Object."""

from __future__ import annotations

from dataclasses import dataclass

from src.core.ddd.value_object import ValueObject
from src.core.time import TimeNormalizer
from src.domain.notification_policy.errors import PolicyNotificationDomainError


@dataclass(frozen=True, slots=True, init=False)
class OccurredAt(ValueObject):
    """시간대가 포함된 발생 시각과 초 단위 중복 제거 값을 함께 제공한다."""

    value: str

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "value", value)
        ValueObject.__init__(self)

    def validate(self) -> None:
        try:
            TimeNormalizer.to_utc_second(self.value)
        except (TypeError, ValueError):
            raise PolicyNotificationDomainError.invalid_occurred_at(
                self.value
            ) from None

    @classmethod
    def now(cls) -> "OccurredAt":
        """현재 UTC 시각으로 발생 시각을 만든다."""
        return cls(TimeNormalizer.utc_now_iso())

    @property
    def utc_second(self) -> str:
        """중복 제거 해시에 사용할 UTC 초 단위 문자열을 반환한다."""
        return TimeNormalizer.to_utc_second(self.value)

    @property
    def utc_iso(self) -> str:
        """Management Stream에 전달할 UTC ISO-8601 시각을 반환한다."""
        return TimeNormalizer.to_utc_iso(self.value)
