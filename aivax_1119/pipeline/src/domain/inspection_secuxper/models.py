from src.domain.inspection_input.models.input_analysis_result import InputInspectionError
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SecuxperInspectionRequest:
    prompt: str
    policy_code: str


@dataclass(frozen=True, slots=True)
class SecuxperInspectionResult:
    provider_action: str | None = None
    evidence: list[dict[str, object]] = field(default_factory=list)
    provider_filtered_content: str = ""
    provider_metadata: Mapping[str, object] = field(default_factory=dict)
    unavailable: bool = False
    error: InputInspectionError | None = None
