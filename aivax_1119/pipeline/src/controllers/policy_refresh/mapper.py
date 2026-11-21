"""정책 refresh 요청 결과를 HTTP 응답 계약으로 변환한다."""

from src.application.use_cases.publish_policy_refresh_signal import (
    PolicyRefreshSignalReceipt,
)
from src.controllers.http_controller_mapper import HttpControllerMapper


class PolicyRefreshMapper(HttpControllerMapper):
    _API_COMMAND = "policy refresh"

    @classmethod
    def to_success_response(cls, result: PolicyRefreshSignalReceipt) -> dict:
        return cls.success_response(
            api_command=cls._API_COMMAND,
            result={"status": "accepted", "event_id": result.event_id},
        )
