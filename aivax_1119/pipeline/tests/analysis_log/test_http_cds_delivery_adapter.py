from unittest.mock import Mock

import pytest
import requests

from src.adapters.analysis_log_delivery import HttpCdsDeliveryAdapter
from src.domain.analysis_log_delivery.errors import (
    RetryableAnalysisLogDeliveryError,
)
from src.domain.analysis_log_delivery.models import MergedAnalysisLogDocument


def _traffic_log() -> MergedAnalysisLogDocument:
    return MergedAnalysisLogDocument(
        {
            "@timestamp": "2026-09-23T00:27:46Z",
            "message_id": "message-1",
            "client_host": "127.0.0.1",
            "user": {"email": "tester@example.com"},
            "mode": "block",
            "policy": {"policy_name": "이메일"},
        }
    )


def _session(response_body: dict) -> tuple[Mock, Mock]:
    session = Mock(spec=requests.Session)
    session.headers = {}
    response = Mock()
    response.status_code = 200
    response.json.return_value = response_body
    session.post.return_value = response
    return session, response


def test_returns_validated_partial_result() -> None:
    session, _ = _session(
        {
            "received": 2,
            "inserted": 1,
            "failed": 1,
            "rows": [
                {
                    "index": 0,
                    "status": "OK",
                    "id": 101,
                },
                {
                    "index": 1,
                    "status": "ERROR",
                    "message": "invalid",
                },
            ],
        }
    )
    adapter = HttpCdsDeliveryAdapter(
        "http://192.168.0.10/api/ext/v1/traffic-logs",
        "test-key",
        session=session,
    )

    result = adapter.deliver(
        [
            _traffic_log(),
            _traffic_log(),
        ]
    )

    assert result.rows[0].succeeded is True
    assert result.rows[1].succeeded is False

    session.post.assert_called_once()
    assert session.post.call_args.kwargs["timeout"] == 10


def test_rejects_inconsistent_success_response() -> None:
    session, _ = _session(
        {
            "received": 1,
            "inserted": 1,
            "failed": 0,
            "rows": [],
        }
    )
    adapter = HttpCdsDeliveryAdapter(
        "http://192.168.0.10/api/ext/v1/traffic-logs",
        "test-key",
        session=session,
    )

    with pytest.raises(ValueError):
        adapter.deliver([_traffic_log()])


def test_marks_only_500_response_as_retryable() -> None:
    session, response = _session({})
    response.status_code = 500
    adapter = HttpCdsDeliveryAdapter(
        "http://192.168.0.10/api/ext/v1/traffic-logs",
        "test-key",
        session=session,
    )

    with pytest.raises(RetryableAnalysisLogDeliveryError):
        adapter.deliver([_traffic_log()])


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 501, 503])
def test_rejects_non_500_response_as_non_retryable(
    status_code: int,
) -> None:
    session, response = _session({})
    response.status_code = status_code
    response.text = '{"error":"BAD_REQUEST"}'
    adapter = HttpCdsDeliveryAdapter(
        "http://192.168.0.10/api/ext/v1/traffic-logs",
        "test-key",
        session=session,
    )

    with pytest.raises(RuntimeError, match="BAD_REQUEST") as caught:
        adapter.deliver([_traffic_log()])

    assert not isinstance(caught.value, RetryableAnalysisLogDeliveryError)
