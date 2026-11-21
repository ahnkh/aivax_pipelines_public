"""정규식 정책 검사 결과."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RegexMatchedPolicy:
    id: str
    name: str
    action: str
    targets: str
    category: str
    scope: str


@dataclass(frozen=True, slots=True)
class RegexEvidence:
    policy: RegexMatchedPolicy
    matched_text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RegexInspectionResult:
    action: str = ""
    selected_policy: RegexMatchedPolicy | None = None
    spans: tuple[tuple[int, int], ...] = ()
    evidence: tuple[RegexEvidence, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
