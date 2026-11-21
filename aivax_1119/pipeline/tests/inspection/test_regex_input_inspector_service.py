"""regex input inspector service 계약."""

from __future__ import annotations

import asyncio

from src.adapters.inspection_regex import PythonRegexInspectionAdapter
from src.domain.inspection_input.models import (
    InputAnalysisRequest,
    InputAnalysisUser,
    InputInspectionAction,
)
from src.domain.policy_regex.models import RegexPolicyRule, RegexPolicySnapshot
from src.application.use_cases.input_analysis import RegexInputInspector


class StaticRegexPolicy:
    def __init__(self, *rules: RegexPolicyRule) -> None:
        self.snapshot = RegexPolicySnapshot(rules=rules)

    def get_policy(self) -> RegexPolicySnapshot:
        return self.snapshot


def _service(*rules: RegexPolicyRule) -> RegexInputInspector:
    inspection = PythonRegexInspectionAdapter(StaticRegexPolicy(*rules))
    return RegexInputInspector(inspection)


def test_regex_service_maps_policy_evidence_and_masked_prompt() -> None:
    service = _service(
        RegexPolicyRule(
            id="mask",
            name="Password",
            expression=r"password=\w+",
            action="masking",
            category="credential",
        ),
        RegexPolicyRule(
            id="block",
            name="API key",
            expression=r"api-[a-z]+",
            action="block",
            category="credential",
        ),
    )

    result = asyncio.run(
        service.inspect(
            InputAnalysisRequest(
                requested_filter_names=(),
                prompt="password=abc api-secret",
                user=InputAnalysisUser(id="user-1", ai_service=1),
            )
        )
    )

    assert result.action is InputInspectionAction.BLOCK
    assert result.policy is not None and result.policy.id == "block"
    assert result.masked_prompt == ""
    assert "'Password,API key'" in result.block_message
    assert result.evidence == (
        {
            "policy_id": "mask",
            "policy_name": "Password",
            "action": "masking",
            "target": "prompt",
            "category": "credential",
            "scope": "default",
            "match": "password=abc",
            "start": 0,
            "end": 12,
        },
        {
            "policy_id": "block",
            "policy_name": "API key",
            "action": "block",
            "target": "prompt",
            "category": "credential",
            "scope": "default",
            "match": "api-secret",
            "start": 13,
            "end": 23,
        },
    )


def test_regex_service_preserves_prompt_size_limit_without_calling_regex_port() -> None:
    inspection = PythonRegexInspectionAdapter(StaticRegexPolicy())
    service = RegexInputInspector(inspection, prompt_size_limit=3)

    result = asyncio.run(
        service.inspect(
            InputAnalysisRequest(
                requested_filter_names=(),
                prompt="four",
            )
        )
    )

    assert result.action is InputInspectionAction.BLOCK
    assert result.policy is not None
    assert result.policy.name == "prompt size exceeds limit"
    assert result.masked_prompt == ""


def test_regex_service_applies_later_block_rule_without_masking_prompt() -> None:
    inspection = PythonRegexInspectionAdapter(
        StaticRegexPolicy(
            RegexPolicyRule(
                id="mask",
                name="first match",
                expression=r"127\.0\.0\.1",
                action="masking",
            ),
            RegexPolicyRule(
                id="block",
                name="ip policy",
                expression=r"127\.0\.0\.1",
                action="block",
            ),
        )
    )
    request = InputAnalysisRequest(
        requested_filter_names=(),
        prompt="127.0.0.1 오늘 회의 요약을 작성해 주세요.",
    )

    complete_scan = asyncio.run(
        RegexInputInspector(inspection).inspect(request),
    )

    assert complete_scan.action is InputInspectionAction.BLOCK
    assert complete_scan.masked_prompt == ""
    assert complete_scan.policy is not None
    assert complete_scan.policy.id == "block"


def test_regex_service_masks_prompt_for_masking_action() -> None:
    service = _service(
        RegexPolicyRule(
            id="mask",
            name="Password",
            expression=r"password=\w+",
            action="masking",
        ),
    )
    result = asyncio.run(
        service.inspect(
            InputAnalysisRequest(
                requested_filter_names=(),
                prompt="password=abc hello",
            )
        )
    )

    assert result.action is InputInspectionAction.MASKING
    assert result.masked_prompt == "[AIVAX MASKING] hello"
