"""SLM 추론 외부 시스템에 대한 도메인 port."""

from abc import ABC, abstractmethod

from src.domain.inspection_slm.models import SlmInspectionRequest, SlmInspectionResult


class SlmInspectionPort(ABC):
    @abstractmethod
    async def inspect(self, request: SlmInspectionRequest) -> SlmInspectionResult:
        """프롬프트를 검사한다. 호출 실패는 SlmInspectionError로 전달한다."""
        raise NotImplementedError
