"""입력 분석 HTTP 계약과 application 모델 변환."""

from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException

from src.controllers.http_controller_mapper import HttpControllerMapper
from src.controllers.input_analysis.cso_response import build_cso_response
from src.controllers.input_analysis.v1.dto import InputAnalysisItem
from src.core.network import IpAddressConverter
from src.domain.analysis_log_delivery.models.user_consent import is_user_consent_on
from src.domain.inspection_input.models import (
    InputAnalysisRequest,
    InputAnalysisResult,
    InputAnalysisUser,
    InputAttachment,
    InputInspectionAction,
    InputInspectionResult,
)


class InputAnalysisMapper(HttpControllerMapper):
    _API_COMMAND = "pipeline multiple filter"
    _ACTION_CODES = {
        InputInspectionAction.ACCEPT: 0,
        InputInspectionAction.BLANK: 0,
        InputInspectionAction.FAILED: 0,
        InputInspectionAction.BLOCK: 1,
        InputInspectionAction.MASKING: 2,
    }

    @classmethod
    def to_request(cls, item: InputAnalysisItem) -> InputAnalysisRequest:
        message_id = item.message_id.strip() or secrets.token_hex(16)
        return InputAnalysisRequest(
            requested_filter_names=tuple(item.filter_list or ()),
            prompt=item.prompt,
            user=InputAnalysisUser(
                id=item.user_id or "",
                name=item.user_name or "",
                email=item.email or "",
                ai_service=item.ai_service or 0,
                client_host=cls._normalize_client_host(item.client_host or ""),
            ),
            session_id=item.session_id or "",
            message_id=message_id,
            event_time=item.event_time or "",
            model=(item.model or "").strip(),
            attachments=tuple(
                InputAttachment(
                    id=attachment.id or "",
                    size=attachment.size or 0,
                    name=attachment.name or "",
                    mime_type=attachment.mime_type or "",
                )
                for attachment in (item.attachments or ())
                if str(attachment.id or "").strip()
            ),
            debug=bool(item.debug),
            user_consent=is_user_consent_on(item.user_consent),
        )

    @classmethod
    def to_success_response(
        cls,
        request: InputAnalysisRequest,
        result: InputAnalysisResult,
        *,
        debug_details_enabled: bool = True,
        log_evidence: dict | None = None,
    ) -> dict:
        decision = result.final_decision
        # 로그에 사용하는 분석 판정은 유지하고 실제 차단 응답만 조립한다.
        cso_blocks = bool(result.cso_block_message)
        response_action = InputInspectionAction.BLOCK if cso_blocks else decision.action
        response: dict[str, object] = {
            "cso": build_cso_response(result, response_action),
            "final_decision": {
                "action": response_action.value,
                "action_code": cls._ACTION_CODES[response_action],
                "masked_contents": "" if cso_blocks else decision.masked_prompt,
                "block_message": result.cso_block_message or decision.block_message,
                "message_id": request.message_id,
            },
        }
        if request.debug and debug_details_enabled:
            response["filter_result"] = {
                execution.inspector_type.value: cls._to_filter_response(execution)
                for execution in result.executions
            }
            response["log_evidence"] = log_evidence or {}
        # multiple_filter는 develop의 최상위 판정 필드 계약을 유지한다.
        return {
            "api_code": 200,
            "api_message_code": "success",
            "api_command": cls._API_COMMAND,
            **response,
            "completed_date": cls.completed_date(),
        }

    @classmethod
    def to_unsupported_inspector_http_error(cls, error: Exception) -> HTTPException:
        return cls.error_response(
            status_code=404,
            api_command=cls._API_COMMAND,
            api_message_code="Resource Not Found Error",
            result=error,
            logger=logging.getLogger("khan-python"),
        )

    @classmethod
    def to_unavailable_http_error(cls, error: Exception) -> HTTPException:
        return cls.error_response(
            status_code=404,
            api_command=cls._API_COMMAND,
            api_message_code="Resource Not Found Error",
            result=error,
            logger=logging.getLogger("khan-python"),
        )

    @classmethod
    def _to_filter_response(cls, execution: InputInspectionResult) -> dict:
        policy = execution.policy
        return {
            "action": execution.action.value,
            "action_code": cls._ACTION_CODES[execution.action],
            "masked_contents": execution.masked_prompt,
            "block_message": execution.block_message,
            "policy_id": policy.id if policy else "",
            "policy_name": policy.name if policy else "",
            "target": policy.target if policy else "",
            "category": policy.category if policy else "",
            "scope": policy.scope if policy else "",
        }

    @staticmethod
    def _normalize_client_host(value: str) -> str:
        normalized = IpAddressConverter.normalize(value)
        if normalized is not None:
            return normalized
        if not value.strip():
            return ""
        logging.getLogger("khan-python").warning(
            "invalid client ip, replace empty string, ip=%s",
            value.strip(),
        )
        return ""
