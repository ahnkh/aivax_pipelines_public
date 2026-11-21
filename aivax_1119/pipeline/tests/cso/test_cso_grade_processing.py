"""CSO 차단 대상은 분석 없이 파일을 보관하고 최종 차단 로그를 기록한다."""

from tests.ai_service_catalog import make_ai_service_catalog
import asyncio
import json
import logging
import shutil
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.cso_fixtures import write_cso_docx as write_docx
from src.adapters.analysis_log_delivery import FluentBitAnalysisLogDeliveryAdapter
from src.adapters.analysis_log_merge import MemoryAnalysisLogMergeStore
from src.adapters.delivery_file import LocalFileDeliveryAdapter
from src.application.services.analysis_log_merge import AnalysisLogMergeService
from src.application.services.notification_attachment import (
    AttachmentNotificationService,
)
from src.application.use_cases.input_analysis import (
    InputAnalysisLogBuilder,
    InputAnalysisUseCase,
)
from src.application.use_cases.output_analysis import OutputAnalysisUseCase
from src.domain.inspection_input.models.visible_block_log import (
    logged_block_output,
    logged_block_output_for_result,
)
from src.bootstrap.env_loader import ConfigurationError, EnvironmentLoader
from src.bootstrap.pipeline_bootstrap import PipelineBootstrap
from src.controllers.input_analysis.v1.input_analysis_controller import (
    InputAnalysisController,
)
from src.controllers.input_analysis.v1.mapper import InputAnalysisMapper
from src.controllers.output_analysis.v1.output_analysis_controller import (
    OutputAnalysisController,
)
from src.core.constants import ERR_OK
from src.core.file import FileMetadata
from src.domain.analysis_log_delivery.errors import RetryableAnalysisLogDeliveryError
from src.domain.analysis_log_delivery.errors.consent_log_not_found_error import (
    ConsentLogNotFoundError,
)
from src.domain.config_aivax.models import AivaxConfig, AivaxMode
from src.domain.delivery_file.models import FileDeliveryResult
from src.domain.inspection_input.models import (
    InputAnalysisOptions,
    InputAnalysisRequest,
    InputAnalysisResult,
    InputAttachment,
    InputFinalDecision,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
    InputPolicyReference,
)
from src.domain.inspection_input.models.cso_grade_result import (
    CsoGradeResult,
    CsoGradeStatus,
)
from src.extensions.cso.fasoo_grade_adapter import FasooCsoGradeAdapter
from src.extensions.cso.guard import CsoInputGuard
from src.extensions.cso.jvm_process import CsoJvmProcess

CSO_DIRECTORY = Path(__file__).resolve().parents[2] / "third_party/fasoo/cso"
OPEN = CsoGradeResult(3, CsoGradeStatus.TAGGED)
SKIPPED = CsoGradeResult(None, CsoGradeStatus.SKIPPED)
UNSUPPORTED = CsoGradeResult(None, CsoGradeStatus.ERROR, unsupported=True)
BLOCKING_GRADES = (
    CsoGradeResult(1, CsoGradeStatus.TAGGED),
    CsoGradeResult(2, CsoGradeStatus.TAGGED),
    CsoGradeResult(None, CsoGradeStatus.UNTAGGED),
    CsoGradeResult(None, CsoGradeStatus.ERROR),
)


@pytest.fixture
def mime_metadata(monkeypatch):
    reader = Mock()
    formats = {
        "/docs/image": ("image/png", "png"),
        "/docs/first": ("image/png", "png"),
        "/docs/second": ("application/pdf", "pdf"),
        "/docs/third": ("text/plain", "txt"),
    }
    reader.read.side_effect = lambda path: FileMetadata(
        *formats.get(str(path), ("application/msword", "doc")), size_bytes=100
    )
    monkeypatch.setattr(
        "src.extensions.cso.guard.LocalFileMetadataReader", lambda: reader
    )
    return reader


def file_detection(document):
    return next(
        item for item in document["filter_detect"] if item["filter"] == "file block"
    )


@pytest.mark.parametrize(
    ("grades", "status"),
    (
        ((), None),
        ((OPEN,), "open"),
        ((OPEN, OPEN), "open"),
        ((SKIPPED,), "skipped"),
        ((SKIPPED, SKIPPED), "skipped"),
        ((SKIPPED, OPEN), "open"),
        ((SKIPPED, BLOCKING_GRADES[0]), "blocked"),
        ((SKIPPED, BLOCKING_GRADES[2]), "untagged"),
        ((SKIPPED, BLOCKING_GRADES[3]), "error"),
        ((BLOCKING_GRADES[0],), "blocked"),
        ((BLOCKING_GRADES[1],), "blocked"),
        ((BLOCKING_GRADES[2],), "untagged"),
        ((BLOCKING_GRADES[3],), "error"),
        ((OPEN, BLOCKING_GRADES[0]), "blocked"),
        ((BLOCKING_GRADES[0], BLOCKING_GRADES[2]), "untagged"),
        ((BLOCKING_GRADES[2], BLOCKING_GRADES[0]), "untagged"),
        ((BLOCKING_GRADES[2], BLOCKING_GRADES[3]), "untagged"),
        ((BLOCKING_GRADES[3], BLOCKING_GRADES[2]), "untagged"),
        ((BLOCKING_GRADES[0], BLOCKING_GRADES[3]), "blocked"),
        ((BLOCKING_GRADES[3], BLOCKING_GRADES[0]), "blocked"),
        ((BLOCKING_GRADES[1], BLOCKING_GRADES[3]), "blocked"),
        ((BLOCKING_GRADES[3], BLOCKING_GRADES[1]), "blocked"),
        ((OPEN, BLOCKING_GRADES[3]), "error"),
        ((OPEN, BLOCKING_GRADES[2], BLOCKING_GRADES[3]), "untagged"),
        ((BLOCKING_GRADES[3], OPEN, BLOCKING_GRADES[2]), "untagged"),
        ((UNSUPPORTED,), "error"),
        ((OPEN, UNSUPPORTED), "error"),
        ((UNSUPPORTED, SKIPPED), "error"),
        ((BLOCKING_GRADES[0], UNSUPPORTED), "blocked"),
        ((BLOCKING_GRADES[2], UNSUPPORTED), "untagged"),
    ),
)
def test_cso_response_and_log_share_status_priority(grades, status):
    request = InputAnalysisRequest(
        (),
        "document summary",
        attachments=tuple(
            InputAttachment(id=f"/docs/{index}.docx", name=f"{index}.docx")
            for index in range(len(grades))
        ),
    )
    # 다른 필터의 최종 차단 여부와 CSO 상태는 독립적이다.
    result = InputAnalysisResult(
        executions=(),
        final_decision=InputFinalDecision(action=InputInspectionAction.BLOCK),
        cso_grades=grades,
    )
    document = InputAnalysisLogBuilder(make_ai_service_catalog()).build(request, result)
    response = InputAnalysisMapper.to_success_response(request, result)
    if status is None:
        assert "cso" not in document
        assert response["cso"] is None
    else:
        assert document["cso"]["status"] == status
        assert response["cso"]["status"] == status
        assert "blocked" not in document["cso"]
        assert [item["status"] for item in document["cso"]["files"]] == [
            grade.status.value for grade in grades
        ]
        assert all(
            set(item) == {"file_name", "file_path", "grade", "status"}
            for item in document["cso"]["files"]
        )


def make_use_case(
    reader,
    *,
    action=InputInspectionAction.ACCEPT,
    mode="inline",
    skip_unsupported_extensions_enabled=False,
    skip_untagged_enabled=False,
    skip_error_enabled=False,
    **kwargs,
):
    execution = InputInspectionResult(
        inspector_type=InputInspectorType.MULTIMODAL,
        action=action,
        block_message="기존 필터 차단",
        masked_prompt="[masked]" if action is InputInspectionAction.MASKING else "",
        policy=InputPolicyReference(id="policy-1", name="기존 정책"),
        details={"file_summary": [{"file_name": "a.docx"}]},
    )
    inspector = Mock(inspect=AsyncMock(return_value=execution))
    merge = kwargs.pop(
        "analysis_log_merge_service",
        Mock(deliver=Mock(return_value=ERR_OK), record_input=Mock(return_value=ERR_OK)),
    )
    builder = InputAnalysisLogBuilder(make_ai_service_catalog())
    files = kwargs.pop(
        "file_delivery", Mock(deliver=AsyncMock(return_value=FileDeliveryResult()))
    )
    notifications = kwargs.pop("attachment_notification_service", None)
    guard = (
        CsoInputGuard(
            reader,
            skip_unsupported_extensions_enabled=skip_unsupported_extensions_enabled,
            skip_untagged_enabled=skip_untagged_enabled,
            skip_error_enabled=skip_error_enabled,
            file_delivery=files,
            input_log_builder=builder,
            analysis_log_merge_service=merge,
            consent_log_writer=kwargs.get("consent_log_writer"),
            attachment_notification_service=notifications,
        )
        if reader is not None
        else None
    )
    use_case = InputAnalysisUseCase(
        regex_inspector=Mock(),
        multimodal_inspector=inspector,
        slm_inspector=Mock(),
        input_guard=guard,
        options=kwargs.pop("options", InputAnalysisOptions()),
        input_log_builder=builder,
        analysis_log_merge_service=merge,
        aivax_config=Mock(
            get_config=Mock(return_value=AivaxConfig(mode=AivaxMode(mode))),
        ),
        **kwargs,
    )
    request = InputAnalysisRequest(
        requested_filter_names=(InputInspectorType.MULTIMODAL.value,),
        prompt="문서 요약",
        message_id="cso-request",
        attachments=(InputAttachment(id="/docs/a.docx", name="a.docx"),),
    )
    return use_case, request, inspector, merge


@pytest.mark.parametrize("enabled", (False, True))
@pytest.mark.parametrize("consent", (False, True))
@pytest.mark.parametrize("grade", (OPEN, *BLOCKING_GRADES))
@pytest.mark.parametrize(
    ("option", "status"),
    (
        ("skip_untagged_enabled", CsoGradeStatus.UNTAGGED),
        ("skip_error_enabled", CsoGradeStatus.ERROR),
    ),
)
def test_post_lookup_skip_only_bypasses_selected_status(
    enabled, consent, grade, option, status
):
    reader = Mock(read_grades=AsyncMock(return_value=(grade,)))
    use_case, request, inspector, _ = make_use_case(reader, **{option: enabled})
    request = replace(
        request,
        user_consent=consent,
        requested_filter_names=("input_filter", "file_block_filter"),
    )

    outcome = asyncio.run(use_case.execute(request))

    reader.read_grades.assert_awaited_once_with(("/docs/a.docx",))
    expected = SKIPPED if enabled and grade.status is status else grade
    assert outcome.analysis_result.cso_grades == (expected,)
    file_log = outcome.log_evidence["cso"]["files"][0]
    assert file_log["status"] == expected.status.value
    assert set(file_log) == {"file_name", "file_path", "grade", "status"}
    if expected is SKIPPED:
        assert file_log["grade"] is None
        assert outcome.log_evidence["cso"]["status"] == "skipped"
    if expected.is_blocking:
        inspector.inspect.assert_not_awaited()
        assert outcome.analysis_result.cso_block_message
        assert (
            outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
        )
    else:
        assert not outcome.analysis_result.cso_block_message
        assert (
            outcome.analysis_result.final_decision.action
            is InputInspectionAction.ACCEPT
        )
        if consent:
            inspector.inspect.assert_not_awaited()
        else:
            inspector.inspect.assert_awaited_once_with(request)


@pytest.mark.parametrize("extension_skip", (False, True))
@pytest.mark.parametrize("untagged_skip", (False, True))
@pytest.mark.parametrize("error_skip", (False, True))
def test_cso_skip_options_apply_to_unsupported_untagged_and_error(
    extension_skip, untagged_skip, error_skip, mime_metadata
):
    missing = BLOCKING_GRADES[2]
    error = BLOCKING_GRADES[3]
    reader = Mock(read_grades=AsyncMock(return_value=(missing, error)))
    use_case, request, inspector, _ = make_use_case(
        reader,
        skip_unsupported_extensions_enabled=extension_skip,
        skip_untagged_enabled=untagged_skip,
        skip_error_enabled=error_skip,
    )
    request = replace(
        request,
        requested_filter_names=("input_filter", "file_block_filter"),
        attachments=(
            InputAttachment(id="/docs/image", name="image.png"),
            InputAttachment(id="/docs/document", name="document.docx"),
            InputAttachment(id="/docs/broken", name="broken.pdf"),
        ),
    )

    outcome = asyncio.run(use_case.execute(request))

    reader.read_grades.assert_awaited_once_with(("/docs/document", "/docs/broken"))
    expected_grade = SKIPPED if untagged_skip else missing
    assert outcome.analysis_result.cso_grades == (
        SKIPPED
        if extension_skip or error_skip
        else CsoGradeResult(None, CsoGradeStatus.ERROR, unsupported=True),
        expected_grade,
        SKIPPED if error_skip else error,
    )
    if untagged_skip and error_skip:
        inspector.inspect.assert_awaited_once_with(request)
        assert outcome.log_evidence["cso"]["status"] == "skipped"
    else:
        inspector.inspect.assert_not_awaited()
        assert outcome.log_evidence["cso"]["status"] == (
            "error" if untagged_skip else "untagged"
        )


@pytest.mark.parametrize("grade", (OPEN, *BLOCKING_GRADES[:2], BLOCKING_GRADES[3]))
def test_skipped_untagged_files_preserve_other_grades_and_block_reasons(grade):
    reader = Mock(read_grades=AsyncMock(return_value=(BLOCKING_GRADES[2], grade)))
    use_case, request, inspector, _ = make_use_case(reader, skip_untagged_enabled=True)
    request = replace(
        request,
        requested_filter_names=("input_filter", "file_block_filter"),
        attachments=(
            InputAttachment(id="/docs/first", name="untagged.docx"),
            InputAttachment(id="/docs/second", name="tagged.pdf"),
        ),
    )

    outcome = asyncio.run(use_case.execute(request))

    assert outcome.analysis_result.cso_grades == (SKIPPED, grade)
    files = outcome.log_evidence["cso"]["files"]
    assert files[0]["file_name"] == "untagged.docx"
    assert files[0]["status"] == "skipped"
    assert files[1]["file_name"] == "tagged.pdf"
    assert files[1]["status"] == grade.status.value
    assert "태깅 여부를 확인" not in outcome.analysis_result.cso_block_message
    if grade.is_open:
        inspector.inspect.assert_awaited_once_with(request)
        assert outcome.log_evidence["cso"]["status"] == "open"
    else:
        inspector.inspect.assert_not_awaited()
        assert outcome.log_evidence["cso"]["status"] == (
            "error" if grade.status is CsoGradeStatus.ERROR else "blocked"
        )


@pytest.mark.parametrize("failure", (RuntimeError("lookup failed"), ()))
def test_untagged_skip_does_not_bypass_lookup_or_response_errors(failure):
    lookup = AsyncMock()
    if isinstance(failure, Exception):
        lookup.side_effect = failure
    else:
        lookup.return_value = failure
    use_case, request, inspector, _ = make_use_case(
        Mock(read_grades=lookup), skip_untagged_enabled=True
    )

    outcome = asyncio.run(use_case.execute(request))

    lookup.assert_awaited_once_with(("/docs/a.docx",))
    inspector.inspect.assert_not_awaited()
    assert outcome.analysis_result.cso_grades == (BLOCKING_GRADES[3],)
    assert outcome.log_evidence["cso"]["status"] == "error"


@pytest.mark.parametrize("grade", (OPEN, *BLOCKING_GRADES[:3]))
def test_skipping_file_errors_preserves_other_grades_and_blocking(grade):
    reader = Mock(read_grades=AsyncMock(return_value=(BLOCKING_GRADES[3], grade)))
    use_case, request, inspector, _ = make_use_case(reader, skip_error_enabled=True)
    request = replace(
        request,
        requested_filter_names=("input_filter", "file_block_filter"),
        attachments=(
            InputAttachment(id="/docs/broken", name="broken.docx"),
            InputAttachment(id="/docs/other", name="other.pdf"),
        ),
    )

    outcome = asyncio.run(use_case.execute(request))

    assert outcome.analysis_result.cso_grades == (SKIPPED, grade)
    assert outcome.log_evidence["cso"]["status"] == (
        "open"
        if grade.is_open
        else ("untagged" if grade.status is CsoGradeStatus.UNTAGGED else "blocked")
    )
    if grade.is_open:
        inspector.inspect.assert_awaited_once_with(request)
        assert not outcome.analysis_result.cso_block_message
    else:
        inspector.inspect.assert_not_awaited()
        assert (
            outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
        )
        assert "broken.docx" not in outcome.analysis_result.cso_block_message


@pytest.mark.parametrize("enabled", (False, True))
@pytest.mark.parametrize(
    "failure",
    (
        OSError("Java unavailable"),
        TimeoutError("lookup timeout"),
        ValueError("invalid JSON"),
        (),
    ),
)
def test_error_skip_handles_lookup_failures_and_retains_exception_log(
    enabled, failure, caplog, mime_metadata
):
    lookup = AsyncMock()
    if isinstance(failure, Exception):
        lookup.side_effect = failure
    else:
        lookup.return_value = failure
    use_case, request, inspector, _ = make_use_case(
        Mock(read_grades=lookup),
        skip_error_enabled=enabled,
        skip_unsupported_extensions_enabled=True,
    )
    request = replace(
        request,
        requested_filter_names=("input_filter", "file_block_filter"),
        attachments=(
            InputAttachment(id="/docs/image", name="image.png"),
            *request.attachments,
        ),
    )

    outcome = asyncio.run(use_case.execute(request))

    lookup.assert_awaited_once_with(("/docs/a.docx",))
    assert any(
        record.exc_info
        and "CSO 등급 조회 실패 message_id=cso-request" in record.message
        for record in caplog.records
    )
    assert outcome.analysis_result.cso_grades == (
        SKIPPED,
        SKIPPED if enabled else BLOCKING_GRADES[3],
    )
    assert outcome.log_evidence["cso"]["status"] == ("skipped" if enabled else "error")
    if enabled:
        inspector.inspect.assert_awaited_once_with(request)
        assert (
            outcome.analysis_result.final_decision.action
            is InputInspectionAction.ACCEPT
        )
        assert not outcome.analysis_result.cso_block_message
    else:
        inspector.inspect.assert_not_awaited()
        assert (
            outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
        )


def test_error_skip_does_not_swallow_request_cancellation():
    reader = Mock(read_grades=AsyncMock(side_effect=asyncio.CancelledError))
    use_case, request, inspector, _ = make_use_case(reader, skip_error_enabled=True)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(use_case.execute(request))

    inspector.inspect.assert_not_awaited()


@pytest.mark.parametrize("level", (logging.DEBUG, logging.INFO))
def test_file_debug_logs_once_per_queried_file_before_lookup(
    level, caplog, monkeypatch, tmp_path, mime_metadata
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(logging.getLogger("khan-python"), "propagate", True)
    caplog.set_level(level, logger="khan-python")
    logs_before_lookup = []

    async def lookup(paths):
        logs_before_lookup.extend(
            record for record in caplog.records if record.name == "khan-python"
        )
        return (BLOCKING_GRADES[3], OPEN)

    reader = Mock(read_grades=AsyncMock(side_effect=lookup))
    use_case, request, inspector, _ = make_use_case(
        reader,
        skip_error_enabled=True,
        skip_unsupported_extensions_enabled=True,
    )
    request = replace(
        request,
        attachments=(
            InputAttachment(id="/docs/image", name="image.png"),
            InputAttachment(id="missing.docx", name=""),
            InputAttachment(id="/docs/open", name="open.docx"),
        ),
    )

    outcome = asyncio.run(use_case.execute(request))

    reader.read_grades.assert_awaited_once_with(("missing.docx", "/docs/open"))
    inspector.inspect.assert_awaited_once_with(request)
    assert outcome.analysis_result.cso_grades == (SKIPPED, SKIPPED, OPEN)
    logs = [record for record in caplog.records if record.name == "khan-python"]
    assert logs == logs_before_lookup
    if level == logging.INFO:
        assert not logs
        return
    assert len(logs) == 2
    assert all(record.levelno == logging.DEBUG for record in logs)
    assert all("Starting CSO file lookup" in record.message for record in logs)
    assert all("message_id='cso-request'" in record.message for record in logs)
    assert (
        "file_index=1 file_name='' file_path='missing.docx' "
        f"absolute_path={str(tmp_path / 'missing.docx')!r}" in logs[0].message
    )
    assert (
        "file_index=2 file_name='open.docx' file_path='/docs/open'" in logs[1].message
    )


@pytest.mark.parametrize(
    "attachment",
    ({}, {"id": None, "name": "sample.docx"}, {"id": "", "name": "sample.docx"}),
)
def test_http_empty_attachment_path_skips_cso_and_runs_requested_filters(attachment):
    reader = Mock(read_grades=AsyncMock(side_effect=AssertionError("no file path")))
    use_case, _, inspector, _ = make_use_case(reader)
    app = FastAPI()
    app.include_router(InputAnalysisController(use_case).router)

    with TestClient(app) as client:
        response = client.post(
            "/v1/filter/multiple_filter",
            json={
                "message_id": "empty-attachment-path",
                "prompt": "문서 요약",
                "filter_list": ["input_filter", "file_block_filter"],
                "attachments": [attachment],
                "debug": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["final_decision"]["action"] == "accept"
    reader.read_grades.assert_not_awaited()
    inspector.inspect.assert_awaited_once()
    use_case._input_guard._file_delivery.deliver.assert_not_called()
    assert inspector.inspect.await_args.args[0].attachments == ()
    assert response.json()["cso"] is None
    assert "cso" not in response.json()["log_evidence"]


@pytest.mark.parametrize("grade", (OPEN, *BLOCKING_GRADES))
def test_empty_attachment_slots_are_removed_without_changing_real_file_result(grade):
    reader = Mock(read_grades=AsyncMock(return_value=(grade,)))
    use_case, request, inspector, _ = make_use_case(reader)
    request = replace(
        request,
        requested_filter_names=("input_filter", "file_block_filter"),
        attachments=(
            InputAttachment(id="", name="empty.docx"),
            *request.attachments,
            InputAttachment(id="", name=""),
        ),
    )

    outcome = asyncio.run(use_case.execute(request))

    reader.read_grades.assert_awaited_once_with(("/docs/a.docx",))
    assert outcome.analysis_result.cso_grades == (grade,)
    assert [file["status"] for file in outcome.log_evidence["cso"]["files"]] == [
        grade.status.value,
    ]
    if grade.is_blocking:
        inspector.inspect.assert_not_awaited()
        assert (
            outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
        )
    else:
        inspector.inspect.assert_awaited_once_with(
            replace(request, attachments=(request.attachments[1],))
        )
        assert (
            outcome.analysis_result.final_decision.action
            is InputInspectionAction.ACCEPT
        )


@pytest.mark.parametrize("grade", (BLOCKING_GRADES[2], BLOCKING_GRADES[3]))
def test_skipping_cso_does_not_bypass_a_normal_filter_block(grade):
    reader = Mock(read_grades=AsyncMock(return_value=(grade,)))
    use_case, request, inspector, _ = make_use_case(
        reader,
        skip_untagged_enabled=True,
        skip_error_enabled=True,
        action=InputInspectionAction.BLOCK,
    )

    outcome = asyncio.run(use_case.execute(request))

    reader.read_grades.assert_awaited_once_with(("/docs/a.docx",))
    inspector.inspect.assert_awaited_once_with(request)
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
    assert not outcome.analysis_result.cso_block_message
    assert outcome.log_evidence["cso"]["files"][0]["status"] == "skipped"


@pytest.mark.parametrize("enabled", (False, True))
@pytest.mark.parametrize("consent", (False, True))
def test_unsupported_mime_skip_is_optional_and_preserves_consent_flow(
    enabled, consent, mime_metadata
):
    reader = Mock(read_grades=AsyncMock(return_value=(BLOCKING_GRADES[2],)))
    use_case, request, inspector, _ = make_use_case(
        reader, skip_unsupported_extensions_enabled=enabled
    )
    request = replace(
        request,
        user_consent=consent,
        requested_filter_names=("input_filter", "file_block_filter"),
        attachments=(InputAttachment(id="/docs/image", name="사진.PNG"),),
    )

    outcome = asyncio.run(use_case.execute(request))

    if enabled:
        reader.read_grades.assert_not_awaited()
        assert (
            outcome.analysis_result.final_decision.action
            is InputInspectionAction.ACCEPT
        )
        assert not outcome.analysis_result.cso_block_message
        assert outcome.log_evidence["cso"] == {
            "status": "skipped",
            "files": [
                {
                    "file_name": "사진.PNG",
                    "file_path": "/docs/image",
                    "grade": None,
                    "status": "skipped",
                }
            ],
        }
        if consent:
            inspector.inspect.assert_not_awaited()
        else:
            inspector.inspect.assert_awaited_once_with(request)
    else:
        reader.read_grades.assert_not_awaited()
        inspector.inspect.assert_not_awaited()
        assert outcome.analysis_result.cso_block_message
        assert outcome.log_evidence["cso"]["status"] == "error"


@pytest.mark.parametrize(
    "file_name",
    (
        "a.doc",
        "a.DOCX",
        "a.xls",
        "a.XLSX",
        "a.ppt",
        "a.PPTX",
        "a.hwp",
        "a.HWPX",
        "a.pdf",
        "no-extension",
        "",
        ".hidden",
        "document.png",
        "document.txt",
    ),
)
def test_supported_mime_still_queries_cso_regardless_of_filename(
    file_name, mime_metadata
):
    reader = Mock(read_grades=AsyncMock(return_value=(BLOCKING_GRADES[2],)))
    use_case, request, inspector, _ = make_use_case(
        reader, skip_unsupported_extensions_enabled=True
    )
    request = replace(
        request,
        attachments=(InputAttachment(id="/docs/temp.png", name=file_name),),
    )

    outcome = asyncio.run(use_case.execute(request))

    reader.read_grades.assert_awaited_once_with(("/docs/temp.png",))
    inspector.inspect.assert_not_awaited()
    assert outcome.analysis_result.cso_block_message


@pytest.mark.parametrize("grade", (OPEN, *BLOCKING_GRADES))
def test_skipped_files_preserve_order_and_do_not_override_other_grades(
    grade, mime_metadata
):
    reader = Mock(read_grades=AsyncMock(return_value=(grade,)))
    use_case, request, inspector, _ = make_use_case(
        reader, skip_unsupported_extensions_enabled=True
    )
    request = replace(
        request,
        requested_filter_names=("input_filter", "file_block_filter"),
        attachments=(
            InputAttachment(id="/docs/first", name="image.png"),
            InputAttachment(id="/docs/second", name="document.pdf"),
            InputAttachment(id="/docs/third", name="archive.zip"),
        ),
    )

    outcome = asyncio.run(use_case.execute(request))

    reader.read_grades.assert_awaited_once_with(("/docs/second",))
    assert outcome.analysis_result.cso_grades == (SKIPPED, grade, SKIPPED)
    assert [item["file_name"] for item in outcome.log_evidence["cso"]["files"]] == [
        "image.png",
        "document.pdf",
        "archive.zip",
    ]
    if grade.is_open:
        inspector.inspect.assert_awaited_once_with(request)
        assert outcome.log_evidence["cso"]["status"] == "open"
        assert not outcome.analysis_result.cso_block_message
    else:
        inspector.inspect.assert_not_awaited()
        assert outcome.analysis_result.cso_block_message


@pytest.mark.parametrize("failure", (RuntimeError("lookup failed"), ()))
def test_lookup_failure_marks_only_queried_files_as_errors(failure, mime_metadata):
    lookup = AsyncMock()
    if isinstance(failure, Exception):
        lookup.side_effect = failure
    else:
        lookup.return_value = failure
    use_case, request, inspector, _ = make_use_case(
        Mock(read_grades=lookup), skip_unsupported_extensions_enabled=True
    )
    request = replace(
        request,
        attachments=(
            InputAttachment(id="/docs/image", name="image.png"),
            InputAttachment(id="/docs/document", name="document.docx"),
        ),
    )

    outcome = asyncio.run(use_case.execute(request))

    lookup.assert_awaited_once_with(("/docs/document",))
    inspector.inspect.assert_not_awaited()
    assert outcome.analysis_result.cso_grades == (SKIPPED, BLOCKING_GRADES[3])
    assert outcome.log_evidence["cso"]["status"] == "error"


def test_mime_skip_does_not_bypass_a_normal_filter_block(mime_metadata):
    reader = Mock(read_grades=AsyncMock())
    use_case, request, inspector, _ = make_use_case(
        reader,
        skip_unsupported_extensions_enabled=True,
        action=InputInspectionAction.BLOCK,
    )
    request = replace(
        request, attachments=(InputAttachment(id="/docs/image", name="image.png"),)
    )

    outcome = asyncio.run(use_case.execute(request))

    reader.read_grades.assert_not_awaited()
    inspector.inspect.assert_awaited_once_with(request)
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
    assert not outcome.analysis_result.cso_block_message
    assert outcome.log_evidence["cso"]["status"] == "skipped"


@pytest.mark.parametrize("grade", BLOCKING_GRADES)
def test_non_open_grade_skips_analysis_and_records_block_log(grade):
    reader = Mock(read_grades=AsyncMock(return_value=(grade,)))
    use_case, request, inspector, merge = make_use_case(reader)

    outcome = asyncio.run(use_case.execute(request))

    reader.read_grades.assert_awaited_once_with(("/docs/a.docx",))
    inspector.inspect.assert_not_called()
    decision = outcome.analysis_result.final_decision
    assert decision.action is InputInspectionAction.BLOCK
    assert decision.masked_prompt == ""
    assert decision.policy is None
    assert "[AIVAX] CSO 등급 차단" in outcome.analysis_result.cso_block_message
    assert outcome.analysis_result.executions == ()
    assert "동의합니다" not in outcome.analysis_result.cso_block_message
    assert outcome.log_evidence["filter_detect"][0]["policy_id"] == ""
    assert file_detection(outcome.log_evidence)["file_summary"]
    merge.record_input.assert_not_called()
    merge.deliver.assert_called_once()
    document = merge.deliver.call_args.args[0]
    assert document["mode"] == "block"
    assert document["policy"]["policy_id"] == ""
    assert document["masked_contents"] is None
    assert [item["filter"] for item in document["filter_detect"]] == [
        "regex",
        "file block",
        "slm",
    ]
    empty_fields = {
        "mode": "",
        "policy_id": "",
        "policy_name": "",
        "target": "",
        "category": "",
        "scope": "",
        "masked_contents": "",
        "evidence": [],
    }
    assert document["filter_detect"][0] == {"filter": "regex", **empty_fields}
    assert document["filter_detect"][2] == {
        "filter": "slm",
        **empty_fields,
        "slm_content": "",
    }
    assert document["filter_detect"][0]["mode"] == ""
    assert "started_at" not in document["filter_detect"][0]
    assert document["prompt"] == request.prompt
    assert document["output_text"] == logged_block_output_for_result(
        outcome.analysis_result, request.prompt
    )
    assert document["cso"]["status"] == (
        "blocked" if grade.status is CsoGradeStatus.TAGGED else grade.status.value
    )
    assert "blocked" not in document["cso"]
    assert "message" not in document["cso"]
    assert document["cso"]["files"][0]["status"] == grade.status.value
    assert "cso_grades" not in document and "blocked_by" not in document
    response = InputAnalysisMapper.to_success_response(request, outcome.analysis_result)
    response_decision = response["final_decision"]
    assert response_decision["action"] == "block"
    assert response_decision["action_code"] == 1
    assert response_decision["masked_contents"] == ""
    assert response_decision["block_message"] == outcome.analysis_result.cso_block_message


@pytest.mark.parametrize(
    ("status", "guidance"),
    (
        (CsoGradeStatus.UNTAGGED, "CSO 등급이 존재하지 않는 첨부파일"),
        (CsoGradeStatus.ERROR, "CSO 등급 확인에 실패한 첨부파일"),
    ),
)
def test_missing_grade_and_lookup_error_have_distinct_guidance(status, guidance):
    reader = Mock(read_grades=AsyncMock(return_value=(CsoGradeResult(None, status),)))
    use_case, request, _, _ = make_use_case(reader)

    outcome = asyncio.run(use_case.execute(request))

    assert guidance in outcome.analysis_result.cso_block_message
    assert "태깅 변경" not in outcome.analysis_result.cso_block_message


@pytest.mark.parametrize("grade", BLOCKING_GRADES)
@pytest.mark.parametrize("global_consent", (False, True))
@pytest.mark.parametrize("consent", (False, True))
def test_non_open_archives_all_files_and_registers_attachments_without_filters(
    tmp_path,
    grade,
    global_consent,
    consent,
):
    from docx import Document

    paths = [tmp_path / "private.docx", tmp_path / "open.docx"]
    for path in paths:
        document = Document()
        document.add_paragraph("document contents")
        document.save(path)

    async def read_grades(received):
        assert received == tuple(str(path) for path in paths)
        assert all(path.is_file() for path in paths)
        return (grade, OPEN)

    writer = FluentBitAnalysisLogDeliveryAdapter(tmp_path / "bulk")
    merge = AnalysisLogMergeService(MemoryAnalysisLogMergeStore(), writer)
    outbox = Mock()
    notification = AttachmentNotificationService(
        outbox,
        "aivax_log",
        shared_backup_roots=(tmp_path / "backup",),
    )
    use_case, request, inspector, _ = make_use_case(
        Mock(read_grades=AsyncMock(side_effect=read_grades)),
        options=InputAnalysisOptions(user_consent_global_bypass_enabled=global_consent),
        file_delivery=LocalFileDeliveryAdapter(tmp_path / "backup"),
        attachment_notification_service=notification,
        analysis_log_merge_service=merge,
        consent_log_writer=writer,
    )
    request = replace(
        request,
        attachments=tuple(
            InputAttachment(id=str(path), name=path.name) for path in paths
        ),
        requested_filter_names=tuple(kind.value for kind in InputInspectorType),
        user_consent=consent,
    )
    outcome = asyncio.run(use_case.execute(request))
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
    assert outcome.analysis_result.executions == ()
    for item in (use_case._regex_inspector, inspector, use_case._slm_inspector):
        item.inspect.assert_not_called()
    assert not any(path.exists() for path in paths)
    document = json.loads(next((tmp_path / "bulk").glob("*.json")).read_text())
    assert document["mode"] == "block"
    assert document["output_text"] == logged_block_output_for_result(
        outcome.analysis_result, request.prompt
    )
    assert document["cso"]["files"][0]["status"] == grade.status.value
    detect = file_detection(document)
    assert detect["filter"] == "file block" and detect["mode"] == ""
    assert "started_at" not in detect and "completed_at" not in detect
    assert outbox.enqueue.call_count == 2
    for summary, call in zip(
        detect["file_summary"], outbox.enqueue.call_args_list, strict=True
    ):
        backup = Path(summary["file_backup_path"])
        assert Document(backup).paragraphs[0].text == "document contents"
        assert summary["file_info"]["size"] == backup.stat().st_size
        assert summary["file_info"]["inspection_status"] == "skipped"
        assert summary["file_info"]["skip_reason"] == "cso_blocked"
        assert "hash" not in summary["file_info"]
        event = call.args[0]
        assert event.file_backup_path == str(backup)
        assert event.message_id == request.message_id


def test_cso_log_delivery_failure_is_retryable():
    use_case, request, inspector, _ = make_use_case(
        Mock(read_grades=AsyncMock(return_value=(BLOCKING_GRADES[0],))),
        analysis_log_merge_service=Mock(deliver=Mock(return_value=-1)),
    )
    with pytest.raises(RetryableAnalysisLogDeliveryError):
        asyncio.run(use_case.execute(request))
    inspector.inspect.assert_not_called()


@pytest.mark.parametrize(
    "action",
    (
        InputInspectionAction.ACCEPT,
        InputInspectionAction.MASKING,
        InputInspectionAction.BLOCK,
        InputInspectionAction.FAILED,
    ),
)
def test_open_grade_preserves_existing_decision(action):
    use_case, request, inspector, _ = make_use_case(
        Mock(read_grades=AsyncMock(return_value=(OPEN,))),
        action=action,
    )
    outcome = asyncio.run(use_case.execute(request))
    assert outcome.analysis_result.final_decision.action is action
    assert outcome.analysis_result.cso_grades == (OPEN,)
    assert outcome.analysis_result.cso_block_message == ""
    inspector.inspect.assert_awaited_once()
    response = InputAnalysisMapper.to_success_response(request, outcome.analysis_result)
    assert response["final_decision"]["action"] == action.value


@pytest.mark.parametrize(
    "failure",
    (
        OSError("Java 없음"),
        ValueError("잘못된 JSON"),
        TimeoutError(),
    ),
)
def test_lookup_exceptions_use_one_error_status_and_skip_analysis(failure):
    reader = Mock(read_grades=AsyncMock(side_effect=failure))
    use_case, request, inspector, _ = make_use_case(reader)
    outcome = asyncio.run(use_case.execute(request))
    inspector.inspect.assert_not_called()
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
    assert outcome.analysis_result.cso_block_message
    assert outcome.analysis_result.cso_grades == (BLOCKING_GRADES[-1],)
    assert outcome.log_evidence["cso"]["status"] == "error"


def test_cso_does_not_depend_on_requested_filters_or_ssl_bypass():
    use_case, request, inspector, merge = make_use_case(
        Mock(read_grades=AsyncMock(return_value=(BLOCKING_GRADES[0],))),
        options=InputAnalysisOptions(bypass_block=True, bypass_masking=True),
    )
    outcome = asyncio.run(use_case.execute(replace(request, requested_filter_names=())))
    inspector.inspect.assert_not_called()
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
    assert outcome.analysis_result.cso_block_message
    merge.deliver.assert_called_once()


def test_blank_attachment_slot_is_not_graded_or_blocked():
    reader = Mock(
        read_grades=AsyncMock(side_effect=AssertionError("빈 경로는 조회하지 않는다"))
    )
    use_case, request, inspector, merge = make_use_case(reader)
    request = replace(
        request,
        prompt="gdb 잡아보자",
        attachments=(InputAttachment(id="", size=0, name="", mime_type=""),),
    )

    outcome = asyncio.run(use_case.execute(request))

    reader.read_grades.assert_not_called()
    inspector.inspect.assert_awaited_once()
    assert outcome.analysis_result.cso_block_message == ""
    assert outcome.analysis_result.cso_grades == ()
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.ACCEPT
    merge.deliver.assert_not_called()


@pytest.mark.parametrize("skip_enabled", (False, True))
def test_no_attachments_skips_lookup_and_disabled_cso_keeps_existing_behavior(
    skip_enabled,
):
    reader = Mock(read_grades=AsyncMock())
    use_case, request, inspector, _ = make_use_case(
        reader,
        skip_unsupported_extensions_enabled=skip_enabled,
        skip_untagged_enabled=skip_enabled,
        skip_error_enabled=skip_enabled,
    )
    outcome = asyncio.run(use_case.execute(replace(request, attachments=())))
    reader.read_grades.assert_not_called()
    inspector.inspect.assert_awaited_once()
    assert outcome.analysis_result.cso_grades == ()
    use_case, request, _, _ = make_use_case(
        None,
        skip_unsupported_extensions_enabled=skip_enabled,
        skip_untagged_enabled=skip_enabled,
        skip_error_enabled=skip_enabled,
    )
    outcome = asyncio.run(use_case.execute(request))
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.ACCEPT


def test_mirror_accepts_before_analysis_without_looking_up_cso():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        reader = Mock(
            read_grades=AsyncMock(side_effect=AssertionError("no CSO in Mirror"))
        )
        use_case, request, inspector, merge = make_use_case(reader, mode="mirror")
        request = replace(
            request, requested_filter_names=("input_filter", "file_block_filter")
        )
        execution = inspector.inspect.return_value

        async def inspect(_request):
            started.set()
            await release.wait()
            return execution

        inspector.inspect.side_effect = inspect
        outcome = await asyncio.wait_for(use_case.execute(request), timeout=1)
        assert (
            outcome.analysis_result.final_decision.action is InputInspectionAction.BLANK
        )
        assert not outcome.analysis_result.cso_block_message
        assert outcome.analysis_result.cso_grades == ()
        await asyncio.wait_for(started.wait(), timeout=1)
        assert use_case._background_tasks
        merge.record_input.assert_not_called()
        release.set()
        await use_case.shutdown()
        inspector.inspect.assert_awaited_once_with(request)
        reader.read_grades.assert_not_called()
        use_case._input_guard._file_delivery.deliver.assert_not_called()
        merge.deliver.assert_not_called()
        assert "cso" not in merge.record_input.call_args.args[1]

    asyncio.run(scenario())


@pytest.mark.parametrize("grade", (OPEN, *BLOCKING_GRADES))
@pytest.mark.parametrize("global_consent", (False, True))
@pytest.mark.parametrize("consent", (False, True))
def test_mirror_ignores_cso_and_consent_and_runs_background_filters(
    grade, global_consent, consent
):
    async def scenario():
        writer = Mock()
        reader = Mock(read_grades=AsyncMock(return_value=(grade,)))
        use_case, request, inspector, merge = make_use_case(
            reader,
            mode="mirror",
            skip_unsupported_extensions_enabled=True,
            skip_untagged_enabled=True,
            skip_error_enabled=True,
            options=InputAnalysisOptions(
                user_consent_global_bypass_enabled=global_consent
            ),
            consent_log_writer=writer,
        )
        request = replace(
            request,
            user_consent=consent,
            requested_filter_names=("input_filter", "file_block_filter"),
        )
        outcome = await use_case.execute(request)
        assert (
            outcome.analysis_result.final_decision.action is InputInspectionAction.BLANK
        )
        assert not outcome.analysis_result.cso_block_message
        assert outcome.analysis_result.cso_grades == ()
        await use_case.shutdown()
        inspector.inspect.assert_awaited_once_with(request)
        reader.read_grades.assert_not_called()
        assert writer.mock_calls == []
        use_case._input_guard._file_delivery.deliver.assert_not_called()
        merge.deliver.assert_not_called()
        document = merge.record_input.call_args.args[1]
        assert document["filter_detect"][0]["policy_id"] == "policy-1"
        assert "cso" not in document and "user_consent" not in document

    asyncio.run(scenario())


@pytest.mark.parametrize("source", (".", "missing.txt"))
def test_invalid_attachment_path_is_not_moved(tmp_path, monkeypatch, source):
    monkeypatch.chdir(tmp_path)
    use_case, request, inspector, merge = make_use_case(
        Mock(read_grades=AsyncMock(return_value=(BLOCKING_GRADES[-1],))),
    )
    request = replace(
        request, attachments=(InputAttachment(id=source, name="invalid.txt"),)
    )
    asyncio.run(use_case.execute(request))
    use_case._input_guard._file_delivery.deliver.assert_not_called()
    inspector.inspect.assert_not_called()
    assert tmp_path.is_dir()
    document = merge.deliver.call_args.args[0]
    assert document["mode"] == "block"
    assert file_detection(document)["file_summary"][0]["file_backup_path"] == ""


def test_open_mirror_keeps_background_analysis_without_cso_log():
    async def scenario():
        use_case, request, inspector, merge = make_use_case(
            Mock(read_grades=AsyncMock(return_value=(OPEN,))),
            mode="mirror",
        )
        request = replace(
            request,
            requested_filter_names=("input_filter", "file_block_filter"),
        )
        outcome = await use_case.execute(request)
        assert outcome.debug_details_enabled is False
        decision = outcome.analysis_result.final_decision
        assert decision.action is not InputInspectionAction.BLOCK
        await use_case.shutdown()
        inspector.inspect.assert_awaited_once()
        merge.deliver.assert_not_called()
        document = merge.record_input.call_args.args[1]
        assert "cso" not in document

    asyncio.run(scenario())


@pytest.mark.parametrize("grade", (OPEN, BLOCKING_GRADES[0]))
def test_consent_replay_checks_current_attachments(grade):
    writer = Mock()
    writer.read_block_log.side_effect = ConsentLogNotFoundError("missing")
    use_case, request, inspector, merge = make_use_case(
        Mock(read_grades=AsyncMock(return_value=(grade,))),
        consent_log_writer=writer,
    )
    if grade.is_open:
        outcome = asyncio.run(use_case.execute(replace(request, user_consent=True)))
        assert (
            outcome.analysis_result.final_decision.action
            is InputInspectionAction.ACCEPT
        )
        writer.require_block_log.assert_not_called()
        inspector.inspect.assert_not_called()
        merge.deliver.assert_not_called()
    else:
        outcome = asyncio.run(use_case.execute(replace(request, user_consent=True)))
        writer.require_block_log.assert_not_called()
        inspector.inspect.assert_not_called()
        merge.deliver.assert_called_once()
        assert (
            outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
        )
        assert outcome.analysis_result.cso_block_message


def test_real_java_blank_grades_and_unsupported_files(tmp_path):
    if not all(shutil.which(executable) for executable in ("java", "javac")):
        pytest.skip("JDK 17+ required for library-tagged CSO fixtures")
    paths = [
        write_docx(tmp_path / f"blank-{index}.docx", grade)
        for index, grade in enumerate((None, "", " \t ", "\u2003"))
    ]
    text = tmp_path / "unsupported.txt"
    text.write_text("Generated text without document properties")
    archive_path = tmp_path / "unsupported.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("readme.txt", "Generated ordinary ZIP")
    paths.extend((text, archive_path, tmp_path / "missing.docx"))
    paths.append(write_docx(tmp_path / "invalid.docx", 9))
    paths.append(write_docx(tmp_path / "padded-open.docx", " 3 "))

    async def scenario():
        runtime = CsoJvmProcess(CSO_DIRECTORY)
        try:
            await runtime.start()
            return await FasooCsoGradeAdapter(runtime).read_grades(
                tuple(map(str, paths))
            )
        finally:
            await runtime.aclose()

    results = asyncio.run(scenario())

    assert results == (
        *(CsoGradeResult(None, CsoGradeStatus.UNTAGGED) for _ in range(6)),
        CsoGradeResult(None, CsoGradeStatus.ERROR),
        CsoGradeResult(None, CsoGradeStatus.ERROR),
        OPEN,
    )


def test_real_java_reuses_jvm_for_repeated_and_concurrent_grade_batches(tmp_path):
    if not all(shutil.which(executable) for executable in ("java", "javac")):
        pytest.skip("JDK 17+ required for library-tagged CSO fixtures")
    paths = [
        write_docx(tmp_path / f"문서 {grade}\n.docx", grade) for grade in (1, 2, 3)
    ]

    async def scenario():
        runtime = CsoJvmProcess(CSO_DIRECTORY)
        reader = FasooCsoGradeAdapter(runtime)
        try:
            await runtime.start()
            process = runtime._process
            assert process is not None
            for _ in range(2):
                results = await asyncio.gather(
                    *(reader.read_grades((str(path),)) for path in paths)
                )
                assert [batch[0].grade for batch in results] == [1, 2, 3]
                assert runtime._process is process
                assert process.returncode is None
        finally:
            await runtime.aclose()
        assert process.returncode is not None

    asyncio.run(scenario())


def test_real_java_missing_vendor_module_fails_startup_and_reaps_process(tmp_path):
    """CSO vendor 모듈이 없으면 준비 완료로 처리하지 않고 기동 실패시킨다."""
    if not shutil.which("java"):
        pytest.skip("Java runtime required")

    # 준비: 원본 모듈을 지우지 않고 vendor class/JAR가 없는 별도 경로를 사용한다.
    # 실제 JVM이 CsoJvmWorker를 실행하다 의존 모듈을 로드하지 못하도록 한다.
    logger = Mock()
    runtime = CsoJvmProcess(tmp_path, startup_timeout_seconds=5, logger=logger)

    async def scenario():
        try:
            # 실행·검증: 실제 로드 실패로 준비 응답을 받지 못하면 예외가 발생한다.
            with pytest.raises(asyncio.IncompleteReadError):
                await runtime.start()
            # 실패한 JVM은 정리되며 사용 가능한 프로세스로 남으면 안 된다.
            assert runtime._process is None
            assert runtime._stderr_task is None
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
    # 단순 파일 조회 실패가 아닌 vendor 모듈 로드 실패가 재현되었는지 확인한다.
    assert "NoClassDefFoundError" in str(logger.warning.call_args_list)


def test_real_java_lookup_before_file_move_and_http_block_log(tmp_path):
    if not all(shutil.which(executable) for executable in ("java", "javac")):
        pytest.skip("JDK 17+ required for library-tagged CSO fixtures")
    paths = [write_docx(tmp_path / f"등급 {grade}.docx", grade) for grade in (1, 2, 3)]
    paths.append(write_docx(tmp_path / "미태깅.docx"))
    png = tmp_path / "미지원.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    paths.extend(
        (
            png,
            tmp_path / "없는 파일.docx",
            write_docx(tmp_path / "잘못된 등급.docx", 9),
        )
    )
    paths.append(write_docx(tmp_path / "예외 이후 O.docx", 3))
    writer = FluentBitAnalysisLogDeliveryAdapter(tmp_path / "bulk")
    merge = AnalysisLogMergeService(MemoryAnalysisLogMergeStore(), writer)
    runtime = CsoJvmProcess(CSO_DIRECTORY)
    use_case, _, inspector, _ = make_use_case(
        FasooCsoGradeAdapter(runtime),
        analysis_log_merge_service=merge,
        file_delivery=LocalFileDeliveryAdapter(tmp_path / "backup"),
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            await runtime.start()
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(InputAnalysisController(use_case).router)
    with TestClient(app) as client:
        response = client.post(
            "/v1/filter/multiple_filter",
            json={
                "message_id": "real-cso",
                "prompt": "문서 요약",
                "filter_list": ["file_block_filter"],
                "attachments": [{"id": str(path), "name": path.name} for path in paths],
            },
        )
    assert response.status_code == 200
    decision = response.json()["final_decision"]
    assert decision["action"] == "block" and decision["action_code"] == 1
    document = json.loads(next((tmp_path / "bulk").glob("*.json")).read_text())
    assert document["output_text"] == logged_block_output(
        decision["block_message"],
        prompt="문서 요약",
        cso_status=document["cso"]["status"],
    )
    inspector.inspect.assert_not_called()
    assert document["filter_detect"][0]["mode"] == ""
    summaries = file_detection(document)["file_summary"]
    for source, summary in zip(paths, summaries, strict=True):
        if source.name == "없는 파일.docx":
            assert summary["file_backup_path"] == ""
        else:
            assert not source.exists()
            assert Path(summary["file_backup_path"]).is_file()
    assert [(r["grade"], r["status"]) for r in document["cso"]["files"]] == [
        (1, "tagged"),
        (2, "tagged"),
        (3, "tagged"),
        (None, "untagged"),
        (None, "error"),
        (None, "error"),
        (None, "error"),
        (3, "tagged"),
    ]
    assert document["mode"] == "block"
    assert document["policy"]["policy_id"] == ""
    assert document["filter_detect"][0]["policy_id"] == ""
    assert document["cso"]["status"] == "untagged"
    assert response.json()["cso"]["status"] == "untagged"
    assert "CSO 등급 변경 절차" not in response.json()["cso"]["message"]
    assert "CSO 등급 태깅 절차" in response.json()["cso"]["message"]
    assert "CSO 등급 조회에 실패" not in response.json()["cso"]["message"]
    assert "blocked" not in document["cso"]
    assert "message" not in document["cso"]
    assert list((tmp_path / "bulk/.consent-state").glob("*.json"))


@pytest.mark.parametrize("filter_block", (False, True))
@pytest.mark.parametrize(
    (
        "extension_skip",
        "untagged_skip",
        "error_skip",
        "grade",
        "action",
        "status",
    ),
    (
        (False, False, False, None, "block", "untagged"),
        (False, False, False, 1, "block", "blocked"),
        (False, False, False, 2, "block", "blocked"),
        (False, False, False, 9, "block", "error"),
        (True, False, False, None, "block", "untagged"),
        (False, True, False, None, "block", "error"),
        (True, True, False, None, "accept", "skipped"),
        (True, False, False, 3, "accept", "open"),
        (False, True, False, 3, "block", "error"),
        (True, True, False, 1, "block", "blocked"),
        (True, True, False, 2, "block", "blocked"),
        (True, True, False, 9, "block", "error"),
        (True, False, True, 9, "accept", "skipped"),
        (False, True, True, 9, "accept", "skipped"),
        (False, False, True, 9, "accept", "skipped"),
        (True, True, True, 1, "block", "blocked"),
        (True, True, True, 2, "block", "blocked"),
        (True, True, True, 3, "accept", "open"),
    ),
)
def test_real_java_skip_options_preserve_http_decision_and_stored_log(
    tmp_path,
    extension_skip,
    untagged_skip,
    error_skip,
    grade,
    action,
    status,
    filter_block,
):
    if not all(shutil.which(executable) for executable in ("java", "javac")):
        pytest.skip("JDK 17+ required for library-tagged CSO fixtures")
    document_path = write_docx(tmp_path / "document.docx", grade)
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    paths = (image_path, document_path)
    settings = EnvironmentLoader(environ={}).build_settings(
        {
            "AIVAX_PIPELINE_CSO_SKIP_UNSUPPORTED_EXTENSIONS_ENABLED": str(
                extension_skip
            ),
            "AIVAX_PIPELINE_CSO_SKIP_UNTAGGED_ENABLED": str(untagged_skip),
            "AIVAX_PIPELINE_CSO_SKIP_ERROR_ENABLED": str(error_skip),
        }
    )
    writer = FluentBitAnalysisLogDeliveryAdapter(tmp_path / "bulk")
    merge = AnalysisLogMergeService(MemoryAnalysisLogMergeStore(), writer)
    runtime = CsoJvmProcess(CSO_DIRECTORY)
    use_case, _, inspector, _ = make_use_case(
        FasooCsoGradeAdapter(runtime),
        action=InputInspectionAction.BLOCK
        if filter_block
        else InputInspectionAction.ACCEPT,
        skip_unsupported_extensions_enabled=(
            settings.cso_skip_unsupported_extensions_enabled
        ),
        skip_untagged_enabled=settings.cso_skip_untagged_enabled,
        skip_error_enabled=settings.cso_skip_error_enabled,
        analysis_log_merge_service=merge,
        file_delivery=LocalFileDeliveryAdapter(tmp_path / "backup"),
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            await runtime.start()
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(InputAnalysisController(use_case).router)
    app.include_router(OutputAnalysisController(OutputAnalysisUseCase(merge, ai_service_catalog=make_ai_service_catalog())).router)
    with TestClient(app) as client:
        response = client.post(
            "/v1/filter/multiple_filter",
            json={
                "message_id": "real-cso-skip",
                "prompt": "문서 요약",
                "debug": filter_block,
                "filter_list": ["input_filter", "file_block_filter"],
                "attachments": [{"id": str(path), "name": path.name} for path in paths],
            },
        )
        assert response.status_code == 200
        assert response.json()["final_decision"]["action"] == (
            "block" if filter_block else action
        )
        assert response.json()["cso"]["status"] == status
        assert set(response.json()["cso"]) == {"status", "message"}
        if status == "open":
            assert "결재가 상신됩니다." in response.json()["cso"]["message"]
            assert ("‘동의합니다’" in response.json()["cso"]["message"]) is filter_block
        if action == "accept":
            inspector.inspect.assert_awaited_once()
            if not filter_block:
                output = client.post(
                    "/v1/filter/output_filter",
                    json={"message_id": "real-cso-skip", "llm_output": "요약 결과"},
                )
                assert output.status_code == 200
            assert all(path.is_file() for path in paths)
        else:
            inspector.inspect.assert_not_awaited()
            assert not any(path.exists() for path in paths)

    saved = json.loads(next((tmp_path / "bulk").glob("*.json")).read_text())
    if filter_block:
        assert saved["cso"] == response.json()["log_evidence"]["cso"]
    else:
        assert "log_evidence" not in response.json()
        assert "filter_result" not in response.json()
    assert "message" not in saved["cso"]
    assert saved["cso"]["status"] == status
    assert [item["file_name"] for item in saved["cso"]["files"]] == [
        "image.png",
        "document.docx",
    ]
    image_log, document_log = saved["cso"]["files"]
    assert all(
        set(item) == {"file_name", "file_path", "grade", "status"}
        for item in saved["cso"]["files"]
    )
    if extension_skip or error_skip:
        assert image_log["status"] == "skipped"
    else:
        assert image_log["status"] == "error"
    if (grade is None and untagged_skip) or (grade == 9 and error_skip):
        assert document_log["status"] == "skipped"
        assert document_log["grade"] is None


@pytest.mark.parametrize(
    "stdout",
    (
        b"not json",
        b"[]",
        b"[{}]",
        b'[{"grade":true,"status":"tagged"}]',
        b'[{"grade":9,"status":"tagged"}]',
        b'[{"grade":3,"status":"untagged"}]',
        b'[{"grade":null,"status":"unknown"}]',
        b'[{"grade":null,"status":"unavailable"}]',
        b'[{"grade":null,"status":"skipped"}]',
    ),
)
def test_invalid_java_output_fails_closed(stdout):
    with pytest.raises((ValueError, KeyError)):
        FasooCsoGradeAdapter._parse_results(stdout, 1)


@pytest.mark.parametrize("enabled", (False, True))
def test_bootstrap_only_injects_reader_when_enabled(monkeypatch, tmp_path, enabled):
    settings = EnvironmentLoader(environ={}).build_settings(
        {
            "AIVAX_PIPELINE_CSO_ENABLED": str(enabled),
            "AIVAX_PIPELINE_CSO_REQUEST_TIMEOUT_SECONDS": "11",
            "AIVAX_PIPELINE_CSO_SKIP_UNSUPPORTED_EXTENSIONS_ENABLED": "true",
            "AIVAX_PIPELINE_CSO_SKIP_UNTAGGED_ENABLED": "true",
            "AIVAX_PIPELINE_CSO_SKIP_ERROR_ENABLED": "true",
        }
    )
    monkeypatch.chdir(tmp_path)
    runtime = Mock()
    runtime.aivax_config.get_config.return_value = AivaxConfig()
    bootstrap = PipelineBootstrap(settings, http_client_registry=None, logger=Mock())
    use_case = bootstrap._create_input_analysis_use_case(
        runtime,
        SimpleNamespace(multimodal=Mock(), slm=Mock()),
    )
    if enabled:
        assert (
            use_case._input_guard._cso_grade_reader._runtime._directory == CSO_DIRECTORY
        )
        assert use_case._input_guard._cso_grade_reader._runtime._timeout_seconds == 11
        assert use_case._input_guard._skip_unsupported_extensions_enabled is True
        assert use_case._input_guard._skip_untagged_enabled is True
        assert use_case._input_guard._skip_error_enabled is True
    else:
        assert use_case._input_guard is None


def test_cso_environment_enable_flag():
    loader = EnvironmentLoader(environ={})
    assert loader.build_settings({}).cso_enabled is False
    settings = loader.build_settings(
        {
            "AIVAX_PIPELINE_CSO_ENABLED": "true",
        }
    )
    assert settings.cso_enabled
    with pytest.raises(ConfigurationError):
        loader.build_settings({"AIVAX_PIPELINE_CSO_ENABLED": "invalid"})


@pytest.mark.parametrize(
    ("key", "field"),
    (
        (
            "AIVAX_PIPELINE_CSO_SKIP_UNSUPPORTED_EXTENSIONS_ENABLED",
            "cso_skip_unsupported_extensions_enabled",
        ),
        ("AIVAX_PIPELINE_CSO_SKIP_UNTAGGED_ENABLED", "cso_skip_untagged_enabled"),
        ("AIVAX_PIPELINE_CSO_SKIP_ERROR_ENABLED", "cso_skip_error_enabled"),
    ),
)
@pytest.mark.parametrize(
    ("value", "expected"),
    (("true", True), ("on", True), ("false", False), ("off", False)),
)
def test_cso_skip_environment_flags(key, field, value, expected):
    loader = EnvironmentLoader(environ={})
    assert getattr(loader.build_settings({}), field) is False
    settings = loader.build_settings({key: value})
    assert getattr(settings, field) is expected
    with pytest.raises(ConfigurationError, match=key):
        loader.build_settings({key: "invalid"})


def test_cso_environment_timeout_default_and_override():
    loader = EnvironmentLoader(environ={})
    assert loader.build_settings({}).cso_request_timeout_seconds == 5
    settings = loader.build_settings(
        {"AIVAX_PIPELINE_CSO_REQUEST_TIMEOUT_SECONDS": "7"}
    )
    assert settings.cso_request_timeout_seconds == 7


@pytest.mark.parametrize("value", ("0", "-1", "1.5", "invalid"))
def test_cso_environment_rejects_invalid_timeout(value):
    with pytest.raises(ConfigurationError, match="CSO_REQUEST_TIMEOUT_SECONDS"):
        EnvironmentLoader(environ={}).build_settings(
            {"AIVAX_PIPELINE_CSO_REQUEST_TIMEOUT_SECONDS": value}
        )


@pytest.mark.parametrize("unsupported_skip", (False, True))
@pytest.mark.parametrize("untagged_skip", (False, True))
@pytest.mark.parametrize("error_skip", (False, True))
def test_markany_skip_options_preserve_external_contract(
    unsupported_skip, untagged_skip, error_skip
):
    unsupported = CsoGradeResult(None, CsoGradeStatus.ERROR, unsupported=True)
    untagged = CsoGradeResult(None, CsoGradeStatus.UNTAGGED)
    error = CsoGradeResult(None, CsoGradeStatus.ERROR)
    reader = Mock(
        read_grades=AsyncMock(return_value=(unsupported, untagged, error, OPEN)),
        supports_mime_skip=False,
    )
    use_case, request, inspector, _ = make_use_case(
        reader,
        skip_unsupported_extensions_enabled=unsupported_skip,
        skip_untagged_enabled=untagged_skip,
        skip_error_enabled=error_skip,
    )
    request = replace(
        request,
        requested_filter_names=(*request.requested_filter_names, "input_filter"),
        attachments=tuple(
            InputAttachment(id=f"/docs/{i}", name=f"{i}.png") for i in range(4)
        ),
    )
    outcome = asyncio.run(use_case.execute(request))
    expected = (
        SKIPPED if unsupported_skip or error_skip else unsupported,
        SKIPPED if untagged_skip else untagged,
        SKIPPED if error_skip else error,
        OPEN,
    )
    assert outcome.analysis_result.cso_grades == expected
    reader.read_grades.assert_awaited_once_with(
        tuple(a.id for a in request.attachments)
    )
    blocked = not (untagged_skip and error_skip)
    assert outcome.analysis_result.final_decision.action is (
        InputInspectionAction.BLOCK if blocked else InputInspectionAction.ACCEPT
    )
    if blocked:
        inspector.inspect.assert_not_called()
    else:
        inspector.inspect.assert_awaited_once()
    response = InputAnalysisMapper.to_success_response(request, outcome.analysis_result)
    status = "untagged" if not untagged_skip else "error" if not error_skip else "open"
    assert response["cso"]["status"] == status
    assert outcome.log_evidence["cso"]["status"] == status
    files = outcome.log_evidence["cso"]["files"]
    assert [file["status"] for file in files] == [g.status.value for g in expected]
    assert all(
        set(file) == {"file_name", "file_path", "grade", "status"} for file in files
    )


@pytest.mark.parametrize("level", ("C", "O"))
def test_markany_uses_real_tags_before_normal_filters_despite_filename(tmp_path, level):
    from src.extensions.cso.markany_grade_adapter import MarkAnyCsoGradeAdapter

    path = tmp_path / "document.png"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "docProps/custom.xml",
            f'<property name="N2SF_Level"><vt:lpwstr>{level}</vt:lpwstr></property>',
        )
    use_case, request, inspector, _ = make_use_case(
        MarkAnyCsoGradeAdapter(), skip_unsupported_extensions_enabled=True
    )
    request = replace(
        request, attachments=(InputAttachment(id=str(path), name=path.name),)
    )
    outcome = asyncio.run(use_case.execute(request))
    grade = outcome.analysis_result.cso_grades[0]
    assert grade.grade == {"C": 1, "O": 3}[level]
    assert grade.status is CsoGradeStatus.TAGGED
    if level == "C":
        assert (
            outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
        )
        inspector.inspect.assert_not_called()
    else:
        assert (
            outcome.analysis_result.final_decision.action
            is InputInspectionAction.ACCEPT
        )
        inspector.inspect.assert_awaited_once()


@pytest.mark.parametrize("skip", (False, True))
@pytest.mark.parametrize("error_skip", (False, True))
@pytest.mark.parametrize("untagged_skip", (False, True))
@pytest.mark.parametrize("consent", (False, True))
@pytest.mark.parametrize("provider", ("fasoo", "markany"))
def test_providers_classify_unsupported_as_error_or_skipped(
    tmp_path, provider, skip, error_skip, untagged_skip, consent
):
    from PIL import Image

    from src.extensions.cso.markany_grade_adapter import MarkAnyCsoGradeAdapter

    path = tmp_path / "document.docx"
    Image.new("RGB", (2, 2)).save(path, format="PNG")
    unsupported = CsoGradeResult(None, CsoGradeStatus.ERROR, unsupported=True)
    runtime = Mock(
        request=AsyncMock(side_effect=AssertionError("unsupported file reached JVM"))
    )
    reader = (
        FasooCsoGradeAdapter(runtime)
        if provider == "fasoo"
        else MarkAnyCsoGradeAdapter()
    )
    use_case, request, inspector, _ = make_use_case(
        reader,
        skip_unsupported_extensions_enabled=skip,
        skip_untagged_enabled=untagged_skip,
        skip_error_enabled=error_skip,
    )
    request = replace(
        request,
        user_consent=consent,
        requested_filter_names=(*request.requested_filter_names, "input_filter"),
        attachments=(
            InputAttachment(id=str(path), name=path.name, mime_type="application/pdf"),
        ),
    )
    outcome = asyncio.run(use_case.execute(request))
    response = InputAnalysisMapper.to_success_response(request, outcome.analysis_result)
    skip = skip or error_skip
    assert response["cso"]["status"] == ("skipped" if skip else "error")
    assert response["final_decision"]["action"] == ("accept" if skip else "block")
    assert outcome.analysis_result.cso_grades == (SKIPPED if skip else unsupported,)
    assert outcome.log_evidence["cso"] == {
        "status": "skipped" if skip else "error",
        "files": [
            {
                "file_name": path.name,
                "file_path": str(path),
                "grade": None,
                "status": "skipped" if skip else "error",
            }
        ],
    }
    runtime.request.assert_not_awaited()
    if not skip or consent:
        inspector.inspect.assert_not_called()
    else:
        inspector.inspect.assert_awaited_once()


@pytest.mark.parametrize(
    ("other", "status", "message"),
    [
        (None, "error", "조회 불가"),
        (CsoGradeResult(1, CsoGradeStatus.TAGGED), "blocked", "등급 변경"),
        (CsoGradeResult(None, CsoGradeStatus.UNTAGGED), "untagged", "태깅 안내"),
    ],
)
def test_markany_unsupported_guidance_respects_other_block_reasons(
    monkeypatch, other, status, message
):
    from src.domain.inspection_input.models.block_phrases import BlockPhrases

    phrases = BlockPhrases(
        cso_title="CSO 차단",
        cso_error="조회 실패",
        cso_guidance_error="조회 불가",
        cso_guidance_blocked="등급 변경",
        cso_guidance_untagged="태깅 안내",
    )
    monkeypatch.setattr("src.extensions.cso.guard.get_block_phrases", lambda: phrases)
    monkeypatch.setattr(
        "src.controllers.input_analysis.cso_response.get_block_phrases", lambda: phrases
    )
    unsupported = CsoGradeResult(None, CsoGradeStatus.ERROR, unsupported=True)
    grades = (unsupported,) if other is None else (unsupported, other)
    reader = Mock(read_grades=AsyncMock(return_value=grades), supports_mime_skip=False)
    use_case, request, _, _ = make_use_case(reader)
    request = replace(
        request,
        attachments=tuple(
            InputAttachment(id=f"/docs/{i}", name=f"{i}.txt")
            for i in range(len(grades))
        ),
    )
    outcome = asyncio.run(use_case.execute(request))
    response = InputAnalysisMapper.to_success_response(request, outcome.analysis_result)
    assert response["cso"] == {"status": status, "message": message}
    assert "조회 실패" in response["final_decision"]["block_message"]


@pytest.mark.parametrize("name", ("renamed.png", "document", ""))
@pytest.mark.parametrize("grade", (OPEN, *BLOCKING_GRADES[:2]))
def test_fasoo_mime_prescan_queries_office_content_regardless_of_name(
    tmp_path, name, grade
):
    from docx import Document

    path = tmp_path / "upload.png"
    document = Document()
    document.add_paragraph("MIME-based CSO lookup")
    document.save(path)
    reader = Mock(read_grades=AsyncMock(return_value=(grade,)))
    use_case, request, inspector, _ = make_use_case(
        reader, skip_unsupported_extensions_enabled=True
    )
    request = replace(
        request,
        attachments=(InputAttachment(id=str(path), name=name, mime_type="image/png"),),
    )
    outcome = asyncio.run(use_case.execute(request))
    reader.read_grades.assert_awaited_once_with((str(path),))
    assert outcome.analysis_result.cso_grades == (grade,)
    if grade.is_open:
        inspector.inspect.assert_awaited_once()
    else:
        inspector.inspect.assert_not_awaited()


@pytest.mark.parametrize("name", ("document.docx", "image", ""))
def test_fasoo_mime_prescan_skips_image_content_despite_document_name(tmp_path, name):
    from PIL import Image

    path = tmp_path / "upload.docx"
    Image.new("RGB", (2, 2)).save(path, format="PNG")
    reader = Mock(read_grades=AsyncMock())
    use_case, request, inspector, _ = make_use_case(
        reader, skip_unsupported_extensions_enabled=True
    )
    request = replace(
        request,
        requested_filter_names=("input_filter", "file_block_filter"),
        attachments=(
            InputAttachment(id=str(path), name=name, mime_type="application/pdf"),
        ),
    )
    outcome = asyncio.run(use_case.execute(request))
    reader.read_grades.assert_not_awaited()
    inspector.inspect.assert_awaited_once()
    assert outcome.analysis_result.cso_grades == (SKIPPED,)
    assert outcome.log_evidence["cso"]["status"] == "skipped"


@pytest.mark.parametrize("error_skip", (False, True))
@pytest.mark.parametrize("unsupported_skip", (False, True))
def test_fasoo_mime_failure_falls_back_to_lookup_and_error_policy(
    monkeypatch, caplog, error_skip, unsupported_skip
):
    metadata = Mock(read=Mock(side_effect=OSError("MIME read failed")))
    monkeypatch.setattr(
        "src.extensions.cso.guard.LocalFileMetadataReader", lambda: metadata
    )
    monkeypatch.setattr(logging.getLogger("khan-python"), "propagate", True)
    reader = Mock(read_grades=AsyncMock(return_value=(BLOCKING_GRADES[3],)))
    use_case, request, _, _ = make_use_case(
        reader,
        skip_unsupported_extensions_enabled=unsupported_skip,
        skip_error_enabled=error_skip,
    )
    outcome = asyncio.run(use_case.execute(request))
    reader.read_grades.assert_awaited_once_with(("/docs/a.docx",))
    assert outcome.analysis_result.cso_grades == (
        SKIPPED if error_skip else BLOCKING_GRADES[3],
    )
    assert "CSO MIME detection failed; continuing grade lookup" in caplog.text


@pytest.mark.parametrize("mime_skip", (False, True))
def test_mime_prescan_does_not_run_when_provider_handles_types(monkeypatch, mime_skip):
    metadata = Mock()
    monkeypatch.setattr(
        "src.extensions.cso.guard.LocalFileMetadataReader", lambda: metadata
    )
    reader = Mock(read_grades=AsyncMock(return_value=(OPEN,)), supports_mime_skip=False)
    use_case, request, _, _ = make_use_case(
        reader, skip_unsupported_extensions_enabled=mime_skip
    )
    outcome = asyncio.run(use_case.execute(request))
    assert outcome.analysis_result.cso_grades == (OPEN,)
    metadata.read.assert_not_called()
    reader.read_grades.assert_awaited_once()
