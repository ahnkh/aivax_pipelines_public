from abc import ABC, abstractmethod

from src.domain.notification_user.models.user_notification_event import (
    UserNotificationEvent,
)


class UserNotificationOutboxPort(ABC):
    """사용자 이벤트를 적재하고 발행하는 outbox의 인터페이스."""

    @abstractmethod
    def enqueue(self, event: UserNotificationEvent) -> None:
        """이벤트를 발행 대기 상태로 적재한다."""
        raise NotImplementedError

    @abstractmethod
    def flush(self) -> int:
        """대기 중인 이벤트를 발행하고 발행 건수를 반환한다."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """outbox의 background resource를 종료한다."""
        raise NotImplementedError
