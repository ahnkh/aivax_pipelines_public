"""Controller 이동 전 API 계약과 요청 검증 동작을 고정한다."""

import hashlib
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from src.controllers.input_analysis.v1.input_analysis_controller import (
    InputAnalysisController,
)
from src.controllers.input_analysis.v1.dto import InputAnalysisItem
from src.controllers.output_analysis.v1.output_analysis_controller import (
    OutputAnalysisController,
)
from src.controllers.policy_refresh import PolicyRefreshController
from src.controllers.test_filter_rule import FilterRuleTestController
from src.domain.analysis_log_merge.models.output_log import OutputLogInput
from src.domain.analysis_log_merge.errors.output_log_processor_unavailable_error import (
    OutputLogProcessorUnavailableError,
)
from src.domain.inspection_input.models import InputAnalysisResult, InputFinalDecision
from src.application.use_cases.input_analysis import InputAnalysisOutcome
from src.application.use_cases.test_filter_rule import FilterRuleTestResult
from src.application.use_cases.publish_policy_refresh_signal import (
    PolicyRefreshSignalReceipt,
)


class ControllerContractTest(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.output_service = AsyncMock()
        self.output_service.execute.return_value = {}
        self.controller = OutputAnalysisController(self.output_service)
        self.input_service = AsyncMock()
        self.input_service.execute.return_value = InputAnalysisOutcome(
            analysis_result=InputAnalysisResult(
                executions=(),
                final_decision=InputFinalDecision(),
            ),
        )
        self.input_controller = InputAnalysisController(self.input_service)
        self.rule_test_use_case = AsyncMock()
        self.rule_test_use_case.execute.return_value = FilterRuleTestResult()
        self.rule_test_controller = FilterRuleTestController(self.rule_test_use_case)
        self.policy_refresh_use_case = MagicMock()
        self.policy_refresh_use_case.execute.return_value = PolicyRefreshSignalReceipt(
            event_id="1-0",
        )
        self.policy_refresh_controller = PolicyRefreshController(
            self.policy_refresh_use_case,
        )
        self.app.include_router(self.input_controller.router)
        self.app.include_router(self.controller.router)
        self.app.include_router(self.policy_refresh_controller.router)
        self.app.include_router(self.rule_test_controller.router)
        self.client = TestClient(self.app)

    def test_invalid_attachment_is_rejected_before_service_execution(self):
        response = self.client.post(
            "/v1/filter/multiple_filter",
            json={
                "prompt": "hello",
                "attachments": [{"size": "not-a-number"}],
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn(
            ["body", "attachments", 0, "size"],
            [error["loc"] for error in response.json()["detail"]],
        )
        self.input_service.execute.assert_not_awaited()

    def test_valid_request_reaches_service_as_domain_request(self):
        payload = {
            "prompt": "hello",
            "message_id": "contract-test",
            "attachments": [{"id": "/backup/file", "size": "42", "name": "file"}],
        }
        response = self.client.post("/v1/filter/multiple_filter", json=payload)
        self.assertEqual(response.status_code, 200)
        request = self.input_service.execute.await_args.args[0]
        self.assertEqual(request.message_id, "contract-test")
        self.assertEqual(request.attachments[0].size, 42)
        self.assertEqual(request.attachments[0].name, "file")

    def test_output_type_validation_precedes_command(self):
        response = self.client.post("/v1/filter/output_filter", json={"llm_output": []})
        self.assertEqual(response.status_code, 422)
        self.output_service.execute.assert_not_awaited()

    def test_output_controller_calls_injected_service_directly(self):
        self.output_service.execute.return_value = {"processed": True}
        response = self.client.post(
            "/v1/filter/output_filter",
            json={
                "message_id": "direct",
                "llm_output": "응답",
                "email": "test@example.com",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["out_result"], {"processed": True})
        self.assertEqual(response.json()["api_command"], "output api router")
        self.output_service.execute.assert_awaited_once()
        data = self.output_service.execute.await_args.args[0]
        self.assertIsInstance(data, OutputLogInput)
        self.assertEqual(data.message_id, "direct")
        self.assertEqual(data.llm_output, "응답")
        self.assertEqual(data.email, "test@example.com")

    def test_output_unavailable_preserves_http_404(self):
        self.output_service.execute.side_effect = OutputLogProcessorUnavailableError()
        response = self.client.post(
            "/v1/filter/output_filter", json={"llm_output": "응답"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["api_code"], 404)
        self.assertEqual(response.json()["detail"]["api_command"], "output api router")

    def test_output_service_needs_no_fastapi_dependency_or_app_state(self):
        self.assertFalse(hasattr(self.app.state, "output_analysis_service"))
        route = next(
            route
            for route in self.controller.router.routes
            if route.path == "/v1/filter/output_filter"
        )
        self.assertEqual(route.dependant.dependencies, [])
        response = self.client.post(
            "/v1/filter/output_filter", json={"llm_output": "응답"}
        )
        self.assertEqual(response.status_code, 200)
        self.output_service.execute.assert_awaited_once()

    def test_output_service_is_scoped_to_each_fastapi_app(self):
        other_app = FastAPI()
        other_service = AsyncMock()
        other_service.execute.return_value = {"app": "other"}
        other_app.include_router(OutputAnalysisController(other_service).router)
        with TestClient(other_app) as client:
            response = client.post(
                "/v1/filter/output_filter", json={"llm_output": "응답"}
            )
        self.assertEqual(response.json()["out_result"], {"app": "other"})
        self.output_service.execute.assert_not_awaited()

    def test_v1_input_dto_is_owned_by_input_controller(self):
        self.assertTrue(InputAnalysisItem.__module__.startswith("src.controllers."))
