"""프로세스 수명 동안 외부 HTTP 연결 풀을 소유하는 registry."""

from __future__ import annotations

from src.core.http.async_json_http_client import AsyncJsonHttpClient, HttpClientOptions


class HttpClientRegistry:
    """이름별 HTTP client를 한 번 만들고 애플리케이션 종료 시 함께 닫는다."""

    def __init__(self) -> None:
        self._clients: dict[str, AsyncJsonHttpClient] = {}

    def get_or_create(
        self,
        name: str,
        options: HttpClientOptions,
    ) -> AsyncJsonHttpClient:
        client = self._clients.get(name)
        if client is None:
            client = AsyncJsonHttpClient(options)
            self._clients[name] = client
        return client

    async def aclose(self) -> None:
        clients, self._clients = tuple(self._clients.values()), {}
        for client in clients:
            await client.aclose()
