import threading
import time
from unittest.mock import Mock

from src.application.use_cases.send_pending_cds_logs import (
    SendPendingCdsLogsUseCase,
)
from src.core.constants import ERR_OK
from src.schedulers import CdsLogDeliveryCron


def test_executes_periodically_without_duplicate_thread() -> None:
    executed = threading.Event()
    use_case = Mock(spec=SendPendingCdsLogsUseCase)
    use_case.execute.side_effect = executed.set

    cron = CdsLogDeliveryCron(
        use_case,
        0.01,
        shutdown_timeout_seconds=1,
    )

    try:
        assert cron.start() == ERR_OK
        thread = cron._thread

        assert cron.start() == ERR_OK
        assert cron._thread is thread
        assert executed.wait(1)
    finally:
        assert cron.shutdown() == ERR_OK

    assert not thread.is_alive()


def test_shutdown_wakes_long_interval_immediately() -> None:
    use_case = Mock(spec=SendPendingCdsLogsUseCase)
    cron = CdsLogDeliveryCron(
        use_case,
        3600,
        shutdown_timeout_seconds=1,
    )

    cron.start()
    started_at = time.monotonic()

    assert cron.shutdown() == ERR_OK
    assert time.monotonic() - started_at < 1
    use_case.execute.assert_not_called()
