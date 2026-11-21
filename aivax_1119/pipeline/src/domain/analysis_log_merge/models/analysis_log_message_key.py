"""입력 분석 로그와 출력 분석 로그를 연결하는 메시지 키 Value Object."""

from __future__ import annotations

from dataclasses import dataclass

from src.core.ddd.value_object import ValueObject
from src.domain.analysis_log_merge.errors import AnalysisLogMergeDomainError


@dataclass(frozen=True, slots=True, init=False)
class AnalysisLogMessageKey(ValueObject):
    """동일한 요청의 INPUT/OUTPUT 분석 로그를 식별하는 불변 키."""

    value: str

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "value", value)
        ValueObject.__init__(self)

    def validate(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise AnalysisLogMergeDomainError.empty_message_key(self.value)

    def __str__(self) -> str:
        return self.value
