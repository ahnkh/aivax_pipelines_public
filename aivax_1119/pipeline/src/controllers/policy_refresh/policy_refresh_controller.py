"""모든 worker에 정책 cache refresh를 요청하는 HTTP controller."""

from anyio.to_thread import run_sync
from fastapi import APIRouter

from src.application.use_cases.publish_policy_refresh_signal import (
    PublishPolicyRefreshSignalUseCase,
)
from src.controllers.policy_refresh.dto import PolicyRefreshRequest
from src.controllers.policy_refresh.mapper import PolicyRefreshMapper


class PolicyRefreshController:
    def __init__(self, use_case: PublishPolicyRefreshSignalUseCase) -> None:
        self._use_case = use_case
        self.router = APIRouter()
        self.router.add_api_route(
            "/v1/filter/policy-refresh",
            self.request_refresh,
            methods=["POST"],
            name="requestPolicyRefresh",
        )
        # Management은 Stream을 직접 발행한다. 기존 외부 호출자만 호환한다.
        self.router.add_api_route(
            "/v1/filter/policy-signal",
            self.request_refresh,
            methods=["POST"],
            name="requestPolicyRefreshLegacy",
            include_in_schema=False,
        )

    async def request_refresh(self, model_item: PolicyRefreshRequest) -> dict:
        result = await run_sync(self._use_case.execute)
        return PolicyRefreshMapper.to_success_response(result)
