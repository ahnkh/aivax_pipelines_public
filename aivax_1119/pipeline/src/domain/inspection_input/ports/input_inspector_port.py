"""각 입력 inspector 구현체가 만족할 실행 port."""

from abc import ABC, abstractmethod

from src.domain.inspection_input.models import (
    InputAnalysisRequest,
    InputInspectionResult,
)


class InputInspectorPort(ABC):
    @abstractmethod
    async def inspect(self, request: InputAnalysisRequest) -> InputInspectionResult:
        """하나의 입력 inspector를 실행하고 정규화된 결과를 반환한다."""
        raise NotImplementedError
