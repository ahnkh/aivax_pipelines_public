"""출력 서비스 전환 전후의 응답·병합·차단 후 기록 계약을 검증한다."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.use_cases.output_analysis import OutputAnalysisUseCase
from src.controllers.output_analysis.v1.dto import OutputFilterItem
from src.controllers.output_analysis.v1.output_analysis_controller import (
    OutputAnalysisController,
)
from src.domain.analysis_log_merge.models.output_log import OutputLogInput
from src.domain.analysis_log_merge.models.output_log_options import OutputLogOptions
from tests.ai_service_catalog import make_ai_service_catalog


def make_app(options: OutputLogOptions = OutputLogOptions()):
    record_output = Mock(return_value=1)
    service = OutputAnalysisUseCase(
        SimpleNamespace(record_output=record_output),
        options,
        ai_service_catalog=make_ai_service_catalog(),
    )
    return SimpleNamespace(
        GetOutputAnalysisUseCase=lambda: service,
        controller=OutputAnalysisController(service),
        record_output=record_output,
        service=service,
    )


@pytest.mark.parametrize(
    ("ai_service", "service_name"),
    [(9, "GitHub Copilot"), (10, "openapi.copilot"), (13, "Genspark")],
)
def test_service_code_is_named_in_the_output_log(ai_service, service_name):
    app = make_app()
    asyncio.run(
        app.controller.write_ouput_response(
            OutputFilterItem(
                message_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                llm_output="안녕하세요",
                ai_service=ai_service,
                client_host="10.0.50.60",
            ),
            None,
        )
    )
    _, document = app.record_output.call_args.args
    assert document["ai_service_id"] == ai_service
    assert document["ai_service"] == service_name
    assert document["output_text"] == "안녕하세요"


@pytest.mark.parametrize(
    "text",
    ["", "회의 요약입니다.", "가" * 70000],
    ids=["empty", "normal", "utf8-truncation"],
)
def test_output_response_and_document_contract(text):
    app = make_app()
    result = asyncio.run(
        app.controller.write_ouput_response(
            OutputFilterItem(
                llm_output=text,
                message_id="output-contract",
                session_id="session",
                user_id="user",
                email="user@example.com",
                ai_service=1,
                client_host="127.0.0.1",
            ),
            None,
        )
    )
    assert result["api_code"] == 200
    assert result["api_message_code"] == "success"
    assert result["api_command"] == "output api router"
    assert result["completed_date"]
    assert result["out_result"] == {}
    key, document = app.record_output.call_args.args
    assert key == document["message_id"] == "output-contract"
    assert document["output_text"] == text.encode()[:200000].decode(errors="ignore")
    assert document["user_email"] == "user@example.com"
    assert document["ai_service_id"] == 1
    assert document["ai_service"] == "GPT"
    assert document["client_host"] == "127.0.0.1"


def test_output_failure_does_not_record_partial_log():
    app = make_app()
    app.service._build_analysis_log = Mock(side_effect=RuntimeError("failure"))
    with pytest.raises(RuntimeError, match="failure"):
        asyncio.run(app.controller.write_ouput_response(OutputFilterItem(), None))
    app.record_output.assert_not_called()


def test_merge_error_return_preserves_existing_http_success_contract():
    app = make_app()
    app.record_output.return_value = -1
    result = asyncio.run(app.controller.write_ouput_response(OutputFilterItem(), None))
    assert result["api_code"] == 200


def test_record_output_runs_outside_event_loop_thread():
    import threading

    app = make_app()
    loop_thread = threading.get_ident()
    thread_ids = []
    app.record_output.side_effect = lambda *args: thread_ids.append(
        threading.get_ident()
    )
    asyncio.run(
        app.GetOutputAnalysisUseCase().execute(OutputLogInput("thread", "response"))
    )
    assert len(thread_ids) == 1
    assert thread_ids[0] != loop_thread
