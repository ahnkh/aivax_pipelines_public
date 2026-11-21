"""HTTP controller가 공통으로 쓰는 응답 변환 기반 클래스."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from fastapi import HTTPException


class HttpControllerMapper:
    """HTTP 응답 envelope와 예외 응답을 만드는 controller 전용 기반 클래스.

    이 클래스는 HTTP 계약을 알기 때문에 ``src.core.ddd``가 아니라 controller
    계층에 둔다. 도메인/영속성 변환용 ``src.core.ddd.Mapper``와는 책임이 다르다.
    """

    @staticmethod
    def completed_date() -> str:
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    @classmethod
    def success_response(cls, *, api_command: str, result: Any) -> dict[str, Any]:
        return {
            "api_code": 200,
            "api_message_code": "success",
            "api_command": api_command,
            "out_result": result,
            "completed_date": cls.completed_date(),
        }

    @classmethod
    def error_response(
        cls,
        *,
        status_code: int,
        api_command: str,
        api_message_code: str,
        result: Any,
        logger: logging.Logger | None = None,
    ) -> HTTPException:
        if logger is not None:
            logger.error(
                "api fail, code = %s, message = %s",
                status_code,
                result,
            )
        return HTTPException(
            status_code=status_code,
            detail={
                "api_command": api_command,
                "api_code": status_code,
                "api_message_code": api_message_code,
                "out_result": str(result),
                "completed_date": cls.completed_date(),
            },
        )
