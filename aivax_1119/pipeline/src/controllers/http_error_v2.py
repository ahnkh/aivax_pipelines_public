"""입력·출력 V2 API가 공유하는 HTTP 오류 계약."""

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

FilterErrorCodeV2 = Literal[
    "unsupported_filter",
    "processor_unavailable",
    "consent_log_not_found",
    "log_delivery_failed",
]


class FilterErrorV2(BaseModel):
    code: FilterErrorCodeV2 = Field(description="프로그램에서 구분할 오류 코드")
    message: str = Field(description="오류 설명")


class FilterErrorResponseV2(BaseModel):
    detail: FilterErrorV2 = Field(
        description="오류 코드와 설명. HTTP 상태 코드는 본문에 중복하지 않음"
    )


FILTER_ERROR_RESPONSES_V2 = {
    404: {"model": FilterErrorResponseV2, "description": "필터·처리기 사용 불가"},
    409: {"model": FilterErrorResponseV2, "description": "동의 원본 로그 없음"},
    503: {"model": FilterErrorResponseV2, "description": "로그 저장·전달 재시도 필요"},
}


def filter_http_error_v2(
    status: int, code: FilterErrorCodeV2, error: Exception
) -> HTTPException:
    detail = FilterErrorV2(code=code, message=str(error))
    return HTTPException(status_code=status, detail=detail.model_dump())
