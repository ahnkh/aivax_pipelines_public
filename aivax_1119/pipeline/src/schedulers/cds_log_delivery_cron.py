"""CDS 전송 대기 로그를 주기적으로 처리하는 scheduler."""

from __future__ import annotations

import logging
import math
import threading

from src.application.use_cases.send_pending_cds_logs import (
    SendPendingCdsLogsUseCase,
)
from src.core.constants import ERR_FAIL, ERR_OK


class CdsLogDeliveryCron:
    """워커별 CDS 배치 전송 thread의 수명주기를 관리한다."""

    def __init__(
        self,
        use_case: SendPendingCdsLogsUseCase,
        interval_seconds: float,
        *,
        shutdown_timeout_seconds: float = 15,
        logger: logging.Logger | None = None,
    ) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError(
                "CDS 전송 주기는 유한한 양수여야 합니다.",
            )

        if not math.isfinite(shutdown_timeout_seconds) or shutdown_timeout_seconds <= 0:
            raise ValueError(
                "CDS 종료 대기 시간은 유한한 양수여야 합니다.",
            )

        self._use_case = use_case
        self._interval_seconds = interval_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._logger = logger or logging.getLogger("khan-python")
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> int:
        """실행 중인 thread가 없을 때만 scheduler를 시작한다."""
        if self._thread is not None and self._thread.is_alive():
            return ERR_OK

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="aivax-cds-log-delivery",
            daemon=True,
        )
        self._thread.start()

        self._logger.debug(
            "CDS log delivery scheduler started interval=%ss",
            self._interval_seconds,
        )
        return ERR_OK

    def shutdown(self) -> int:
        """대기를 깨우고 실행 중인 전송 작업의 종료를 기다린다."""
        self._stop.set()

        if self._thread is None:
            return ERR_OK

        self._thread.join(
            timeout=self._shutdown_timeout_seconds,
        )

        if self._thread.is_alive():
            self._logger.error(
                "CDS log delivery scheduler shutdown timed out",
            )
            return ERR_FAIL

        return ERR_OK

    def _run(self) -> None:
        # 한 작업이 끝난 뒤 interval만큼 대기하므로 전송 작업은 겹치지 않는다.
        while not self._stop.wait(self._interval_seconds):
            try:
                self._use_case.execute()
            except Exception:
                # 유스케이스가 예외를 격리하지만 scheduler도 마지막 방어선을 둔다.
                self._logger.exception(
                    "CDS log delivery scheduler failed",
                )
