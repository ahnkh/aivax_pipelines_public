"""분석 로그 병합 대기 항목의 만료 처리를 구동하는 scheduler."""

import logging
import math
import threading

from src.core.constants import ERR_FAIL, ERR_OK
from src.application.use_cases.expire_analysis_log_merge import (
    ExpireAnalysisLogMergeUseCase,
)


class AnalysisLogMergeExpiryCron:
    def __init__(
        self,
        use_case: ExpireAnalysisLogMergeUseCase,
        interval_seconds: float,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("만료 처리 실행 주기는 유한한 양수여야 합니다.")
        self._use_case = use_case
        self._interval_seconds = interval_seconds
        self._logger = (
            logger if logger is not None else logging.getLogger("khan-python")
        )
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> int:
        """MainApp 초기화 시 호출한다. 실행 중이면 추가 스레드를 만들지 않는다."""
        if self._thread is not None and self._thread.is_alive():
            return ERR_OK
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="aivax-analysis-log-merge-expiry",
            daemon=True,
        )
        self._thread.start()
        # 프로세스 시작 요약은 composition root가 한 줄로 기록한다.
        self._logger.debug(
            "analysis log merge expiry scheduler started (interval=%ss)",
            self._interval_seconds,
        )
        return ERR_OK

    def shutdown(self) -> int:
        """대기를 깨우고 실행 중인 sweep의 종료를 기다린다."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_seconds + 1)
            if self._thread.is_alive():
                # 실행 중인 서비스가 사용하는 outbox/연결을 먼저 닫지 않도록 알린다.
                self._logger.error("analysis log merge expiry shutdown timed out")
                return ERR_FAIL
        return ERR_OK

    def _run(self) -> None:
        # 대기 시간에는 CPU를 점유하지 않는다. Sweep 완료 후 다음 주기를 기다리므로 겹치지 않는다.
        while not self._stop.wait(self._interval_seconds):
            try:
                self._use_case.execute()
            except Exception:
                self._logger.exception("analysis log merge expiry failed")
