from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.schedulers import PolicyRefreshCron


def test_cron_executes_the_injected_use_case() -> None:
    use_case = Mock()
    cron = PolicyRefreshCron(use_case, 30, policy_name="regex")
    cron._stop.wait = Mock(side_effect=(False, True))

    cron._run()

    use_case.execute.assert_called_once_with()


def test_cron_logs_successful_refresh_at_debug_level() -> None:
    use_case = Mock()
    use_case.execute.return_value = SimpleNamespace(
        active_rule_count=3,
        rejected_rule_count=1,
    )
    logger = Mock()
    cron = PolicyRefreshCron(
        use_case,
        30,
        policy_name="regex",
        logger=logger,
    )
    cron._stop.wait = Mock(side_effect=(False, True))

    cron._run()

    logger.debug.assert_called_once_with(
        "policy refresh completed "
        "(policy=%s active_rule_count=%s rejected_rule_count=%s)",
        "regex",
        3,
        1,
    )


def test_start_and_shutdown_own_one_background_thread() -> None:
    use_case = Mock()
    thread = Mock()
    thread.is_alive.return_value = False
    with patch(
        "src.schedulers.policy_refresh_cron.threading.Thread",
        return_value=thread,
    ) as thread_type:
        cron = PolicyRefreshCron(use_case, 30, policy_name="slm")
        cron.start()
        assert cron.shutdown()

    thread_type.assert_called_once()
    thread.start.assert_called_once_with()
    thread.join.assert_called_once_with(timeout=31)
