import pytest

from src.adapters.notification_user.mapper import (
    UserNotificationMapper,
)
from src.domain.notification_user.errors import UserNotificationDomainError
from src.domain.notification_user.models.user_notification_event import (
    UserNotificationEvent,
)


def test_mapper_converts_a_user_notification_event_to_management_user_sync_fields() -> (
    None
):
    event = UserNotificationEvent(
        message_id="message-1",
        client_ip="203.0.113.8",
        email="user@example.com",
        ai_service=2,
        prompt="hello",
        name="홍길동",
    )

    mapper = UserNotificationMapper()
    fields = mapper.toPersistence(event)

    assert fields == {
        "messageId": "message-1",
        "email": "user@example.com",
        "aiServiceId": "2",
        "ip": "203.0.113.8",
        "prompt": "hello",
        "name": "홍길동",
    }
    restored = mapper.toDomain(fields)
    assert restored.email == event.email
    assert restored.ai_service == "2"
    assert restored.client_ip == event.client_ip
    assert restored.prompt == event.prompt
    assert restored.name == event.name


def test_mapper_omits_optional_fields_when_no_update_is_requested() -> None:
    event = UserNotificationEvent(
        message_id="message-1",
        email="user@example.com",
        ai_service=2,
        client_ip=None,
        prompt=None,
    )

    assert UserNotificationMapper().toPersistence(event) == {
        "messageId": "message-1",
        "email": "user@example.com",
        "aiServiceId": "2",
    }


def test_mapper_canonicalizes_management_ai_service_id_as_a_string() -> None:
    event = UserNotificationEvent(
        message_id="message-1",
        email="user@example.com",
        ai_service="001",
    )

    assert UserNotificationMapper().toPersistence(event)["aiServiceId"] == "1"


@pytest.mark.parametrize("ai_service_id", [None, 0, "0", -1, "GPT", True])
def test_event_rejects_unregistered_ai_service_id_shape(
    ai_service_id: object,
) -> None:
    with pytest.raises(UserNotificationDomainError):
        UserNotificationEvent(
            message_id="message-1",
            email="user@example.com",
            ai_service=ai_service_id,  # type: ignore[arg-type]
        )


def test_event_rejects_missing_message_id() -> None:
    with pytest.raises(UserNotificationDomainError):
        UserNotificationEvent(
            message_id="",
            email="user@example.com",
            ai_service=1,
        )
