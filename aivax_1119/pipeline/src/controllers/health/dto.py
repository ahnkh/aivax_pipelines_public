"""Management Health API와 동일한 JSON 필드명·값을 사용한다."""

from typing import Literal

from pydantic import BaseModel, Field


class ProcessStatusResponse(BaseModel):
    status: bool


class DependencyHealthResponse(BaseModel):
    status: Literal["ok", "error"]
    latencyMs: int = Field(ge=0)
    message: str | None = None
    usedPercent: float | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "error"]
    timestamp: str
    checks: dict[str, DependencyHealthResponse] | None = None
