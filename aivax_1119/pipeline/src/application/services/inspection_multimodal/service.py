"""멀티모달 분석 정책과 분석 엔진을 조합하는 공용 서비스."""

import asyncio
import logging

from src.domain.inspection_multimodal.models import (
    MultimodalInspectionOptions,
    MultimodalInspectionRequest,
    MultimodalInspectionResult,
    MultimodalInspectionTarget,
)
from src.domain.inspection_multimodal.ports import MultimodalInspectionPort
from src.domain.policy_multimodal.models import MultimodalPolicySnapshot
from src.domain.policy_multimodal.ports import MultimodalPolicyProviderPort


class MultimodalInspectionService:
    """현재 정책 또는 호출자가 지정한 정책으로 같은 분석 엔진을 실행한다."""

    def __init__(
        self,
        inspection: MultimodalInspectionPort,
        policy_provider: MultimodalPolicyProviderPort,
        default_options: MultimodalInspectionOptions,
        *,
        max_inflight: int = 4,
        acquire_timeout_seconds: float = 0,
    ) -> None:
        if max_inflight < 1:
            raise ValueError("max_inflight는 1 이상이어야 합니다.")
        if acquire_timeout_seconds < 0:
            raise ValueError("acquire_timeout_seconds는 0 이상이어야 합니다.")
        self._inspection = inspection
        self._policy_provider = policy_provider
        self._default_options = default_options
        self._semaphore = asyncio.Semaphore(max_inflight)
        self._acquire_timeout_seconds = float(acquire_timeout_seconds)
        self._logger = logging.getLogger("khan-python")

    async def inspect(
        self,
        targets: tuple[MultimodalInspectionTarget, ...],
        *,
        email: str = "",
        ai_service_id: int | str = 0,
        policy_override: MultimodalPolicySnapshot | None = None,
        options_override: MultimodalInspectionOptions | None = None,
    ) -> MultimodalInspectionResult:
        # 첨부 없는 프롬프트가 앞선 파일 검사 슬롯을 기다리지 않게 한다.
        if not targets:
            return MultimodalInspectionResult()
        if not await self._acquire_file_slot():
            # 슬롯을 병합 창보다 오래 기다리면 응답 수집이 프롬프트보다 먼저 만료된다.
            self._logger.error(
                "skip multimodal inspection because the file slot wait exceeded %.0fs",
                self._acquire_timeout_seconds,
            )
            return MultimodalInspectionResult(unavailable=True)
        try:
            return await self._inspection.inspect(
                MultimodalInspectionRequest(
                    targets=targets,
                    policy=(
                        policy_override
                        if policy_override is not None
                        else self._policy_provider.get_policy()
                    ),
                    options=options_override or self._default_options,
                    email=email,
                    ai_service_id=ai_service_id,
                ),
            )
        finally:
            self._semaphore.release()

    async def _acquire_file_slot(self) -> bool:
        if self._acquire_timeout_seconds <= 0:
            await self._semaphore.acquire()
            return True
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._acquire_timeout_seconds,
            )
        except TimeoutError:
            return False
        return True
