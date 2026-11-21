"""multiple_filter HTTP controller."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.application.use_cases.input_analysis import InputAnalysisUseCase
from src.controllers.input_analysis.v1.dto import InputAnalysisItem
from src.controllers.input_analysis.v1.mapper import InputAnalysisMapper
from src.domain.analysis_log_delivery.errors import RetryableAnalysisLogDeliveryError
from src.domain.analysis_log_delivery.errors.consent_log_not_found_error import (
    ConsentLogNotFoundError,
)
from src.domain.analysis_log_merge.errors import (
    OutputLogProcessorUnavailableError,
)
from src.domain.inspection_input.errors import UnsupportedInputInspectorError


class InputAnalysisController:
    """HTTP 요청을 입력 분석 application service에 전달한다."""

    def __init__(
        self,
        use_case: InputAnalysisUseCase,
    ) -> None:
        self._use_case = use_case
        self.router = APIRouter()
        self.router.add_api_route(
            "/v1/filter/multiple_filter",
            self.analyze_input,
            methods=["POST"],
            name="filter_prompt_from_engine",
        )

    async def analyze_input(self, model_item: InputAnalysisItem):
        request = InputAnalysisMapper.to_request(model_item)
        try:
            outcome = await self._use_case.execute(request)
        except UnsupportedInputInspectorError as error:
            raise InputAnalysisMapper.to_unsupported_inspector_http_error(
                error
            ) from error
        except OutputLogProcessorUnavailableError as error:
            raise InputAnalysisMapper.to_unavailable_http_error(error) from error
        except ConsentLogNotFoundError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except RetryableAnalysisLogDeliveryError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

        return InputAnalysisMapper.to_success_response(
            request,
            outcome.analysis_result,
            debug_details_enabled=outcome.debug_details_enabled,
            log_evidence=(
                dict(outcome.log_evidence) if outcome.log_evidence is not None else None
            ),
        )
