from src.adapters.notification_policy.mapper import PolicyNotificationMapper
from src.domain.notification_policy.models.occurred_at import OccurredAt
from src.domain.notification_policy.models.policy_notification_event import (
    PolicyNotificationEvent,
)


def _event(*, occurred_at: str = "2026-09-09T01:02:03Z") -> PolicyNotificationEvent:
    return PolicyNotificationEvent(
        occurred_at=OccurredAt(occurred_at),
        message_id="message-1",
        user_id="user-1",
        user_email="user@example.com",
        ai_service="chatgpt",
        prompt="hello",
        mode="block",
        output_text="blocked",
    )


def test_mapper_uses_management_policy_notification_stream_contract() -> None:
    event = _event()
    fields = PolicyNotificationMapper().toPersistence(event)

    restored_event = PolicyNotificationMapper().toDomain(fields)

    assert restored_event == event
    assert restored_event.event_id == event.event_id
    assert fields == {
        "timestamp": "2026-09-09T01:02:03Z",
        "messageId": "message-1",
        "userId": "user-1",
        "userEmail": "user@example.com",
        "aiService": "chatgpt",
        "prompt": "hello",
        "mode": "block",
        "outputText": "blocked",
    }


def test_event_id_hash_uses_management_message_id_and_mode_dedupe_key() -> None:
    first_millisecond = _event(occurred_at="2026-09-09T01:02:03.001Z")
    last_millisecond = _event(occurred_at="2026-09-09T01:02:03.999Z")
    accept = PolicyNotificationEvent(
        occurred_at=OccurredAt("2026-09-09T01:02:04Z"),
        message_id="message-1",
        user_id="user-1",
        ai_service="chatgpt",
        prompt="hello",
        mode="accept",
        output_text="accepted",
    )

    assert first_millisecond.event_id == last_millisecond.event_id
    assert first_millisecond.event_id != accept.event_id


def test_mapper_omits_empty_optional_management_fields() -> None:
    fields = PolicyNotificationMapper().toPersistence(
        PolicyNotificationEvent(
            occurred_at=OccurredAt("2026-09-09T01:02:03+09:00"),
            message_id="message-1",
            user_id="user-1",
            ai_service="chatgpt",
            prompt="hello",
            mode="block",
            output_text="blocked",
        ),
    )

    assert fields["timestamp"] == "2026-09-08T16:02:03Z"
    assert "userRole" not in fields
    assert "userEmail" not in fields
    assert "userUuid" not in fields
    assert "policyName" not in fields
