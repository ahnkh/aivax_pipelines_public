"""감사 이벤트 외부 알림 계약."""

from abc import ABC, abstractmethod

from src.domain.notification_audit.models import AuditEvent


class AuditNotificationPort(ABC):
    @abstractmethod
    def notify(self, event: AuditEvent) -> None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError
