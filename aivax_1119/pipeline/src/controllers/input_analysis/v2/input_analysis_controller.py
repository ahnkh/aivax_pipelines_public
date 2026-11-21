"""V2 multiple_filter HTTP controller."""

from fastapi import APIRouter

from src.application.use_cases.input_analysis import InputAnalysisUseCase
from src.controllers.http_error_v2 import (
    FILTER_ERROR_RESPONSES_V2,
    filter_http_error_v2,
)
from src.controllers.input_analysis.v2.dto import (
    InputAnalysisRequestV2,
    InputAnalysisResponseV2,
)
from src.controllers.input_analysis.v2.mapper import InputAnalysisMapperV2
from src.domain.analysis_log_delivery.errors import RetryableAnalysisLogDeliveryError
from src.domain.analysis_log_delivery.errors.consent_log_not_found_error import (
    ConsentLogNotFoundError,
)
from src.domain.analysis_log_merge.errors import OutputLogProcessorUnavailableError
from src.domain.inspection_input.errors import UnsupportedInputInspectorError


class InputAnalysisControllerV2:
    def __init__(self, use_case: InputAnalysisUseCase) -> None:
        self._use_case = use_case
        self.router = APIRouter(tags=["Pipeline V2"])
        self.router.add_api_route(
            "/v2/filter/multiple_filter",
            self.analyze_input,
            methods=["POST"],
            response_model=InputAnalysisResponseV2,
            responses=FILTER_ERROR_RESPONSES_V2,
            summary="입력 검사 V2",
        )

    async def analyze_input(
        self, item: InputAnalysisRequestV2
    ) -> InputAnalysisResponseV2:
        request = InputAnalysisMapperV2.to_input_request(item)
        try:
            outcome = await self._use_case.execute(request)
        except UnsupportedInputInspectorError as error:
            raise filter_http_error_v2(404, "unsupported_filter", error) from error
        except OutputLogProcessorUnavailableError as error:
            raise filter_http_error_v2(404, "processor_unavailable", error) from error
        except ConsentLogNotFoundError as error:
            raise filter_http_error_v2(409, "consent_log_not_found", error) from error
        except RetryableAnalysisLogDeliveryError as error:
            raise filter_http_error_v2(503, "log_delivery_failed", error) from error
        return InputAnalysisMapperV2.to_input_response(
            request,
            outcome.analysis_result,
        )
