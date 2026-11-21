"""filter rule test controller의 기존 HTTP 계약."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.application.use_cases.test_filter_rule import (
    FilterRuleTestExecution,
    FilterRuleTestResult,
)
from src.controllers.test_filter_rule import FilterRuleTestController


def test_controller_calls_one_use_case_and_preserves_response_keys() -> None:
    use_case = AsyncMock()
    use_case.execute.return_value = FilterRuleTestResult(
        regex_result=FilterRuleTestExecution(
            action="masking",
            content="[AIVAX MASKING]",
            block_message="[AIVAX MASKING]",
        ),
    )
    app = FastAPI()
    controller = FilterRuleTestController(use_case)
    app.include_router(controller.router)

    response = TestClient(app).post(
        "/v1/filter/testrule",
        json={
            "prompt": "secret",
            "policies": {
                "regex": {
                    "rules": [{
                        "id": "regex-1",
                        "name": "regex",
                        "rule": "secret",
                        "action": "masking",
                    }],
                },
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["api_command"] == "rule filter test"
    assert response.json()["regex_result"] == {
        "action": "masking",
        "content": "[AIVAX MASKING]",
        "block_message": "[AIVAX MASKING]",
    }
    assert response.json()["slm_result"] == {}
    assert response.json()["multimodal_result"] == {}
    request = use_case.execute.await_args.args[0]
    assert request.prompt == "secret"
    assert request.regex_policy.rules[0].expression == "secret"


def test_request_validation_happens_before_use_case() -> None:
    use_case = AsyncMock()
    app = FastAPI()
    app.include_router(FilterRuleTestController(use_case).router)

    response = TestClient(app).post(
        "/v1/filter/testrule",
        json={
            "prompt": "missing policies",
        },
    )

    assert response.status_code == 422
    use_case.execute.assert_not_awaited()


def test_controller_maps_policy_snapshots_and_attachments() -> None:
    use_case = AsyncMock()
    use_case.execute.return_value = FilterRuleTestResult()
    app = FastAPI()
    app.include_router(FilterRuleTestController(use_case).router)

    response = TestClient(app).post(
        "/v1/filter/testrule",
        json={
            "prompt": "inspect me",
            "attachments": [{"id": "/tmp/sample.pdf", "name": "sample.pdf"}],
            "subject": {"email": "user@example.com", "ai_service_id": 7},
            "policies": {
                "regex": {
                    "rules": [{
                        "id": "regex-1", "name": "regex", "rule": "inspect",
                        "action": "block", "scope": "user",
                        "subject_id": "user@example.com",
                    }],
                },
                "multimodal": {
                    "rules": [{
                        "id": "file-1", "name": "file", "rule": "secret",
                        "action": "block", "targets": "file",
                    }],
                    "settings": [{"skey": "fileControlMaxSize", "svalue": "2048"}],
                },
                "slm": {
                    "rules": [{
                        "id": "slm-1", "name": "slm", "action": "block",
                        "detection_kind": "abuse",
                    }],
                },
            },
        },
    )

    assert response.status_code == 200
    request = use_case.execute.await_args.args[0]
    assert request.attachments[0].id == "/tmp/sample.pdf"
    assert request.email == "user@example.com"
    assert request.regex_policy.rules[0].id == "regex-1"
    assert request.multimodal_policy.max_size_bytes == 2048
    assert request.slm_policy.rules[0].detection_kind == "abuse"


def test_controller_accepts_null_policy_categories() -> None:
    use_case = AsyncMock()
    use_case.execute.return_value = FilterRuleTestResult()
    app = FastAPI()
    app.include_router(FilterRuleTestController(use_case).router)

    response = TestClient(app).post(
        "/v1/filter/testrule",
        json={
            "prompt": "inspect me",
            "policies": {
                "regex": {
                    "rules": [{
                        "id": "regex-1", "name": "regex", "rule": "inspect",
                        "action": "block", "category": None,
                    }],
                },
                "multimodal": {
                    "rules": [{
                        "id": "file-1", "name": "file", "rule": "secret",
                        "action": "block", "category": None,
                    }],
                },
                "slm": {
                    "rules": [{
                        "id": "slm-1", "name": "slm", "action": "block",
                        "detection_kind": "abuse", "category": None,
                    }],
                },
            },
        },
    )

    assert response.status_code == 200
    request = use_case.execute.await_args.args[0]
    assert request.regex_policy.rules[0].category == ""
    assert request.multimodal_policy.rules[0].category == ""
    assert request.slm_policy.rules[0].category == ""


def test_multipart_upload_uses_request_scoped_temporary_file() -> None:
    use_case = AsyncMock()

    async def inspect(request):
        attachment = request.attachments[0]
        path = Path(attachment.id)
        assert path.exists()
        assert path.read_bytes() == b"temporary document"
        return FilterRuleTestResult()

    use_case.execute.side_effect = inspect
    app = FastAPI()
    app.include_router(FilterRuleTestController(use_case).router)
    payload = {
        "prompt": "inspect attachment",
        "policies": {
            "multimodal": {
                "rules": [],
                "settings": [],
            },
        },
    }

    response = TestClient(app).post(
        "/v1/filter/testrule/upload",
        data={"payload": json.dumps(payload)},
        files={"files": ("sample.txt", b"temporary document", "text/plain")},
    )

    assert response.status_code == 200
    temporary_path = Path(use_case.execute.await_args.args[0].attachments[0].id)
    assert not temporary_path.exists()
