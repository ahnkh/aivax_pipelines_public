"""`/v1/filter/policy-refresh` HTTP 요청 계약."""

import datetime

from pydantic import BaseModel, Field


class PolicyRefreshRequest(BaseModel):
    # 기존 빈 요청 body도 OpenAPI 호환을 위해 유지한다.
    date: datetime.datetime = Field(default_factory=datetime.datetime.now)
