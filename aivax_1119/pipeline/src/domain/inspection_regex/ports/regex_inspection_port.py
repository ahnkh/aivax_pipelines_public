"""정규식 정책 검사에 대한 도메인 port."""

from abc import ABC, abstractmethod

from src.domain.inspection_regex.models import (
    RegexInspectionRequest,
    RegexInspectionResult,
)


class RegexInspectionPort(ABC):
    @abstractmethod
    def inspect(self, request: RegexInspectionRequest) -> RegexInspectionResult:
        """정책 공급자가 제공하는 현재 정책으로 문자열을 검사한다."""
        raise NotImplementedError
