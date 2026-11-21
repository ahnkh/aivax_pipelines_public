from abc import ABC, abstractmethod

from src.domain.inspection_secuxper.models import (
    SecuxperInspectionRequest,
    SecuxperInspectionResult,
)


class SecuxperInspectionPort(ABC):
    @abstractmethod
    async def inspect(
        self, request: SecuxperInspectionRequest
    ) -> SecuxperInspectionResult: ...
