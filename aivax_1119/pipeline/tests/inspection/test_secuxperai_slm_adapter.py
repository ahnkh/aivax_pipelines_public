"""SecuXperAI 독립 adapter의 핵심 계약 테스트."""

from __future__ import annotations

import asyncio
import pytest
from types import SimpleNamespace

from src.adapters.inspection_secuxper.secuxperai.adapter import (
    SecuxperaiInspectionAdapter,
)
from src.adapters.inspection_secuxper.secuxperai.response_mapper import (
    SecuxperaiResponseMapper,
)
from src.adapters.inspection_secuxper.secuxperai.token_manager import (
    SecuxperaiTokenManager,
)
from src.core.http import HttpRequestError
from src.domain.inspection_secuxper.models import SecuxperInspectionRequest


def _masking_response() -> dict:
    return {
        "id": "filter-id",
        "chat_id": "chat-id",
        "filter_result": 2,
        "filter_message": {
            "content": "제 이메일은 ●●●●@example.com입니다.",
            "message": {
                "personal_data": "Y",
            },
        },
        "filter_detail": [
            {
                "function": "개인정보",
                "item": "메일주소",
                "policy": 2,
                "detected_count": 1,
                "detected_items": {
                    "user@example.com": [[7, 23]],
                },
            },
            {
                "function": "의도분류",
                "item": "상용",
                "policy": 0,
                "detected_count": 1,
            },
        ],
    }


class StubHttpClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.tokens: list[str] = []

    async def inspect(
        self,
        prompt: str,
        access_token: str,
        policy_code: str,
    ) -> dict:
        self.tokens.append(access_token)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class StubTokenManager:
    def __init__(self) -> None:
        self.recovery_calls: list[tuple[str, bool]] = []

    async def get_access_token(self) -> str:
        return "old-token"

    async def recover_after_auth_failure(
        self,
        failed_token: str,
        *,
        try_refresh: bool,
    ) -> str:
        self.recovery_calls.append((failed_token, try_refresh))
        return "new-token"


class StubTokenStore:
    def __init__(self, token: str | None) -> None:
        self.token = token
        self.saved_tokens: list[str] = []
        self.released = False

    async def get(self) -> str | None:
        return self.token

    async def save(self, access_token: str) -> None:
        self.token = access_token
        self.saved_tokens.append(access_token)

    async def acquire_lock(
        self,
        owner_id: str,
        ttl_seconds: int,
    ) -> bool:
        return True

    async def release_lock(self, owner_id: str) -> None:
        self.released = True


class StubTokenHttpClient:
    def __init__(self) -> None:
        self.refresh_calls: list[str] = []
        self.issue_calls = 0

    async def refresh_token(self, access_token: str) -> str:
        self.refresh_calls.append(access_token)
        raise RuntimeError("refresh unavailable")

    async def issue_token(self) -> str:
        self.issue_calls += 1
        return "issued-token"


def test_response_mapper_converts_masking_result() -> None:
    prompt = "제 이메일은 user@example.com입니다."

    result = SecuxperaiResponseMapper().to_result(
        _masking_response(),
        prompt,
    )

    assert result.provider_action == "masking"
    assert result.provider_filtered_content == ("제 이메일은 ●●●●@example.com입니다.")
    assert result.provider_metadata == {
        "provider": "secuxperai",
        "external_filter_id": "filter-id",
        "external_chat_id": "chat-id",
    }
    assert result.evidence == [
        {
            "source": "secuxperai",
            "kind": "pii",
            "function": "개인정보",
            "type": "메일주소",
            "action": "masking",
            "detected_count": 1,
            "value": "user@example.com",
            "start": 7,
            "end": 23,
        },
    ]


def test_adapter_retries_once_with_recovered_token_after_401() -> None:
    unauthorized = HttpRequestError(
        "status",
        "POST",
        "https://secuxperai.test/chat/filter/",
        status_code=401,
    )
    http_client = StubHttpClient(
        [
            unauthorized,
            _masking_response(),
        ]
    )
    token_manager = StubTokenManager()
    adapter = SecuxperaiInspectionAdapter(
        http_client,
        token_manager,
        SecuxperaiResponseMapper(),
    )

    result = asyncio.run(
        adapter.inspect(
            SecuxperInspectionRequest(
                "제 이메일은 user@example.com입니다.",
                "selected-policy",
            ),
        )
    )

    assert result.provider_action == "masking"
    assert http_client.tokens == ["old-token", "new-token"]
    assert token_manager.recovery_calls == [
        ("old-token", True),
    ]


def test_adapter_fails_open_for_invalid_response() -> None:
    invalid_response = {
        "id": "filter-id",
        "chat_id": "chat-id",
        "filter_result": "masking",
        "filter_message": {},
        "filter_detail": [],
    }
    adapter = SecuxperaiInspectionAdapter(
        StubHttpClient([invalid_response]),
        StubTokenManager(),
        SecuxperaiResponseMapper(),
    )

    result = asyncio.run(
        adapter.inspect(
            SecuxperInspectionRequest("prompt", "selected-policy"),
        )
    )

    assert result.unavailable
    assert result.provider_action is None


def test_token_manager_issues_new_token_when_refresh_fails() -> None:
    token_store = StubTokenStore("expired-token")
    http_client = StubTokenHttpClient()
    settings = SimpleNamespace(
        token_lock_ttl_seconds=10,
        token_wait_seconds=1,
    )
    manager = SecuxperaiTokenManager(
        settings,
        token_store,
        http_client,
    )

    result = asyncio.run(
        manager.recover_after_auth_failure(
            "expired-token",
            try_refresh=True,
        )
    )

    assert result == "issued-token"
    assert http_client.refresh_calls == ["expired-token"]
    assert http_client.issue_calls == 1
    assert token_store.saved_tokens == ["issued-token"]
    assert token_store.released


@pytest.mark.parametrize("kind,status,code", [
    ("timeout", None, "UPSTREAM_TIMEOUT"),
    ("connection", None, "UPSTREAM_CONNECTION_ERROR"),
    ("status", 503, "UPSTREAM_HTTP_ERROR"),
    ("invalid_json", None, "INVALID_RESPONSE"),
])
def test_adapter_preserves_inspection_failure_reason(kind, status, code):
    adapter = SecuxperaiInspectionAdapter(
        StubHttpClient([HttpRequestError(kind, "POST", "https://example.invalid", status_code=status)]),
        StubTokenManager(), SecuxperaiResponseMapper(),
    )
    result = asyncio.run(adapter.inspect(SecuxperInspectionRequest("prompt", "policy")))
    assert result.unavailable
    assert result.error.code == code
    assert result.error.http_status == status
    assert "example.invalid" not in result.error.message
