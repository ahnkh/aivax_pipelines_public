"""출력 분석 HTTP 계약을 application 입력·응답으로 변환한다."""

import logging

from fastapi import HTTPException

from src.controllers.http_controller_mapper import HttpControllerMapper
from src.controllers.output_analysis.v1.dto import OutputFilterItem
from src.domain.analysis_log_delivery.models.user_consent import is_user_consent_on
from src.domain.analysis_log_merge.models.output_log import OutputLogInput


class OutputAnalysisMapper(HttpControllerMapper):
    """출력 분석 endpoint의 요청·성공 응답·오류 응답을 소유한다."""

    _API_COMMAND = "output api router"

    @staticmethod
    def to_output_log_input(model: OutputFilterItem) -> OutputLogInput:
        return OutputLogInput(
            message_id=model.message_id.strip(),
            llm_output=model.llm_output,
            session_id=model.session_id,
            user_id=model.user_id,
            email=model.email,
            ai_service=model.ai_service,
            client_host=model.client_host,
            user_consent=is_user_consent_on(model.user_consent),
            generated_only=model.generated_only,
            output_revision=model.output_revision,
            logged_at=(model.logged_at or "").strip(),
            model=(model.model or "").strip(),
            generated_files=tuple(
                {
                    "file_name": item.file_name,
                    "file_backup_path": item.file_backup_path,
                    "mime_type": item.mime_type,
                    "size": item.size,
                }
                for item in model.generated_files
                if item.file_name.strip() and item.file_backup_path.strip()
            ),
        )

    @classmethod
    def to_success_response(cls, result: dict) -> dict:
        return cls.success_response(api_command=cls._API_COMMAND, result=result)

    @classmethod
    def to_unavailable_http_error(cls, error: Exception) -> HTTPException:
        return cls.error_response(
            status_code=404,
            api_command=cls._API_COMMAND,
            api_message_code="Resource Not Found Error",
            result=error,
            logger=logging.getLogger("khan-python"),
        )
