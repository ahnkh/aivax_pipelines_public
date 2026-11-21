"""배포된 필터 정책 cache를 즉시 갱신하는 유스케이스."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from anyio.to_thread import run_sync

from src.domain.policy_multimodal.ports import MultimodalPolicyPort
from src.domain.policy_regex.ports import RegexPolicyPort
from src.domain.policy_slm.ports import SlmPolicyPort


@dataclass(frozen=True, slots=True)
class PolicyRefreshExecution:
    status: Literal["applied", "failed"]
    active_rule_count: int | None = None
    rejected_rule_count: int | None = None


@dataclass(frozen=True, slots=True)
class FilterPolicyRefreshResult:
    regex_policy: PolicyRefreshExecution
    multimodal_policy: PolicyRefreshExecution
    slm_policy: PolicyRefreshExecution


class RefreshFilterPolicyUseCase:
    """시그널을 받으면 Valkey 기반 정책 cache를 즉시 갱신한다."""

    def __init__(
        self,
        regex_policy: RegexPolicyPort,
        multimodal_policy: MultimodalPolicyPort,
        slm_policy: SlmPolicyPort,
        *,
        logger: Any,
    ) -> None:
        self._regex_policy = regex_policy
        self._multimodal_policy = multimodal_policy
        self._slm_policy = slm_policy
        self._logger = logger

    async def execute(self) -> FilterPolicyRefreshResult:
        regex_result = await self._refresh_regex_policy()
        multimodal_result = await self._refresh_multimodal_policy()
        slm_result = await self._refresh_slm_policy()
        return FilterPolicyRefreshResult(
            regex_policy=regex_result,
            multimodal_policy=multimodal_result,
            slm_policy=slm_result,
        )

    async def _refresh_regex_policy(self) -> PolicyRefreshExecution:
        try:
            result = await run_sync(self._regex_policy.refresh)
            return PolicyRefreshExecution(
                status="applied",
                active_rule_count=result.active_rule_count,
                rejected_rule_count=result.rejected_rule_count,
            )
        except Exception:
            self._logger.exception("regex policy signal refresh failed")
            return PolicyRefreshExecution(status="failed")

    async def _refresh_multimodal_policy(self) -> PolicyRefreshExecution:
        try:
            result = await run_sync(self._multimodal_policy.refresh)
            return PolicyRefreshExecution(
                status="applied",
                active_rule_count=result.active_rule_count,
            )
        except Exception:
            self._logger.exception("multimodal policy signal refresh failed")
            return PolicyRefreshExecution(status="failed")

    async def _refresh_slm_policy(self) -> PolicyRefreshExecution:
        try:
            result = await run_sync(self._slm_policy.refresh)
            return PolicyRefreshExecution(
                status="applied",
                active_rule_count=result.active_rule_count,
            )
        except Exception:
            self._logger.exception("slm policy signal refresh failed")
            return PolicyRefreshExecution(status="failed")
