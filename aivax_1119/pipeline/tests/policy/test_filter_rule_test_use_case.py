"""운영 snapshot을 변경하지 않는 filter rule test 유스케이스 계약."""

import asyncio
from unittest.mock import AsyncMock

from src.adapters.inspection_regex import PythonRegexInspectionAdapter
from src.application.services.inspection_slm import (
    SlmInspectionOptions,
    SlmInspectionService,
)
from src.application.use_cases.test_filter_rule import (
    FilterRuleTestRequest,
    FilterRuleTestUseCase,
)
from src.domain.inspection_slm.models import SlmInspectionRequest, SlmInspectionResult
from src.domain.inspection_slm.ports import SlmInspectionPort
from src.domain.policy_regex.models import RegexPolicyRule, RegexPolicySnapshot
from src.domain.policy_regex.ports import RegexPolicyProviderPort
from src.domain.policy_slm.models import SlmPolicyRule, SlmPolicySnapshot
from src.domain.policy_slm.ports import SlmPolicyProviderPort


class StubRegexPolicyProvider(RegexPolicyProviderPort):
    def __init__(self) -> None:
        self.snapshot = RegexPolicySnapshot(
            rules=(
                RegexPolicyRule(
                    id="live",
                    name="live",
                    expression="live-secret",
                    action="block",
                ),
            )
        )

    def get_policy(self) -> RegexPolicySnapshot:
        return self.snapshot


class UnsafeSlmInspection(SlmInspectionPort):
    async def inspect(self, request: SlmInspectionRequest) -> SlmInspectionResult:
        return SlmInspectionResult(
            raw_content="Unsafe",
            unsafe=True,
            evidence=[{"type": "EMAIL", "value": "user@example.com"}],
            use_default_policy_metadata=True,
        )


class StubSlmPolicyProvider(SlmPolicyProviderPort):
    def __init__(self) -> None:
        self.snapshot = SlmPolicySnapshot(
            rules=(
                SlmPolicyRule(
                    id="live-slm",
                    name="live SLM",
                    action="block",
                ),
            )
        )

    def get_policy(self) -> SlmPolicySnapshot:
        return self.snapshot


def _use_case() -> tuple[
    FilterRuleTestUseCase,
    StubRegexPolicyProvider,
    StubSlmPolicyProvider,
]:
    regex_policy = StubRegexPolicyProvider()
    slm_policy = StubSlmPolicyProvider()
    slm_service = SlmInspectionService(
        UnsafeSlmInspection(),
        slm_policy,
        SlmInspectionOptions(),
    )
    return (
        FilterRuleTestUseCase(
            PythonRegexInspectionAdapter(regex_policy),
            AsyncMock(),
            slm_service,
        ),
        regex_policy,
        slm_policy,
    )


def test_regex_rule_is_tested_without_replacing_live_policy() -> None:
    use_case, regex_policy, _ = _use_case()
    live_snapshot = regex_policy.snapshot

    result = asyncio.run(
        use_case.execute(
            FilterRuleTestRequest(
                prompt="temporary-secret",
                regex_policy=RegexPolicySnapshot(
                    rules=(
                        RegexPolicyRule(
                            id="temporary",
                            name="temporary",
                            expression="temporary-secret",
                            action="masking",
                        ),
                    ),
                ),
            )
        )
    )

    assert result.regex_result is not None
    assert result.regex_result.action == "masking"
    assert result.regex_result.content == "[AIVAX MASKING]"
    assert result.slm_result is None
    assert regex_policy.snapshot is live_snapshot


def test_slm_test_action_overrides_live_policy_without_replacing_it() -> None:
    use_case, _, slm_policy = _use_case()
    live_snapshot = slm_policy.snapshot

    result = asyncio.run(
        use_case.execute(
            FilterRuleTestRequest(
                prompt="unsafe prompt",
                slm_policy=SlmPolicySnapshot(
                    rules=(
                        SlmPolicyRule(
                            id="temporary-slm",
                            name="temporary SLM",
                            action="masking",
                        ),
                    ),
                ),
            )
        )
    )

    assert result.regex_result is None
    assert result.slm_result is not None
    assert result.slm_result.action == "masking"
    assert result.slm_result.block_message is not None
    assert "user@example.com" in result.slm_result.block_message
    assert slm_policy.snapshot is live_snapshot


def test_slm_accept_action_is_returned_as_detected() -> None:
    use_case, _, _ = _use_case()

    result = asyncio.run(
        use_case.execute(
            FilterRuleTestRequest(
                prompt="unsafe prompt",
                slm_policy=SlmPolicySnapshot(
                    rules=(
                        SlmPolicyRule(
                            id="detect-slm",
                            name="detect SLM",
                            action="accept",
                        ),
                    ),
                ),
            )
        )
    )

    assert result.slm_result is not None
    assert result.slm_result.action == "accept"
    assert result.slm_result.policy_id == "detect-slm"
    assert result.slm_result.policy_name == "detect SLM"
    assert result.slm_result.evidence
    assert result.final_decision is not None
    assert result.final_decision.action == "accept"


def test_filters_run_in_fixed_order_until_one_blocks() -> None:
    use_case, _, _ = _use_case()

    result = asyncio.run(
        use_case.execute(
            FilterRuleTestRequest(
                prompt="temporary-secret",
                regex_policy=RegexPolicySnapshot(
                    rules=(
                        RegexPolicyRule(
                            id="temporary",
                            name="temporary",
                            expression="temporary-secret",
                            action="block",
                        ),
                    ),
                ),
                slm_policy=SlmPolicySnapshot(
                    rules=(
                        SlmPolicyRule(
                            id="temporary-slm",
                            name="temporary SLM",
                            action="block",
                        ),
                    ),
                ),
            )
        )
    )

    assert result.regex_result is not None
    assert result.regex_result.action == "block"
    assert result.slm_result is None
