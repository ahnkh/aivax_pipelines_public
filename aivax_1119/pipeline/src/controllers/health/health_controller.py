from fastapi import APIRouter, Response

from src.application.use_cases.check_health import CheckHealthUseCase
from src.controllers.health.dto import HealthResponse, ProcessStatusResponse
from src.controllers.health.mapper import HealthMapper


class HealthController:
    def __init__(self, use_case: CheckHealthUseCase) -> None:
        self._use_case = use_case
        self.router = APIRouter(tags=["Health"])
        # Management's existing /v1 contract and Pipeline's smoke script /.
        for path in ("/", "/v1"):
            self.router.add_api_route(
                path,
                self.check_status,
                methods=["GET"],
                response_model=ProcessStatusResponse,
            )
        for prefix in ("/v1/health", "/api/v1/health"):
            for suffix, handler in (
                ("", self.check_live),
                ("/live", self.check_live),
                ("/ready", self.check_ready),
            ):
                self.router.add_api_route(
                    prefix + suffix,
                    handler,
                    methods=["GET"],
                    response_model=HealthResponse,
                    response_model_exclude_none=True,
                    include_in_schema=prefix == "/v1/health",
                )

    def check_status(self) -> ProcessStatusResponse:
        return ProcessStatusResponse(status=self._use_case.check_live().status == "ok")

    def check_live(self) -> HealthResponse:
        return HealthMapper.to_response(self._use_case.check_live())

    async def check_ready(self, response: Response) -> HealthResponse:
        report = await self._use_case.check_ready()
        if report.status != "ok":
            response.status_code = 503
        return HealthMapper.to_response(report)
