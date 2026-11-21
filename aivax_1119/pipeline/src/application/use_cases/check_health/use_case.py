"""Management와 동일한 상태 모델로 생존·의존성 준비 상태를 조회한다."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal

HealthStatus = Literal["ok", "error"]
HealthProbe = Callable[[], Awaitable[float | None]]


class HealthProbeError(RuntimeError):
    def __init__(self, message: str, *, used_percent: float | None = None) -> None:
        super().__init__(message)
        self.used_percent = used_percent


@dataclass(frozen=True, slots=True)
class DependencyHealth:
    status: HealthStatus
    latency_ms: int
    message: str | None = None
    used_percent: float | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: HealthStatus
    timestamp: str
    checks: dict[str, DependencyHealth] | None = None


class CheckHealthUseCase:
    def __init__(
        self,
        probes: Mapping[str, HealthProbe],
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        self._probes = dict(probes)
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def check_live() -> HealthReport:
        return HealthReport(status="ok", timestamp=CheckHealthUseCase._timestamp())

    async def check_ready(self) -> HealthReport:
        values = await asyncio.gather(
            *(self._measure(probe) for probe in self._probes.values())
        )
        checks = dict(zip(self._probes, values, strict=True))
        return HealthReport(
            status="ok" if all(item.status == "ok" for item in values) else "error",
            timestamp=self._timestamp(),
            checks=checks,
        )

    async def _measure(self, probe: HealthProbe) -> DependencyHealth:
        started = perf_counter()
        try:
            used_percent = await asyncio.wait_for(
                probe(), timeout=self._timeout_seconds
            )
            return DependencyHealth(
                status="ok",
                latency_ms=round((perf_counter() - started) * 1000),
                used_percent=used_percent,
            )
        except Exception as error:
            message = (
                f"헬스체크 타임아웃({self._timeout_seconds:g}초)"
                if isinstance(error, TimeoutError)
                else str(error)
            )
            return DependencyHealth(
                status="error",
                latency_ms=round((perf_counter() - started) * 1000),
                message=message,
                used_percent=(
                    error.used_percent if isinstance(error, HealthProbeError) else None
                ),
            )

    @staticmethod
    def _timestamp() -> str:
        return (
            datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
