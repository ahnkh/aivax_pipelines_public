from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DocumentEvidence:
    sequence: int
    page_number: int
    line_number: int
    context: str
    matched_text: str


@dataclass(frozen=True, slots=True)
class DocumentEvidenceResult:
    evidence: tuple[DocumentEvidence, ...] = ()
