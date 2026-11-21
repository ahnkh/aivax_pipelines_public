"""선택적으로 설치하는 입력 선행 가드 계약."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.domain.inspection_input.models import InputAnalysisRequest, InputAnalysisResult


@dataclass(frozen=True, slots=True)
class InputGuardResult:
    request: InputAnalysisRequest
    blocked_result: InputAnalysisResult | None = None
    log_evidence: Mapping[str, Any] | None = None

    def apply(self, result: InputAnalysisResult) -> InputAnalysisResult:
        """통과한 가드의 조회 결과를 분석 결과에 반영한다."""
        return result


class InputGuardPort(ABC):
    @abstractmethod
    async def check(
        self,
        request: InputAnalysisRequest,
        *,
        original_document: dict[str, Any] | None = None,
    ) -> InputGuardResult:
        """선행 검사와 필요한 차단 후처리를 완료한다."""
        raise NotImplementedError
