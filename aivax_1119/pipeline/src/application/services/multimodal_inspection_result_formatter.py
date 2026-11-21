"""운영 검사와 정책 테스트의 파일별 정책 탐지 결과를 변환한다."""

from src.domain.inspection_multimodal.models import MultimodalFileInspectionResult


def format_multimodal_matches(
    result: MultimodalFileInspectionResult,
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "policy_id": match.policy.id,
            "policy_name": match.policy.name,
            "action": match.policy.action,
            "target": match.policy.target,
            "targets": match.policy.target,
            "category": match.policy.category,
            "scope": match.policy.scope,
            "match": match.policy.matched_text,
            "file_name": result.file_name,
            "expression": match.policy.expression,
            "flags": match.policy.flags,
            "evidence": [
                {
                    "matched_text": item.matched_text,
                    "page": item.page_number,
                    "line": item.line_number,
                    "context": item.context,
                }
                for item in match.evidence
            ],
        }
        for match in result.matches
    )
