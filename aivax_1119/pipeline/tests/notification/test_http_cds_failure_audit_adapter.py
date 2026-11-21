from unittest.mock import Mock

import pytest
import requests

from src.adapters.notification_audit import (
    HttpManagementAuditNotificationAdapter,
)
from src.domain.notification_audit.models import AuditEvent


def test_posts_failure_to_management_internal_api() -> None:
    session = Mock(spec=requests.Session)
    session.headers = {}
    response = Mock()
    response.status_code = 201
    session.post.return_value = response

    adapter = HttpManagementAuditNotificationAdapter(
        "http://127.0.0.1:3000",
        session=session,
    )

    adapter.notify(
        AuditEvent(
            succeeded=False,
            category=4,
            source="nNetCDS",
            message="nNetCDS: CDS 요청 실패, 실패 2건",
        )
    )

    session.post.assert_called_once_with(
        "http://127.0.0.1:3000/api/v1/internal/audits",
        json={
            "isSuccess": False,
            "category": 4,
            "message": "nNetCDS: CDS 요청 실패, 실패 2건",
        },
        timeout=3,
    )


def test_rejects_non_created_response() -> None:
    session = Mock(spec=requests.Session)
    session.headers = {}
    response = Mock()
    response.status_code = 500
    session.post.return_value = response

    adapter = HttpManagementAuditNotificationAdapter(
        "http://127.0.0.1:3000",
        session=session,
    )

    with pytest.raises(RuntimeError):
        adapter.notify(
            AuditEvent(
                succeeded=False,
                category=4,
                source="nNetCDS",
                message="nNetCDS: CDS 요청 실패",
            )
        )
