"""구현 방식과 무관한 멀티모달 분석 엔진 계약."""

from abc import ABC, abstractmethod

from src.domain.inspection_multimodal.models import (
    MultimodalInspectionRequest,
    MultimodalInspectionResult,
)


class MultimodalInspectionPort(ABC):
    @abstractmethod
    async def inspect(
        self,
        request: MultimodalInspectionRequest,
    ) -> MultimodalInspectionResult:
        """첨부파일을 분석하고 정규화된 결과를 반환한다."""
        raise NotImplementedError

    @abstractmethod
    def expire_temporary_evidence(self, directory: str) -> int:
        """30분이 지난 증적 변환 결과를 정리하고 삭제 항목 수를 반환한다."""
        raise NotImplementedError
