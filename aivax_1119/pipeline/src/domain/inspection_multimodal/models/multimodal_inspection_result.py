"""구현 방식과 무관하게 사용하는 멀티모달 분석 결과."""

from dataclasses import dataclass
from enum import StrEnum

from src.core.file import FileMetadata
from src.domain.inspection_multimodal.models.document_evidence import DocumentEvidence
from src.domain.inspection_multimodal.models.document_source import DocumentSource


class MultimodalFileLimitReason(StrEnum):
    UNSUPPORTED_EXTENSION = "unsupported_extension"
    OVERSIZED = "oversized"


@dataclass(frozen=True, slots=True)
class MultimodalMatchedPolicy:
    id: str = ""
    name: str = ""
    action: str = ""
    target: str = ""
    category: str = ""
    scope: str = "default"
    matched_text: str = ""
    expression: str = ""
    flags: int = 0


@dataclass(frozen=True, slots=True)
class MultimodalPolicyMatch:
    policy: MultimodalMatchedPolicy
    evidence: tuple[DocumentEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class MultimodalFileInspectionResult:
    source: DocumentSource
    file_name: str
    metadata: FileMetadata
    matches: tuple[MultimodalPolicyMatch, ...] = ()
    file_hash: str = ""
    content_chunk: str | None = None
    content_inspected: bool = False
    inspection_status: str = ""
    skip_reason: str = ""
    limit_reason: MultimodalFileLimitReason | None = None

    @property
    def selected_match(self) -> MultimodalPolicyMatch | None:
        priorities = {"block": 3, "masking": 2, "accept": 1, "": 0}
        return max(
            self.matches,
            key=lambda item: priorities.get(item.policy.action, 0),
            default=None,
        )

    @property
    def policy(self) -> MultimodalMatchedPolicy | None:
        selected = self.selected_match
        return selected.policy if selected is not None else None

    @property
    def action(self) -> str:
        policy = self.policy
        return policy.action if policy is not None else ""

    @property
    def evidence(self) -> tuple[DocumentEvidence, ...]:
        selected = self.selected_match
        return selected.evidence if selected is not None else ()


@dataclass(frozen=True, slots=True)
class MultimodalInspectionResult:
    action: str = ""
    selected_policy: MultimodalMatchedPolicy | None = None
    files: tuple[MultimodalFileInspectionResult, ...] = ()
    unavailable: bool = False
