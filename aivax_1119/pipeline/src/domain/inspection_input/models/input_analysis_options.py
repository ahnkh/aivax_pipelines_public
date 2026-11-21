"""입력 필터 실행 및 최종 응답 판정 옵션."""

from dataclasses import dataclass

from src.domain.inspection_input.models.input_analysis_request import InputInspectorType


@dataclass(frozen=True, slots=True)
class InputAnalysisOptions:
    bypass_block: bool = False
    bypass_masking: bool = False
    user_consent_global_bypass_enabled: bool = False
    user_consent_regex_enabled: bool = False
    user_consent_multimodal_enabled: bool = True
    user_consent_slm_enabled: bool = False
    user_consent_secuxper_enabled: bool = False

    def is_user_consent_enabled(self, inspector_type: InputInspectorType) -> bool:
        return (
            self.user_consent_global_bypass_enabled
            and {
                InputInspectorType.REGEX: self.user_consent_regex_enabled,
                InputInspectorType.MULTIMODAL: self.user_consent_multimodal_enabled,
                InputInspectorType.SLM: self.user_consent_slm_enabled,
                InputInspectorType.SECUXPER: self.user_consent_secuxper_enabled,
            }[inspector_type]
        )
