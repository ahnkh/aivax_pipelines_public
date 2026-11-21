"""MainApp이 주입한 출력 분석 서비스를 호출하는 HTTP 컨트롤러."""

from fastapi import APIRouter, HTTPException, Request

from src.application.use_cases.output_analysis import OutputAnalysisUseCase
from src.controllers.output_analysis.v1.dto import OutputFilterItem
from src.controllers.output_analysis.v1.mapper import OutputAnalysisMapper
from src.domain.analysis_log_delivery.errors import RetryableAnalysisLogDeliveryError
from src.domain.analysis_log_delivery.errors.consent_log_not_found_error import (
    ConsentLogNotFoundError,
)
from src.domain.analysis_log_merge.errors import (
    OutputLogProcessorUnavailableError,
)


class OutputAnalysisController:
    def __init__(
        self,
        use_case: OutputAnalysisUseCase,
    ):
        self._use_case = use_case
        self.router = APIRouter()
        # 인스턴스 메서드를 등록하므로 요청마다 의존성을 조회하지 않는다.
        self.router.add_api_route(
            "/v1/filter/output_filter",
            self.write_ouput_response,
            methods=["POST"],
        )

    async def write_ouput_response(self, modelItem: OutputFilterItem, request: Request):
        try:
            result = await self._use_case.execute(
                OutputAnalysisMapper.to_output_log_input(modelItem),
            )
        except OutputLogProcessorUnavailableError as error:
            raise OutputAnalysisMapper.to_unavailable_http_error(error) from error
        except ConsentLogNotFoundError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except RetryableAnalysisLogDeliveryError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return OutputAnalysisMapper.to_success_response(result)
