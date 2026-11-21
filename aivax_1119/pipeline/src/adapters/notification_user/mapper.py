"""Management 사용자 동기화 이벤트와 Valkey Stream field의 변환기."""

from src.core.ddd.mapper import Mapper
from src.domain.notification_user.models.user_notification_event import (
    UserNotificationEvent,
)


class UserNotificationMapper(
    Mapper[UserNotificationEvent, dict[str, str], UserNotificationEvent],
):
    """UserNotificationEvent를 `management:user-sync:v1` 계약으로 변환한다."""

    def toPersistence(self, entity: UserNotificationEvent) -> dict[str, str]:
        fields = {
            "messageId": entity.message_id,
            "email": entity.email,
            # Management의 숫자 PK를 canonical decimal string으로 전달한다.
            "aiServiceId": str(int(str(entity.ai_service).strip())),
        }
        if entity.client_ip is not None:
            fields["ip"] = entity.client_ip
        if entity.prompt is not None:
            fields["prompt"] = entity.prompt
        if entity.name is not None:
            fields["name"] = entity.name
        return fields

    def toDomain(self, record: dict[str, str]) -> UserNotificationEvent:
        return UserNotificationEvent(
            message_id=record["messageId"],
            email=record["email"],
            ai_service=record["aiServiceId"],
            client_ip=record.get("ip") or None,
            prompt=record.get("prompt") or None,
            name=record.get("name") or None,
        )
