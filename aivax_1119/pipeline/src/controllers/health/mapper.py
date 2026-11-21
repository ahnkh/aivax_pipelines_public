from src.application.use_cases.check_health import HealthReport
from src.controllers.health.dto import DependencyHealthResponse, HealthResponse


class HealthMapper:
    @staticmethod
    def to_response(report: HealthReport) -> HealthResponse:
        return HealthResponse(
            status=report.status,
            timestamp=report.timestamp,
            checks=(
                {
                    name: DependencyHealthResponse(
                        status=check.status,
                        latencyMs=check.latency_ms,
                        message=check.message,
                        usedPercent=check.used_percent,
                    )
                    for name, check in report.checks.items()
                }
                if report.checks is not None
                else None
            ),
        )
