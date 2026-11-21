"""SecuXperAI v1 inspection adapter."""

from __future__ import annotations

import logging

from src.adapters.inspection_secuxper.secuxperai.http_client import (
    SecuxperaiHttpClient,
)
from src.adapters.inspection_secuxper.secuxperai.response_mapper import (
    SecuxperaiResponseMapper,
)
from src.adapters.inspection_secuxper.secuxperai.token_manager import (
    SecuxperaiTokenManager,
)
from src.core.http import HttpRequestError
from src.domain.inspection_input.models.input_analysis_result import InputInspectionError
from src.domain.inspection_secuxper.models import (
    SecuxperInspectionRequest,
    SecuxperInspectionResult,
)
from src.domain.inspection_secuxper.ports import SecuxperInspectionPort


class SecuxperaiInspectionAdapter(SecuxperInspectionPort):
    """SecuXperAI가 적용한 필터 정책 결과를 secuXper 결과로 변환한다."""

    _MAX_REQUEST_ATTEMPTS = 3

    def __init__(
        self,
        http_client: SecuxperaiHttpClient,
        token_manager: SecuxperaiTokenManager,
        response_mapper: SecuxperaiResponseMapper,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._http_client = http_client
        self._token_manager = token_manager
        self._response_mapper = response_mapper
        self._logger = logger or logging.getLogger("khan-python")

    async def inspect(
        self,
        request: SecuxperInspectionRequest,
    ) -> SecuxperInspectionResult:
        if not request.policy_code.strip():
            raise ValueError("secuXper policy_code is required")
        access_token = await self._token_manager.get_access_token()
        if access_token is None:
            return SecuxperInspectionResult(unavailable=True, error=InputInspectionError("UPSTREAM_AUTH_ERROR", "SecuXper 인증 토큰을 가져오지 못했습니다."))

        for attempt in range(self._MAX_REQUEST_ATTEMPTS):
            try:
                response = await self._http_client.inspect(
                    request.prompt,
                    access_token,
                    request.policy_code,
                )
                result = self._response_mapper.to_result(
                    response,
                    request.prompt,
                )
                self._logger.debug(
                    "SecuXperAI 필터 요청 성공 external_id=%s chat_id=%s action=%s",
                    result.provider_metadata.get(
                        "external_filter_id",
                        "",
                    ),
                    result.provider_metadata.get(
                        "external_chat_id",
                        "",
                    ),
                    result.provider_action,
                )
                return result
            except HttpRequestError as error:
                if error.status_code != 401:
                    self._log_request_error(error)
                    return SecuxperInspectionResult(unavailable=True, error=self._inspection_error(error))

                if attempt >= self._MAX_REQUEST_ATTEMPTS - 1:
                    self._logger.error(
                        "SecuXperAI 인증 복구 후에도 요청이 실패했습니다",
                    )
                    return SecuxperInspectionResult(unavailable=True, error=self._inspection_error(error))

                recovered_token = await self._token_manager.recover_after_auth_failure(
                    access_token,
                    try_refresh=attempt == 0,
                )
                if recovered_token is None:
                    return SecuxperInspectionResult(unavailable=True, error=self._inspection_error(error))

                access_token = recovered_token
            except (TypeError, ValueError) as error:
                self._logger.error(
                    "SecuXperAI 응답 형식 오류 error=%s",
                    error,
                )
                return SecuxperInspectionResult(unavailable=True, error=InputInspectionError("INVALID_RESPONSE", "SecuXper 응답 형식이 올바르지 않습니다."))
            except Exception:
                self._logger.exception(
                    "SecuXperAI 필터 요청 처리 실패",
                )
                return SecuxperInspectionResult(unavailable=True, error=InputInspectionError("INTERNAL_ERROR", "SecuXper 검사를 완료하지 못했습니다."))

        return SecuxperInspectionResult(unavailable=True, error=InputInspectionError("INTERNAL_ERROR", "SecuXper 검사를 완료하지 못했습니다."))

    @staticmethod
    def _inspection_error(error: HttpRequestError) -> InputInspectionError:
        codes = {
            "timeout": ("UPSTREAM_TIMEOUT", "SecuXper API 응답 시간이 초과되었습니다."),
            "connection": ("UPSTREAM_CONNECTION_ERROR", "SecuXper API에 연결하지 못했습니다."),
            "status": ("UPSTREAM_HTTP_ERROR", "SecuXper API가 오류 응답을 반환했습니다."),
            "invalid_json": ("INVALID_RESPONSE", "SecuXper 응답이 유효한 JSON이 아닙니다."),
        }
        code, message = codes.get(error.kind, ("UPSTREAM_CONNECTION_ERROR", "SecuXper API 요청에 실패했습니다."))
        return InputInspectionError(code, message, error.status_code if error.kind == "status" else None)

    def _log_request_error(
        self,
        error: HttpRequestError,
    ) -> None:
        if error.kind == "timeout":
            self._logger.error(
                "SecuXperAI 필터 요청 시간 초과",
            )
        elif error.kind == "connection":
            self._logger.error(
                "SecuXperAI 필터 연결 실패",
            )
        elif error.kind == "status":
            self._logger.error(
                "SecuXperAI 필터 HTTP 오류 status=%s",
                error.status_code,
            )
        elif error.kind == "invalid_json":
            self._logger.error(
                "SecuXperAI 필터 응답이 유효한 JSON이 아닙니다",
            )
        else:
            self._logger.error(
                "SecuXperAI 필터 요청 실패 kind=%s",
                error.kind,
            )
