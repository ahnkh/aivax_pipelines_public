import asyncio
import json

import httpx
import pytest

from src.core.http import (
    AsyncJsonHttpClient,
    HttpClientOptions,
    HttpClientRegistry,
    HttpRequestError,
)


def test_posts_json_without_blocking_transport_contract() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = AsyncJsonHttpClient(
        HttpClientOptions(max_connections=3, max_keepalive_connections=1),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = asyncio.run(
            client.request_json(
                "POST",
                "http://slm.test/inspect",
                json_body={"prompt": "hello"},
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert result == {"ok": True}
    assert requests[0].method == "POST"
    assert json.loads(requests[0].content) == {"prompt": "hello"}


def test_status_failure_is_a_transport_neutral_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"}, request=request)

    client = AsyncJsonHttpClient(
        HttpClientOptions(max_connections=1, max_keepalive_connections=0),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(HttpRequestError) as caught:
            asyncio.run(client.request_json("GET", "http://service.test/health"))
    finally:
        asyncio.run(client.aclose())

    assert caught.value.kind == "status"
    assert caught.value.status_code == 503
    assert json.loads(caught.value.response_body) == {"error": "unavailable"}


def test_invalid_json_preserves_response_body_and_status() -> None:
    client = AsyncJsonHttpClient(
        HttpClientOptions(max_connections=1, max_keepalive_connections=0),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="잘못된 응답\nnot json")
        ),
    )
    try:
        with pytest.raises(HttpRequestError) as caught:
            asyncio.run(client.request_json("POST", "http://slm.test/inspect"))
    finally:
        asyncio.run(client.aclose())

    assert caught.value.kind == "invalid_json"
    assert caught.value.status_code == 200
    assert caught.value.response_body == "잘못된 응답\nnot json"


def test_registry_returns_a_process_shared_client_for_the_same_name() -> None:
    registry = HttpClientRegistry()
    options = HttpClientOptions(max_connections=3, max_keepalive_connections=1)

    assert registry.get_or_create("slm_inspection", options) is registry.get_or_create(
        "slm_inspection",
        options,
    )

    asyncio.run(registry.aclose())
