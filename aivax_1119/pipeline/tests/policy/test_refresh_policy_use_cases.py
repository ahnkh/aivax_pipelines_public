from unittest.mock import Mock

from src.application.use_cases.refresh_policy_multimodal import (
    RefreshMultimodalPolicyUseCase,
)
from src.application.use_cases.refresh_policy_regex import RefreshRegexPolicyUseCase
from src.application.use_cases.refresh_policy_slm import RefreshSlmPolicyUseCase


def test_policy_refresh_use_cases_delegate_to_their_policy_port() -> None:
    for use_case_type in (
        RefreshRegexPolicyUseCase,
        RefreshMultimodalPolicyUseCase,
        RefreshSlmPolicyUseCase,
    ):
        policy = Mock()
        policy.refresh.return_value = object()

        assert use_case_type(policy).execute() is policy.refresh.return_value
        policy.refresh.assert_called_once_with()
