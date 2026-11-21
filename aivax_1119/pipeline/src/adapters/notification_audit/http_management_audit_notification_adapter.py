"""Management CDS 실패 감사로그 HTTP adapter."""

from __future__ import annotations

import requests

from src.domain.notification_audit.models import AuditEvent
from src.domain.notification_audit.ports import AuditNotificationPort


class HttpManagementAuditNotificationAdapter(AuditNotificationPort):
    _AUDIT_PATH = "/api/v1/internal/audits"
    _AUDIT_CATEGORY_LOG = 4

    def __init__(
        self,
        management_api_url: str,
        *,
        timeout_seconds: float = 3,
        session: requests.Session | None = None,
    ) -> None:
        if not management_api_url.strip():
            raise ValueError("Management API URL이 필요합니다.")
        if timeout_seconds <= 0:
            raise ValueError(
                "Management 요청 제한 시간은 양수여야 합니다.",
            )

        self._api_url = management_api_url.rstrip("/") + self._AUDIT_PATH
        self._timeout_seconds = timeout_seconds
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._session.headers.update(
            {
                "Content-Type": "application/json",
            }
        )

    def notify(self, event: AuditEvent) -> None:
        normalized_message = event.message.strip()
        if not normalized_message:
            raise ValueError(
                "CDS 실패 감사로그 메시지가 필요합니다.",
            )

        response = self._session.post(
            self._api_url,
            json={
                "isSuccess": event.succeeded,
                "category": event.category,
                "message": normalized_message,
            },
            timeout=self._timeout_seconds,
        )

        if response.status_code != 201:
            raise RuntimeError(
                f"Management 감사로그 요청 실패 status={response.status_code}",
            )

    def close(self) -> None:
        if self._owns_session:
            self._session.close()
