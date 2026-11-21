"""SecuXperAI 인증 및 필터 HTTP 요청 client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.adapters.inspection_secuxper.secuxperai.settings import (
    SecuxperaiSettings,
)
from src.core.http import AsyncJsonHttpClient


class SecuxperaiHttpClient:
    """SecuXperAI HTTP 계약을 처리한다."""

    _SUCCESS_CODE = "SC0000"

    def __init__(
        self,
        settings: SecuxperaiSettings,
        client: AsyncJsonHttpClient,
    ) -> None:
        self._settings = settings
        self._client = client

    async def issue_token(self) -> str:
        """team UUID와 client 번호로 access token을 발급한다."""
        response = await self._client.request_json(
            "POST",
            self._settings.token_url,
            json_body={
                "team_uuid": self._settings.team_uuid,
                "client_no": self._settings.client_no,
            },
            headers={
                "Content-Type": "application/json",
            },
            timeout_seconds=float(
                self._settings.request_timeout_seconds,
            ),
            connect_timeout_seconds=float(
                self._settings.connect_timeout_seconds,
            ),
        )
        return self._access_token(response)

    async def refresh_token(self, access_token: str) -> str:
        """현재 access token으로 새로운 token을 요청한다."""
        response = await self._client.request_json(
            "POST",
            self._settings.refresh_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout_seconds=float(
                self._settings.request_timeout_seconds,
            ),
            connect_timeout_seconds=float(
                self._settings.connect_timeout_seconds,
            ),
        )
        return self._access_token(response)

    async def inspect(
        self,
        prompt: str,
        access_token: str,
        policy_code: str,
    ) -> dict[str, Any]:
        """프롬프트를 SecuXperAI 필터 API로 검사한다."""
        response = await self._client.request_json(
            "POST",
            self._settings.filter_url,
            json_body={
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                "policy_code": policy_code,
                "site_code": self._settings.site_code,
                "sync_mode": self._settings.sync_mode,
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout_seconds=float(
                self._settings.request_timeout_seconds,
            ),
            connect_timeout_seconds=float(
                self._settings.connect_timeout_seconds,
            ),
        )
        if not isinstance(response, dict):
            raise ValueError(
                "SecuXperAI 필터 응답은 JSON object여야 합니다.",
            )
        return response

    @classmethod
    def _access_token(cls, response: object) -> str:
        if not isinstance(response, Mapping):
            raise ValueError(
                "SecuXperAI token 응답은 JSON object여야 합니다.",
            )

        result_code = response.get("result_code")
        if result_code != cls._SUCCESS_CODE:
            raise ValueError(
                f"SecuXperAI token 요청이 실패했습니다. result_code={result_code!r}",
            )

        data = response.get("data")
        if not isinstance(data, Mapping):
            raise ValueError(
                "SecuXperAI token 응답에 data object가 없습니다.",
            )

        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ValueError(
                "SecuXperAI token 응답에 access_token이 없습니다.",
            )

        return access_token.strip()
