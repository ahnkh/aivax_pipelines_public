"""Pipeline 임시파일 정리 use case의 주기 실행과 thread lifecycle."""

import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.application.use_cases.cleanup_pipeline_files import CleanupPipelineFilesUseCase
from src.core.constants import ERR_OK


class CleanupCron:
    INTERVAL_SECONDS = 60

    def __init__(
        self,
        use_case: CleanupPipelineFilesUseCase,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._use_case = use_case
        self._logger = logger or logging.getLogger("khan-python")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_archive_at: datetime | None = None

    def start(self) -> int:
        if self._thread is not None and self._thread.is_alive():
            return ERR_OK
        self._stop.clear()
        self._next_archive_at = None
        self._thread = threading.Thread(
            target=self._run, name="aivax-pipeline-cleanup", daemon=True
        )
        self._thread.start()
        return ERR_OK

    def shutdown(self) -> int:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self._logger.warning("pipeline cleanup shutdown timed out")
        return ERR_OK

    def _run(self) -> None:
        # 시작 시에도 이전 프로세스가 남긴 임시파일을 정리한다.
        while not self._stop.is_set():
            try:
                self._use_case.execute(
                    archive_bulk=self._archive_due(datetime.now(ZoneInfo("Asia/Seoul")))
                )
            except Exception:
                self._logger.exception("pipeline cleanup failed")
            wait_seconds = self.INTERVAL_SECONDS
            if self._next_archive_at is not None:
                until_archive = (
                    self._next_archive_at - datetime.now(ZoneInfo("Asia/Seoul"))
                ).total_seconds()
                wait_seconds = min(wait_seconds, max(0, until_archive))
            if self._stop.wait(wait_seconds):
                break

    def _archive_due(self, now: datetime) -> bool:
        if self._next_archive_at is None:
            self._next_archive_at = self._following_archive_time(now)
            return False
        if now < self._next_archive_at:
            return False
        # 긴 작업으로 05분을 지나친 경우에도 한 번 실행한다.
        self._next_archive_at = self._following_archive_time(now)
        return True

    @staticmethod
    def _following_archive_time(now: datetime) -> datetime:
        target = now.replace(minute=5, second=0, microsecond=0)
        return target if target > now else target + timedelta(hours=1)
