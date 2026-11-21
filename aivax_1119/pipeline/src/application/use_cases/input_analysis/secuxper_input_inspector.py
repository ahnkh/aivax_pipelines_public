from src.application.services.inspection_secuxper.service import (
    SecuxperInspectionService,
)
from src.domain.inspection_input.models import (
    InputAnalysisRequest,
    InputInspectionResult,
)
from src.domain.inspection_input.ports import InputInspectorPort


class SecuxperInputInspector(InputInspectorPort):
    def __init__(self, service: SecuxperInspectionService):
        self._service = service

    async def inspect(self, request: InputAnalysisRequest) -> InputInspectionResult:
        return await self._service.inspect(request)
