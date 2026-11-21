"""SLM 호출과 응답 해석에서 확인한 실패 원인."""

from typing import Literal

from src.core.ddd.domain_exception import DomainException

SlmInspectionErrorCode = Literal[
    "UPSTREAM_TIMEOUT",
    "UPSTREAM_CONNECTION_ERROR",
    "UPSTREAM_HTTP_ERROR",
    "UPSTREAM_REQUEST_ERROR",
    "INVALID_RESPONSE",
    "INTERNAL_ERROR",
]


class SlmInspectionError(DomainException):
    def __init__(
        self,
        code: SlmInspectionErrorCode,
        message: str,
    ) -> None:
        super().__init__("inspection-slm", code, message)
        self.message = message

    @classmethod
    def invalid_response(cls) -> "SlmInspectionError":
        return cls("INVALID_RESPONSE", "SLM 응답 형식이 올바르지 않습니다.")
