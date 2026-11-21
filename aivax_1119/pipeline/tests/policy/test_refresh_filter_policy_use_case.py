import asyncio
import threading
import unittest
from unittest.mock import Mock

from src.application.use_cases.refresh_filter_policy import (
    RefreshFilterPolicyUseCase,
)
from src.domain.policy_multimodal.models import MultimodalPolicyRefreshResult
from src.domain.policy_regex.models import RegexPolicyRefreshResult
from src.domain.policy_slm.models import SlmPolicyRefreshResult


class RefreshFilterPolicyUseCaseTest(unittest.TestCase):
    def test_refreshes_every_policy_and_returns_existing_contract(self) -> None:
        event_loop_thread_id = threading.get_ident()
        refresh_thread_ids: list[int] = []
        regex_policy = Mock()
        regex_policy.refresh.side_effect = lambda: (
            refresh_thread_ids.append(threading.get_ident())
            or RegexPolicyRefreshResult(2, 1)
        )
        multimodal_policy = Mock()
        multimodal_policy.refresh.return_value = MultimodalPolicyRefreshResult(3)
        slm_policy = Mock()
        slm_policy.refresh.return_value = SlmPolicyRefreshResult(1)
        result = asyncio.run(
            RefreshFilterPolicyUseCase(
                regex_policy,
                multimodal_policy,
                slm_policy,
                logger=Mock(),
            ).execute()
        )

        self.assertEqual(result.regex_policy.status, "applied")
        self.assertEqual(result.regex_policy.active_rule_count, 2)
        self.assertEqual(result.regex_policy.rejected_rule_count, 1)
        self.assertEqual(result.multimodal_policy.active_rule_count, 3)
        self.assertEqual(result.slm_policy.active_rule_count, 1)
        self.assertNotEqual(refresh_thread_ids, [event_loop_thread_id])
        regex_policy.refresh.assert_called_once_with()
        multimodal_policy.refresh.assert_called_once_with()
        slm_policy.refresh.assert_called_once_with()

    def test_one_failure_does_not_cancel_other_policy_refreshes(self) -> None:
        logger = Mock()
        regex_policy = Mock()
        regex_policy.refresh.side_effect = RuntimeError("db unavailable")
        multimodal_policy = Mock()
        multimodal_policy.refresh.return_value = MultimodalPolicyRefreshResult(3)
        slm_policy = Mock()
        slm_policy.refresh.return_value = SlmPolicyRefreshResult(1)
        result = asyncio.run(
            RefreshFilterPolicyUseCase(
                regex_policy,
                multimodal_policy,
                slm_policy,
                logger=logger,
            ).execute()
        )

        self.assertEqual(result.regex_policy.status, "failed")
        self.assertIsNone(result.regex_policy.active_rule_count)
        self.assertEqual(result.multimodal_policy.status, "applied")
        self.assertEqual(result.slm_policy.status, "applied")
        logger.exception.assert_called_once_with(
            "regex policy signal refresh failed",
        )
