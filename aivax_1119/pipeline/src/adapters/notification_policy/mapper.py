"""정책 알림 도메인 이벤트와 Valkey Stream field의 변환기."""

from typing import cast

from src.core.ddd.mapper import Mapper
from src.domain.notification_policy.models.occurred_at import OccurredAt
from src.domain.notification_policy.models.policy_notification_event import (
    PolicyNotificationEvent,
    PolicyNotificationMode,
)


class PolicyNotificationMapper(
    Mapper[PolicyNotificationEvent, dict[str, str], PolicyNotificationEvent],
):
    """PolicyNotificationEvent를 정책 알림 Stream 계약으로 변환한다."""

    def toPersistence(self, entity: PolicyNotificationEvent) -> dict[str, str]:
        fields = {
            "timestamp": entity.occurred_at.utc_iso,
            "messageId": entity.message_id,
            "userId": entity.user_id,
            "aiService": entity.ai_service,
            "prompt": entity.prompt,
            "mode": entity.mode,
            "outputText": entity.output_text,
        }
        optional_fields = {
            "userRole": entity.user_role,
            "userEmail": entity.user_email,
            "userUuid": entity.user_uuid,
            "policyName": entity.policy_name,
        }
        fields.update(
            {
                name: value
                for name, value in optional_fields.items()
                if isinstance(value, str) and value.strip()
            },
        )
        return fields

    def toDomain(self, record: dict[str, str]) -> PolicyNotificationEvent:
        return PolicyNotificationEvent(
            occurred_at=OccurredAt(record["timestamp"]),
            message_id=record["messageId"],
            user_id=record["userId"],
            user_role=record.get("userRole") or None,
            user_email=record.get("userEmail", ""),
            user_uuid=record.get("userUuid") or None,
            ai_service=record["aiService"],
            prompt=record["prompt"],
            # 외부 Stream 값은 domain entity가 유효성을 최종 검증한다.
            mode=cast(PolicyNotificationMode, record["mode"]),
            output_text=record["outputText"],
            policy_name=record.get("policyName") or None,
        )
