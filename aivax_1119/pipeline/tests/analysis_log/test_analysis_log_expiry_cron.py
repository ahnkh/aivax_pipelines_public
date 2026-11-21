"""주기 실행 cron의 수명·직렬 실행·오류 복구 계약."""

import threading
import time
from unittest.mock import Mock

import pytest

from src.core.constants import ERR_FAIL, ERR_OK
from src.schedulers.analysis_log_merge_expiry_cron import (
    AnalysisLogMergeExpiryCron,
)


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_rejects_invalid_intervals(interval):
    with pytest.raises(ValueError):
        AnalysisLogMergeExpiryCron(Mock(), interval)


def test_periodic_execution_and_duplicate_start():
    twice = threading.Event()
    use_case = Mock()
    count = 0

    def sweep():
        nonlocal count
        count += 1
        if count >= 2:
            twice.set()

    use_case.execute.side_effect = sweep
    cron = AnalysisLogMergeExpiryCron(use_case, 0.01)
    try:
        assert cron.start() == ERR_OK
        thread = cron._thread
        assert cron.start() == ERR_OK
        assert cron._thread is thread
        assert twice.wait(1)
    finally:
        assert cron.shutdown() == ERR_OK
    assert not thread.is_alive()


def test_failure_does_not_stop_next_sweep():
    recovered = threading.Event()
    use_case = Mock()
    count = 0

    def sweep():
        nonlocal count
        count += 1
        if count == 1:
            raise RuntimeError("failure")
        recovered.set()

    use_case.execute.side_effect = sweep
    logger = Mock()
    cron = AnalysisLogMergeExpiryCron(use_case, 0.01, logger=logger)
    try:
        cron.start()
        assert recovered.wait(1)
    finally:
        cron.shutdown()
    logger.exception.assert_called_once_with("analysis log merge expiry failed")


def test_shutdown_wakes_long_wait_and_is_repeatable():
    use_case = Mock()
    cron = AnalysisLogMergeExpiryCron(use_case, 3600)
    assert cron.shutdown() == ERR_OK
    cron.start()
    start = time.monotonic()
    assert cron.shutdown() == ERR_OK
    assert time.monotonic() - start < 1
    assert cron.shutdown() == ERR_OK
    assert not cron._thread.is_alive()
    use_case.execute.assert_not_called()


def test_can_restart_after_shutdown():
    called = threading.Event()
    use_case = Mock()
    use_case.execute.side_effect = called.set
    cron = AnalysisLogMergeExpiryCron(use_case, 0.01)
    try:
        cron.start()
        assert called.wait(1)
        cron.shutdown()
        old_thread = cron._thread
        called.clear()
        cron.start()
        assert cron._thread is not old_thread
        assert called.wait(1)
    finally:
        cron.shutdown()


def test_slow_sweep_does_not_overlap_next_sweep():
    entered = threading.Event()
    release = threading.Event()
    overlap = threading.Event()
    calls = 0

    def sweep():
        nonlocal calls
        calls += 1
        if calls > 1:
            overlap.set()
        entered.set()
        release.wait(1)

    use_case = Mock()
    use_case.execute.side_effect = sweep
    cron = AnalysisLogMergeExpiryCron(use_case, 0.01)
    try:
        cron.start()
        assert entered.wait(1)
        assert not overlap.wait(0.05)
    finally:
        release.set()
        cron.shutdown()


def test_shutdown_reports_a_still_running_worker():
    logger = Mock()
    cron = AnalysisLogMergeExpiryCron(Mock(), 1, logger=logger)
    cron._thread = Mock()
    cron._thread.is_alive.return_value = True
    assert cron.shutdown() == ERR_FAIL
    assert cron._stop.is_set()
    cron._thread.join.assert_called_once_with(timeout=2)
    logger.error.assert_called_once()
