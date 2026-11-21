"""AIVAX 프로세스 실행 모드 value object."""

from __future__ import annotations

from dataclasses import dataclass

from src.core.ddd.value_object import ValueObject
from src.domain.config_aivax.errors import AivaxConfigDomainError


@dataclass(frozen=True, slots=True, init=False)
class AivaxMode(ValueObject):
    value: str

    def __init__(self, value: str = "inline") -> None:
        object.__setattr__(self, "value", str(value).strip().lower())
        ValueObject.__init__(self)

    def validate(self) -> None:
        if self.value not in {"inline", "mirror", "proxy"}:
            raise AivaxConfigDomainError.invalid_mode(self.value)

    @property
    def is_mirror(self) -> bool:
        return self.value == "mirror"

    @property
    def is_proxy(self) -> bool:
        return self.value == "proxy"

    @property
    def is_inline(self) -> bool:
        return self.value == "inline"

    def __str__(self) -> str:
        return self.value
