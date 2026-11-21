"""V2 HTTP 계약과 분석 도메인 모델 변환."""

import logging

from src.controllers.input_analysis.cso_response import build_cso_response
from src.controllers.input_analysis.v2.dto import (
    CsoResponseV2,
    FinalDecisionV2,
    InputAnalysisRequestV2,
    InputAnalysisResponseV2,
)
from src.core.network import IpAddressConverter
from src.domain.inspection_input.models import (
    INPUT_LOG_REQUEST_NAME,
    InputAnalysisRequest,
    InputAnalysisResult,
    InputAnalysisUser,
    InputAttachment,
    InputInspectionAction,
)


class InputAnalysisMapperV2:
    @staticmethod
    def to_input_request(item: InputAnalysisRequestV2) -> InputAnalysisRequest:
        client_host = item.client_host or ""
        normalized_host = IpAddressConverter.normalize(client_host)
        if normalized_host is None and client_host.strip():
            logging.getLogger("khan-python").warning(
                "invalid client ip, replace empty string, ip=%s", client_host.strip()
            )
        return InputAnalysisRequest(
            # V2에서 로그는 선택 필터가 아니다. 기존 use case에만 내부 신호로 전달한다.
            requested_filter_names=(INPUT_LOG_REQUEST_NAME, *(item.filter_list or ())),
            prompt=item.prompt,
            message_id=item.message_id,
            user=InputAnalysisUser(
                name=item.user_name or "",
                email=item.email or "",
                ai_service=item.ai_service or 0,
                client_host=normalized_host or "",
            ),
            attachments=tuple(
                InputAttachment(
                    id=attachment.id or "",
                    name=attachment.name or "",
                    size=attachment.size or 0,
                    mime_type=attachment.mime_type or "",
                )
                for attachment in item.attachments or ()
            ),
            event_time=item.event_time,
            model=(item.model or "").strip(),
            user_consent=item.user_consent,
        )

    @staticmethod
    def to_input_response(
        request: InputAnalysisRequest,
        result: InputAnalysisResult,
    ) -> InputAnalysisResponseV2:
        decision = result.final_decision
        cso_blocks = bool(result.cso_block_message)
        action = InputInspectionAction.BLOCK if cso_blocks else decision.action
        cso = build_cso_response(result, action)
        return InputAnalysisResponseV2(
            cso=CsoResponseV2.model_validate(cso) if cso is not None else None,
            final_decision=FinalDecisionV2(
                action=action.value,
                masked_contents="" if cso_blocks else decision.masked_prompt,
                block_message=result.cso_block_message or decision.block_message,
                message_id=request.message_id,
            ),
        )
