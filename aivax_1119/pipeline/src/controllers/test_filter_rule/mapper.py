"""rule test HTTP 계약과 application 모델 변환."""

import base64
import binascii

from src.application.use_cases.test_filter_rule import (
    FilterRuleTestAttachment,
    FilterRuleTestExecution,
    FilterRuleTestMultimodalExecution,
    FilterRuleTestRequest,
    FilterRuleTestResult,
)
from src.controllers.http_controller_mapper import HttpControllerMapper
from src.controllers.test_filter_rule.dto import (
    FilterRuleTestItem,
    FilterRuleTestMultimodalPolicyItem,
)
from src.domain.policy_multimodal.models import (
    MultimodalPolicyRule,
    MultimodalPolicySnapshot,
)
from src.domain.policy_regex.models import RegexPolicyRule, RegexPolicySnapshot
from src.domain.policy_slm.models import SlmPolicyRule, SlmPolicySnapshot


class TestFilterRuleMapper(HttpControllerMapper):
    _API_COMMAND = "rule filter test"

    @classmethod
    def to_request(cls, item: FilterRuleTestItem) -> FilterRuleTestRequest:
        policies = item.policies
        return FilterRuleTestRequest(
            prompt=item.prompt,
            attachments=tuple(
                FilterRuleTestAttachment(id=value.id, name=value.name)
                for value in item.attachments
            ),
            email=item.subject.email,
            ai_service_id=item.subject.ai_service_id,
            regex_policy=(
                RegexPolicySnapshot(
                    rules=tuple(
                        RegexPolicyRule(
                            id=rule.id,
                            name=rule.name,
                            expression=cls._decode_expression(rule.rule),
                            action=rule.action,
                            scope=rule.scope,
                            category=rule.category or "",
                            subject_id=rule.subject_id,
                        )
                        for rule in policies.regex.rules
                    ),
                    group_members=cls._group_members(
                        policies.regex.group_members,
                    ),
                )
                if policies and policies.regex is not None
                else None
            ),
            multimodal_policy=(
                cls._multimodal_policy(policies.multimodal)
                if policies and policies.multimodal is not None
                else None
            ),
            slm_policy=(
                SlmPolicySnapshot(
                    rules=tuple(
                        SlmPolicyRule(
                            id=rule.id,
                            name=rule.name,
                            action=rule.action,
                            detection_kind=rule.detection_kind,
                            detection_type=rule.detection_type,
                            scope=rule.scope,
                            category=rule.category or "",
                            subject_id=rule.subject_id,
                        )
                        for rule in policies.slm.rules
                    ),
                    group_members=cls._group_members(
                        policies.slm.group_members,
                    ),
                )
                if policies and policies.slm is not None
                else None
            ),
        )

    @staticmethod
    def _group_members(
        values: dict[str, list[str]],
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return tuple(
            (group_id, tuple(emails)) for group_id, emails in values.items()
        )

    @classmethod
    def _multimodal_policy(
        cls,
        policy: FilterRuleTestMultimodalPolicyItem,
    ) -> MultimodalPolicySnapshot:
        settings = {item.skey: item.svalue for item in policy.settings}
        extensions = tuple(
            value.strip().lower().lstrip(".")
            for value in settings.get(
                "fileControlAllowedExtensions",
                "",
            ).split(",")
            if value.strip()
        )
        try:
            max_size_bytes = int(settings.get("fileControlMaxSize", "0"))
        except ValueError:
            max_size_bytes = 0
        return MultimodalPolicySnapshot(
            rules=tuple(
                MultimodalPolicyRule(
                    id=rule.id,
                    name=rule.name,
                    expression=cls._decode_expression(rule.rule),
                    action="block" if rule.action == "masking" else rule.action,
                    scope=rule.scope,
                    target=rule.targets,
                    category=rule.category or "",
                    flags=rule.regex_flag,
                    subject_id=rule.subject_id,
                )
                for rule in policy.rules
            ),
            group_members=cls._group_members(policy.group_members),
            allowed_extensions=extensions,
            max_size_bytes=max(0, max_size_bytes),
        )

    @staticmethod
    def _decode_expression(value: str) -> str:
        """Valkey snapshot의 Base64 규칙과 직접 입력한 평문 규칙을 모두 지원한다."""
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return value
        return decoded.strip() or value

    @classmethod
    def to_success_response(cls, result: FilterRuleTestResult) -> dict:
        # management가 사용 중인 기존 top-level 결과 키를 유지한다.
        return {
            "api_code": 200,
            "api_message_code": "success",
            "api_command": cls._API_COMMAND,
            "regex_result": cls._execution(result.regex_result),
            "multimodal_result": cls._multimodal_execution(
                result.multimodal_result,
            ),
            "slm_result": cls._execution(result.slm_result),
            "final_decision": cls._execution(result.final_decision),
            "completed_date": cls.completed_date(),
        }

    @staticmethod
    def _execution(result: FilterRuleTestExecution | None) -> dict[str, object]:
        if result is None:
            return {}
        response = {"action": result.action}
        if result.content is not None:
            response["content"] = result.content
        if result.block_message is not None:
            response["block_message"] = result.block_message
        if result.policy_id:
            response["policy_id"] = result.policy_id
        if result.policy_name:
            response["policy_name"] = result.policy_name
        if result.elapsed_ms > 0:
            response["elapsed_ms"] = result.elapsed_ms
        if result.evidence:
            response["evidence"] = list(result.evidence)
        return response

    @staticmethod
    def _multimodal_execution(
        result: FilterRuleTestMultimodalExecution | None,
    ) -> dict[str, object]:
        if result is None:
            return {}
        return {
            "action": result.action,
            "policy_id": result.policy_id,
            "policy_name": result.policy_name,
            "files": list(result.files),
            "elapsed_ms": result.elapsed_ms,
        }
