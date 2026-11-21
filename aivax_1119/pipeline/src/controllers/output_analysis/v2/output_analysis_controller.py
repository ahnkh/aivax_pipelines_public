"""V2 output_filter HTTP controller."""

from fastapi import APIRouter, Response

from src.application.use_cases.output_analysis import OutputAnalysisUseCase
from src.controllers.http_error_v2 import (
    FILTER_ERROR_RESPONSES_V2,
    filter_http_error_v2,
)
from src.controllers.output_analysis.v2.dto import OutputAnalysisRequestV2
from src.controllers.output_analysis.v2.mapper import OutputAnalysisMapperV2
from src.domain.analysis_log_delivery.errors import RetryableAnalysisLogDeliveryError
from src.domain.analysis_log_delivery.errors.consent_log_not_found_error import (
    ConsentLogNotFoundError,
)
from src.domain.analysis_log_merge.errors import OutputLogProcessorUnavailableError


class OutputAnalysisControllerV2:
    def __init__(self, use_case: OutputAnalysisUseCase) -> None:
        self._use_case = use_case
        self.router = APIRouter(tags=["Pipeline V2"])
        self.router.add_api_route(
            "/v2/filter/output_filter",
            self.record_output,
            methods=["POST"],
            status_code=204,
            response_class=Response,
            response_model=None,
            responses=FILTER_ERROR_RESPONSES_V2,
            summary="AI 출력 기록 V2",
        )

    async def record_output(self, item: OutputAnalysisRequestV2) -> Response:
        try:
            await self._use_case.execute(OutputAnalysisMapperV2.to_output_input(item))
        except OutputLogProcessorUnavailableError as error:
            raise filter_http_error_v2(404, "processor_unavailable", error) from error
        except ConsentLogNotFoundError as error:
            raise filter_http_error_v2(409, "consent_log_not_found", error) from error
        except RetryableAnalysisLogDeliveryError as error:
            raise filter_http_error_v2(503, "log_delivery_failed", error) from error
        return Response(status_code=204)
