"""정책 갱신 유스케이스를 주기적으로 실행하는 scheduler."""

from __future__ import annotations

import logging
import math
import threading
from typing import Protocol


class PolicyRefreshUseCase(Protocol):
    def execute(self) -> object: ...


class PolicyRefreshCron:
    """정책 종류와 무관하게 유스케이스 실행 주기만 관리한다."""

    def __init__(
        self,
        use_case: PolicyRefreshUseCase,
        interval_seconds: float = 15,
        *,
        policy_name: str,
        logger: logging.Logger | None = None,
    ) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("정책 갱신 주기는 유한한 양수여야 합니다.")
        self._use_case = use_case
        self._interval_seconds = interval_seconds
        self._policy_name = policy_name
        self._logger = logger or logging.getLogger("khan-python")
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"aivax-{self._policy_name}-policy-refresh",
            daemon=True,
        )
        self._thread.start()
        # 프로세스 시작 요약은 composition root가 한 줄로 기록한다.
        # scheduler는 독립 실행 시에도 과도한 INFO 로그를 남기지 않는다.
        self._logger.debug(
            "policy refresh scheduler started (policy=%s interval=%ss)",
            self._policy_name,
            self._interval_seconds,
        )

    def shutdown(self) -> bool:
        self._stop.set()
        if self._thread is None:
            return True
        self._thread.join(timeout=self._interval_seconds + 1)
        if self._thread.is_alive():
            self._logger.error(
                f"{self._policy_name} policy refresh cron shutdown timed out",
            )
            return False
        return True

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                result = self._use_case.execute()
                self._logger.debug(
                    "policy refresh completed "
                    "(policy=%s active_rule_count=%s rejected_rule_count=%s)",
                    self._policy_name,
                    getattr(result, "active_rule_count", None),
                    getattr(result, "rejected_rule_count", None),
                )
            except Exception:
                self._logger.exception(
                    f"{self._policy_name} policy refresh failed",
                )
