"""입력 API 버전이 공유하는 CSO 상태와 안내 응답."""

from src.domain.inspection_input.models import (
    InputAnalysisResult,
    InputInspectionAction,
)
from src.domain.inspection_input.models.block_phrases import get_block_phrases
from src.domain.inspection_input.models.cso_grade_result import (
    summarize_cso_grades,
)


def build_cso_response(
    result: InputAnalysisResult,
    action: InputInspectionAction,
) -> dict[str, str] | None:
    status = summarize_cso_grades(result.cso_grades)
    if status is None:
        return None

    message = get_block_phrases().cso_guidance(
        status.value,
        blocked=action is InputInspectionAction.BLOCK,
    )
    return {"status": status.value, "message": message}
