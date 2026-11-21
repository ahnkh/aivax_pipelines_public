"""첨부파일 한 건의 CSO 등급 조회 결과."""

from dataclasses import dataclass
from enum import StrEnum


class CsoGradeStatus(StrEnum):
    TAGGED = "tagged"
    UNTAGGED = "untagged"
    ERROR = "error"
    SKIPPED = "skipped"


class CsoInspectionStatus(StrEnum):
    OPEN = "open"
    BLOCKED = "blocked"
    UNTAGGED = "untagged"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class CsoGradeResult:
    grade: int | None
    status: CsoGradeStatus
    unsupported: bool = False

    @property
    def is_open(self) -> bool:
        return self.status is CsoGradeStatus.TAGGED and self.grade == 3

    @property
    def is_blocking(self) -> bool:
        return not self.is_open and self.status is not CsoGradeStatus.SKIPPED


def summarize_cso_grades(
    grades: tuple[CsoGradeResult, ...],
) -> CsoInspectionStatus | None:
    """미태깅 > C/S > 오류(미지원 포함) > O > 생략 순으로 집계한다."""
    if not grades:
        return None
    if any(grade.status is CsoGradeStatus.UNTAGGED for grade in grades):
        return CsoInspectionStatus.UNTAGGED
    if any(
        grade.status is CsoGradeStatus.TAGGED and grade.is_blocking for grade in grades
    ):
        return CsoInspectionStatus.BLOCKED
    if any(grade.status is CsoGradeStatus.ERROR for grade in grades):
        return CsoInspectionStatus.ERROR
    if all(grade.status is CsoGradeStatus.SKIPPED for grade in grades):
        return CsoInspectionStatus.SKIPPED
    return CsoInspectionStatus.OPEN
