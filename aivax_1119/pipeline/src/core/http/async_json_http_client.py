"""재사용 가능한 비동기 JSON HTTP client."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from src.core.http.http_request_error import HttpRequestError


@dataclass(frozen=True, slots=True)
class HttpClientOptions:
    max_connections: int
    max_keepalive_connections: int
    default_timeout_seconds: float = 60.0


class AsyncJsonHttpClient:
    """연결 풀을 재사용하며 JSON 요청을 비동기로 전송한다."""

    def __init__(
        self,
        options: HttpClientOptions,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._options = options
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock: asyncio.Lock | None = None

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        connect_timeout_seconds: float | None = None,
    ) -> Any:
        normalized_method = method.upper()
        try:
            timeout = self._timeout(timeout_seconds, connect_timeout_seconds)
            response = await (await self._get_client()).request(
                normalized_method,
                url,
                json=dict(json_body) if json_body is not None else None,
                headers=dict(headers) if headers is not None else None,
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise HttpRequestError("timeout", normalized_method, url) from error
        except httpx.ConnectError as error:
            raise HttpRequestError("connection", normalized_method, url) from error
        except httpx.HTTPStatusError as error:
            raise HttpRequestError(
                "status",
                normalized_method,
                url,
                status_code=error.response.status_code,
                response_body=error.response.text,
            ) from error
        except httpx.RequestError as error:
            raise HttpRequestError("request", normalized_method, url) from error

        try:
            return response.json()
        except ValueError as error:
            raise HttpRequestError(
                "invalid_json",
                normalized_method,
                url,
                status_code=response.status_code,
                response_body=response.text,
            ) from error

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _timeout(
        self,
        timeout_seconds: float | None,
        connect_timeout_seconds: float | None,
    ) -> httpx.Timeout:
        request_timeout = timeout_seconds or self._options.default_timeout_seconds
        connect_timeout = min(
            connect_timeout_seconds or request_timeout,
            request_timeout,
        )
        return httpx.Timeout(request_timeout, connect=connect_timeout)

    async def _get_client(self) -> httpx.AsyncClient:
        client = self._client
        if client is not None:
            return client
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            client = self._client
            if client is None:
                client = httpx.AsyncClient(
                    timeout=httpx.Timeout(self._options.default_timeout_seconds),
                    limits=httpx.Limits(
                        max_connections=self._options.max_connections,
                        max_keepalive_connections=self._options.max_keepalive_connections,
                    ),
                    http2=False,
                    follow_redirects=False,
                    transport=self._transport,
                )
                self._client = client
            return client
