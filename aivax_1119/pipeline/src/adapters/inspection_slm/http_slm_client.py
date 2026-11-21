"""SLM HTTP 요청과 호출 실패 원인의 정규화."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from src.adapters.inspection_slm.settings import SlmHttpSettings
from src.core.http import AsyncJsonHttpClient, HttpRequestError
from src.domain.inspection_slm.errors import SlmInspectionError, SlmInspectionErrorCode

_LOGGER = logging.getLogger("khan-python")


class HttpSlmClient:
    def __init__(self, settings: SlmHttpSettings, client: AsyncJsonHttpClient) -> None:
        self._settings = settings
        self._client = client

    async def post(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            body = await self._client.request_json(
                "POST",
                self._settings.endpoint,
                json_body=payload,
                headers={"Content-Type": "application/json"},
                timeout_seconds=float(self._settings.request_timeout_seconds),
                connect_timeout_seconds=float(self._settings.connect_timeout_seconds),
            )
        except HttpRequestError as error:
            reason = (
                "invalid SLM response: invalid JSON"
                if error.kind == "invalid_json"
                else "SLM request failed"
            )
            _LOGGER.error(
                "%s kind=%s status=%s request_body=%s response_body=%s",
                reason,
                error.kind,
                error.status_code,
                json.dumps(dict(payload), ensure_ascii=False),
                json.dumps(error.response_body, ensure_ascii=False),
            )
            if error.kind == "invalid_json":
                raise SlmInspectionError.invalid_response() from error
            codes: dict[str, tuple[SlmInspectionErrorCode, str]] = {
                "timeout": ("UPSTREAM_TIMEOUT", "SLM API 응답 시간이 초과되었습니다."),
                "connection": (
                    "UPSTREAM_CONNECTION_ERROR",
                    "SLM API에 연결하지 못했습니다.",
                ),
                "status": (
                    "UPSTREAM_HTTP_ERROR",
                    "SLM API가 오류 응답을 반환했습니다.",
                ),
            }
            code, message = codes.get(
                error.kind, ("UPSTREAM_REQUEST_ERROR", "SLM API 요청에 실패했습니다.")
            )
            raise SlmInspectionError(code, message) from error
        except Exception:
            _LOGGER.exception(
                "SLM request failed request_body=%s response_body=null",
                json.dumps(dict(payload), ensure_ascii=False),
            )
            raise
        if not isinstance(body, dict):
            _LOGGER.error(
                "invalid SLM response: expected JSON object (body_type=%s) "
                "request_body=%s response_body=%s",
                type(body).__name__,
                json.dumps(dict(payload), ensure_ascii=False),
                json.dumps(body, ensure_ascii=False),
            )
            raise SlmInspectionError.invalid_response()
        return body
