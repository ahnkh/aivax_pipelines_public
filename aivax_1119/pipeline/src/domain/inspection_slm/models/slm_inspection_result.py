"""모델 종류와 무관하게 사용하는 SLM 검사 결과."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from src.domain.inspection_slm.errors import SlmInspectionError


class SlmRiskCode(StrEnum):
    P = "P"
    L = "L"
    M = "M"
    H = "H"


@dataclass(frozen=True, slots=True)
class SlmInspectionResult:
    raw_content: str | None = None
    unsafe: bool = False
    evidence: list[dict[str, Any]] = field(default_factory=list)
    policy_name: str = ""
    policy_category: str = ""
    use_default_policy_metadata: bool = False
    force_block_without_policy: bool = False
    unavailable: bool = False

    risk: SlmRiskCode | None = None
    error: SlmInspectionError | None = None
