import unittest
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.application.use_cases.publish_policy_refresh_signal import (
    PolicyRefreshSignalReceipt,
    PublishPolicyRefreshSignalUseCase,
)
from src.controllers.policy_refresh import PolicyRefreshController


class PolicyRefreshControllerTest(unittest.TestCase):
    def test_refresh_request_publishes_a_fanout_event(self) -> None:
        publisher = Mock()
        publisher.publish.return_value = "1740000000000-0"
        app = FastAPI()
        app.include_router(
            PolicyRefreshController(
                PublishPolicyRefreshSignalUseCase(publisher)
            ).router,
        )

        with TestClient(app) as client:
            response = client.post("/v1/filter/policy-refresh", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["api_command"], "policy refresh")
        self.assertEqual(
            response.json()["out_result"],
            {
                "status": "accepted",
                "event_id": "1740000000000-0",
            },
        )
        publisher.publish.assert_called_once_with(source="pipeline-api")

    def test_receipt_preserves_stream_event_id(self) -> None:
        publisher = Mock()
        publisher.publish.return_value = "1-0"

        receipt = PublishPolicyRefreshSignalUseCase(publisher).execute()

        self.assertEqual(receipt, PolicyRefreshSignalReceipt(event_id="1-0"))
