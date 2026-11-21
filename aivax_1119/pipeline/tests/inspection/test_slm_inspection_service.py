"""SLM 공용 application service의 정책 적용 계약."""

import asyncio
from dataclasses import dataclass, replace

import pytest

from src.application.services.inspection_slm import (
    SlmInspectionOptions,
    SlmInspectionService,
)
from src.domain.inspection_slm.errors import SlmInspectionError
from src.domain.inspection_slm.models import (
    SlmInspectionRequest,
    SlmInspectionResult,
)
from src.domain.policy_slm.models import SlmPolicyRule, SlmPolicySnapshot


@dataclass
class StubInspection:
    result: SlmInspectionResult

    async def inspect(
        self,
        request: SlmInspectionRequest,
    ) -> SlmInspectionResult:
        return self.result


@dataclass
class StubPolicyProvider:
    snapshot: SlmPolicySnapshot

    def get_policy(self) -> SlmPolicySnapshot:
        return self.snapshot


def _service(
    inspection: SlmInspectionResult,
    *rules: SlmPolicyRule,
) -> SlmInspectionService:
    return SlmInspectionService(
        StubInspection(inspection),
        StubPolicyProvider(SlmPolicySnapshot(rules=rules)),
        SlmInspectionOptions(),
    )


def test_unsafe_result_uses_matching_policy_action_and_metadata() -> None:
    service = _service(
        SlmInspectionResult(
            unsafe=True,
            use_default_policy_metadata=True,
            evidence=[
                {
                    "kind": "pii",
                    "type": "EMAIL",
                    "value": "user@example.com",
                }
            ],
        ),
        SlmPolicyRule(
            id="slm-policy",
            name="Email masking",
            action="masking",
            detection_kind="pii",
            detection_type="EMAIL",
            category="safety",
        ),
    )

    result = asyncio.run(service.inspect("user@example.com"))

    assert result.action == "masking"
    assert result.policy is not None
    assert result.policy.id == "slm-policy"
    assert result.policy.name == "Email masking"
    assert result.evidence == (
        {
            "kind": "pii",
            "type": "EMAIL",
            "value": "user@example.com",
            "policy_id": "slm-policy",
            "policy_name": "Email masking",
            "action": "masking",
            "category": "safety",
            "scope": "default",
        },
    )


def test_model_policy_metadata_is_preserved() -> None:
    service = _service(
        SlmInspectionResult(
            unsafe=True,
            policy_name="IP address",
            policy_category="SLM analysis",
            evidence=[
                {
                    "kind": "pii",
                    "type": "IP_ADDRESS",
                    "value": "127.0.0.1",
                }
            ],
        ),
        SlmPolicyRule(
            id="default",
            name="default",
            action="block",
            detection_kind="pii",
            detection_type="IP_ADDRESS",
        ),
    )

    result = asyncio.run(service.inspect("127.0.0.1"))

    assert result.action == "block"
    assert result.policy is not None
    assert result.policy.name == "IP address"
    assert result.policy.category == "SLM analysis"


def test_strongest_action_is_selected_across_detections() -> None:
    service = _service(
        SlmInspectionResult(
            unsafe=True,
            use_default_policy_metadata=True,
            evidence=[
                {
                    "kind": "pii",
                    "type": "EMAIL",
                    "value": "user@example.com",
                },
                {
                    "kind": "guardrail",
                    "type": "PROMPT_INJECTION",
                    "value": "",
                },
            ],
        ),
        SlmPolicyRule(
            id="email",
            name="Email",
            action="masking",
            detection_kind="pii",
            detection_type="EMAIL",
        ),
        SlmPolicyRule(
            id="injection",
            name="Prompt injection",
            action="block",
            detection_kind="guardrail",
            detection_type="PROMPT_INJECTION",
        ),
    )

    result = asyncio.run(service.inspect("user@example.com"))

    assert result.action == "block"
    assert result.policy is not None
    assert result.policy.id == "injection"
    assert tuple(item["action"] for item in result.evidence) == (
        "masking",
        "block",
    )


def test_policy_is_matched_for_each_repeated_detection_value() -> None:
    service = _service(
        SlmInspectionResult(
            unsafe=True,
            use_default_policy_metadata=True,
            evidence=[
                {
                    "kind": "pii",
                    "type": "EMAIL",
                    "value": "first@example.com",
                },
                {
                    "kind": "pii",
                    "type": "EMAIL",
                    "value": "second@example.com",
                },
            ],
        ),
        SlmPolicyRule(
            id="email",
            name="Email",
            action="masking",
            detection_kind="pii",
            detection_type="EMAIL",
        ),
    )

    result = asyncio.run(
        service.inspect(
            "first@example.com second@example.com",
        )
    )

    assert result.action == "masking"
    assert len(result.evidence) == 2
    assert all(item["policy_id"] == "email" for item in result.evidence)
    assert all(item["action"] == "masking" for item in result.evidence)


def test_unmatched_detection_has_blank_action() -> None:
    service = _service(
        SlmInspectionResult(
            unsafe=True,
            evidence=[
                {
                    "kind": "pii",
                    "type": "EMAIL",
                    "value": "user@example.com",
                }
            ],
        ),
    )

    result = asyncio.run(service.inspect("user@example.com"))

    assert result.action == ""
    assert result.policy is None
    assert result.evidence[0]["action"] == ""


def test_force_block_is_used_only_when_no_policy_exists() -> None:
    service = _service(
        SlmInspectionResult(
            unsafe=True,
            force_block_without_policy=True,
        )
    )

    result = asyncio.run(service.inspect("unsafe prompt"))

    assert result.action == "block"


def test_unavailable_slm_fails_open() -> None:
    service = _service(SlmInspectionResult(unavailable=True))

    result = asyncio.run(service.inspect("prompt"))

    assert result.action == ""
    assert result.unavailable
    assert result.risk is None


def test_unavailable_result_preserves_failure_reason() -> None:
    service = _service(
        SlmInspectionResult(
            unavailable=True,
            error=SlmInspectionError("INVALID_RESPONSE", "SLM 응답 형식 오류"),
        )
    )

    result = asyncio.run(service.inspect("prompt"))

    assert result.unavailable
    assert result.risk is None
    assert result.action == ""
    assert result.error.code == "INVALID_RESPONSE"
    assert result.error.message == "SLM 응답 형식 오류"


@pytest.mark.parametrize(
    "actions,expected_action,expected_ids",
    [
        (("accept", "accept"), "accept", ["first", "second"]),
        (("accept", "masking"), "masking", ["first", "second"]),
        (("accept", "block"), "block", ["first", "second"]),
        (("block", "accept"), "block", ["first"]),
    ],
)
def test_overlapping_slm_policies_accumulate_until_block(
    actions, expected_action, expected_ids
):
    model_result = SlmInspectionResult(
        unsafe=True,
        use_default_policy_metadata=True,
        evidence=[{"kind": "pii", "type": "EMAIL", "value": "user@example.com"}],
    )
    service = _service(
        model_result,
        *(
            SlmPolicyRule(
                id=rule_id,
                name=rule_id,
                action=action,
                detection_kind="pii",
                detection_type="EMAIL",
            )
            for rule_id, action in zip(("first", "second"), actions, strict=True)
        ),
    )

    result = asyncio.run(service.inspect("user@example.com"))

    assert result.action == expected_action
    assert [item["policy_id"] for item in result.evidence] == expected_ids
    assert model_result.evidence == [
        {"kind": "pii", "type": "EMAIL", "value": "user@example.com"}
    ]


@pytest.mark.parametrize("matched_scope", ["user", "service", "group", "default"])
def test_slm_selects_one_scope_for_all_detection_types(matched_scope):
    subjects = {
        "user": "user@example.com",
        "service": "7",
        "group": "team",
        "default": "",
    }
    scopes = tuple(subjects)
    selected_index = scopes.index(matched_scope)
    rules = tuple(
        SlmPolicyRule(
            id=scope,
            name=scope,
            action="accept" if index == selected_index else "block",
            detection_kind="pii",
            detection_type="MISSING" if index < selected_index else "EMAIL",
            scope=scope,
            subject_id=subjects[scope],
        )
        for index, scope in enumerate(scopes)
    ) + (
        SlmPolicyRule(
            id="phone-default",
            name="phone",
            action="block",
            detection_kind="pii",
            detection_type="PHONE",
        ),
    )
    service = SlmInspectionService(
        StubInspection(
            SlmInspectionResult(
                unsafe=True,
                use_default_policy_metadata=True,
                evidence=[
                    {"kind": "pii", "type": "EMAIL", "value": "user@example.com"},
                    {"kind": "pii", "type": "PHONE", "value": "010-1234-5678"},
                ],
            )
        ),
        StubPolicyProvider(
            SlmPolicySnapshot(
                rules=rules,
                group_members=(("team", ("user@example.com",)),),
            )
        ),
        SlmInspectionOptions(),
    )

    result = asyncio.run(
        service.inspect("prompt", email="user@example.com", ai_service_id=7)
    )

    assert result.action == ("block" if matched_scope == "default" else "accept")
    assert result.evidence[0]["policy_id"] == matched_scope
    assert result.evidence[1]["action"] == (
        "block" if matched_scope == "default" else ""
    )


def test_slm_deduplicates_group_assignments_but_preserves_repeated_detections():
    rule = SlmPolicyRule(
        id="shared",
        name="shared",
        action="masking",
        detection_kind="pii",
        detection_type="EMAIL",
        scope="group",
        subject_id="first",
    )
    detections = [
        {"kind": "pii", "type": "EMAIL", "value": "user@example.com"},
        {"kind": "pii", "type": "EMAIL", "value": "user@example.com"},
    ]
    service = SlmInspectionService(
        StubInspection(SlmInspectionResult(unsafe=True, evidence=detections)),
        StubPolicyProvider(
            SlmPolicySnapshot(
                rules=(
                    rule,
                    replace(rule, subject_id="second"),
                    replace(rule, id="other"),
                ),
                group_members=(
                    ("first", ("user@example.com",)),
                    ("second", ("user@example.com",)),
                ),
            )
        ),
        SlmInspectionOptions(),
    )

    result = asyncio.run(service.inspect("prompt", email="user@example.com"))

    assert result.action == "masking"
    assert [item["policy_id"] for item in result.evidence] == [
        "shared",
        "other",
        "shared",
        "other",
    ]


@pytest.mark.parametrize("scope", ["user", "service", "group"])
def test_slm_ignores_policies_for_other_subjects(scope):
    rule = SlmPolicyRule(
        id="other",
        name="other",
        action="block",
        detection_kind="pii",
        detection_type="EMAIL",
        scope=scope,
        subject_id="not-this-subject",
    )
    service = _service(
        SlmInspectionResult(
            unsafe=True,
            evidence=[
                {"kind": "pii", "type": "EMAIL", "value": "user@example.com"},
            ],
        ),
        rule,
        replace(rule, id="default", action="masking", scope="default", subject_id=""),
    )

    result = asyncio.run(
        service.inspect("prompt", email="user@example.com", ai_service_id=7)
    )

    assert result.action == "masking"
    assert [item["policy_id"] for item in result.evidence] == ["default"]
