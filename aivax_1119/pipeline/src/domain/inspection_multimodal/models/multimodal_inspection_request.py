"""멀티모달 분석 엔진에 전달하는 요청 계약."""

from dataclasses import dataclass

from src.domain.inspection_multimodal.models.document_source import DocumentSource
from src.domain.inspection_multimodal.models.multimodal_inspection_options import (
    MultimodalInspectionOptions,
)
from src.domain.policy_multimodal.models import MultimodalPolicySnapshot


@dataclass(frozen=True, slots=True)
class MultimodalInspectionTarget:
    source: DocumentSource
    file_name: str


@dataclass(frozen=True, slots=True)
class MultimodalInspectionRequest:
    targets: tuple[MultimodalInspectionTarget, ...]
    policy: MultimodalPolicySnapshot
    options: MultimodalInspectionOptions
    email: str = ""
    ai_service_id: int | str = 0
