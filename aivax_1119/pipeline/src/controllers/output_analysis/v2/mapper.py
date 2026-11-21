"""V2 HTTP 계약과 분석 도메인 모델 변환."""

from src.controllers.output_analysis.v2.dto import OutputAnalysisRequestV2
from src.domain.analysis_log_delivery.models.user_consent import is_user_consent_on
from src.domain.analysis_log_merge.models.output_log import OutputLogInput


class OutputAnalysisMapperV2:
    @staticmethod
    def to_output_input(item: OutputAnalysisRequestV2) -> OutputLogInput:
        return OutputLogInput(
            message_id=item.message_id,
            llm_output=item.llm_output,
            user_id=item.user_id,
            email=item.email,
            ai_service=item.ai_service,
            client_host=item.client_host,
            model=(item.model or "").strip(),
            user_consent=is_user_consent_on(item.user_consent),
        )
