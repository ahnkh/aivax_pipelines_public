"""입력 필터 실행 결과와 최종 판정 모델."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from src.domain.inspection_input.models.cso_grade_result import CsoGradeResult
from src.domain.inspection_input.models.input_analysis_request import InputInspectorType


class InputInspectionAction(StrEnum):
    ACCEPT = "accept"
    BLOCK = "block"
    MASKING = "masking"
    FAILED = "failed"
    BLANK = ""


@dataclass(frozen=True, slots=True)
class InputPolicyReference:
    id: str = ""
    name: str = ""
    category: str = ""
    scope: str = ""
    target: str = ""


@dataclass(frozen=True, slots=True)
class InputInspectionError:
    code: str
    message: str
    http_status: int | None = None

    def to_document(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code, "message": self.message}
        if self.http_status is not None:
            result["http_status"] = self.http_status
        return result


@dataclass(frozen=True, slots=True)
class InputInspectionResult:
    inspector_type: InputInspectorType
    action: InputInspectionAction = InputInspectionAction.BLANK
    masked_prompt: str = ""
    block_message: str = ""
    policy: InputPolicyReference | None = None
    evidence: tuple[Mapping[str, object], ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float | None = None
    error: InputInspectionError | None = None


@dataclass(frozen=True, slots=True)
class InputFinalDecision:
    action: InputInspectionAction = InputInspectionAction.BLANK
    masked_prompt: str = ""
    block_message: str = ""
    policy: InputPolicyReference | None = None


@dataclass(frozen=True, slots=True)
class InputAnalysisResult:
    executions: tuple[InputInspectionResult, ...]
    final_decision: InputFinalDecision
    write_input_log: bool = False
    stopped_after_block: bool = False
    cso_grades: tuple[CsoGradeResult, ...] = ()
    cso_block_message: str = ""
