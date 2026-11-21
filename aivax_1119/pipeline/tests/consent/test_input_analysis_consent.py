"""기존 차단 로그의 동의 갱신, CSO와 일반 분석 흐름 검증."""

from tests.ai_service_catalog import make_ai_service_catalog
import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import orjson
import pytest
from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.analysis_log_delivery import FluentBitAnalysisLogDeliveryAdapter
from src.adapters.analysis_log_merge import MemoryAnalysisLogMergeStore
from src.adapters.delivery_file import LocalFileDeliveryAdapter
from src.application.services.analysis_log_merge import AnalysisLogMergeService
from src.application.use_cases.input_analysis import (
    InputAnalysisLogBuilder,
    InputAnalysisUseCase,
    MultimodalInputInspector,
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
from src.controllers.output_analysis.v1.output_analysis_controller import (
    OutputAnalysisController,
)
from src.domain.analysis_log_delivery.errors import RetryableAnalysisLogDeliveryError
from src.domain.analysis_log_merge.models.output_log import OutputLogInput
from src.domain.config_aivax.models import AivaxConfig, AivaxMode
from src.domain.config_aivax.models.aivax_filters import AivaxFilters, FilterSettings
from src.domain.inspection_input.models import (
    InputAnalysisOptions,
    InputAnalysisRequest,
    InputAttachment,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
)
from src.domain.inspection_input.models.cso_grade_result import (
    CsoGradeResult,
    CsoGradeStatus,
)
from src.domain.inspection_multimodal.models import (
    FileMetadata,
    MultimodalFileInspectionResult,
    MultimodalInspectionResult,
    MultimodalMatchedPolicy,
    MultimodalPolicyMatch,
)
from src.extensions.cso.guard import CsoInputGuard


def write_docx(path, contents):
    document = Document()
    document.add_paragraph(contents)
    document.save(path)
    return path


def make_flow(tmp_path, actions=None, mode="inline", reader=None, options=None):
    actions = actions or [InputInspectionAction.BLOCK] * 4
    inspectors = [
        Mock(
            inspect=AsyncMock(
                return_value=InputInspectionResult(
                    inspector_type=kind,
                    action=action,
                    block_message="차단 안내"
                    if action is InputInspectionAction.BLOCK
                    else "",
                )
            )
        )
        for kind, action in zip(InputInspectorType, actions, strict=True)
    ]
    store = MemoryAnalysisLogMergeStore()
    writer = FluentBitAnalysisLogDeliveryAdapter(tmp_path / "bulk")
    delivery = Mock(wraps=writer)
    merge = AnalysisLogMergeService(store, delivery)
    config = Mock(
        get_config=Mock(
            return_value=AivaxConfig(
                mode=AivaxMode(mode),
                filters=AivaxFilters(secuxper=FilterSettings(True)),
            )
        )
    )
    files = Mock(
        deliver=AsyncMock(wraps=LocalFileDeliveryAdapter(tmp_path / "backup").deliver)
    )
    notifications = Mock()

    async def inspect_files(targets, **_kwargs):
        return MultimodalInspectionResult(
            action=actions[1].value,
            files=tuple(
                MultimodalFileInspectionResult(
                    source=target.source,
                    file_name=target.file_name,
                    metadata=FileMetadata(
                        "text/plain", "txt", target.source.path.stat().st_size
                    ),
                    matches=(
                        MultimodalPolicyMatch(
                            MultimodalMatchedPolicy(action=actions[1].value)
                        ),
                    ),
                )
                for target in targets
            ),
        )

    multimodal = MultimodalInputInspector(
        Mock(inspect=AsyncMock(side_effect=inspect_files)), files, notifications
    )

    async def inspect_multimodal(request):
        if request.attachments:
            return await multimodal.inspect(request)
        return inspectors[1].inspect.return_value

    inspectors[1].inspect.side_effect = inspect_multimodal
    use_case = InputAnalysisUseCase(
        regex_inspector=inspectors[0],
        multimodal_inspector=inspectors[1],
        slm_inspector=inspectors[2],
        secuxper_inspector=inspectors[3],
        options=options
        or InputAnalysisOptions(
            user_consent_global_bypass_enabled=True,
            user_consent_regex_enabled=True,
            user_consent_multimodal_enabled=True,
            user_consent_slm_enabled=True,
            user_consent_secuxper_enabled=True,
        ),
        input_log_builder=InputAnalysisLogBuilder(make_ai_service_catalog()),
        analysis_log_merge_service=merge,
        aivax_config=config,
        consent_log_writer=writer,
        input_guard=CsoInputGuard(
            reader,
            file_delivery=files,
            input_log_builder=InputAnalysisLogBuilder(make_ai_service_catalog()),
            analysis_log_merge_service=merge,
            consent_log_writer=writer,
            attachment_notification_service=notifications,
        )
        if reader is not None
        else None,
    )
    output = OutputAnalysisUseCase(merge, ai_service_catalog=make_ai_service_catalog())
    request = InputAnalysisRequest(
        tuple(kind.value for kind in InputInspectorType),
        "abcdef",
        message_id="message-1",
    )
    return use_case, output, request, inspectors, store, writer, files, notifications


def records(directory, pattern):
    return [
        orjson.loads(line)
        for path in directory.glob(pattern)
        for line in path.read_bytes().splitlines()
    ]


@pytest.mark.parametrize("kind", tuple(InputInspectorType))
@pytest.mark.parametrize("enabled", (False, True))
def test_filter_consent_option_controls_notice_without_gating_approval(
    tmp_path, kind, enabled
):
    actions = [InputInspectionAction.ACCEPT] * 4
    blocked_index = tuple(InputInspectorType).index(kind)
    actions[blocked_index] = InputInspectionAction.BLOCK
    options = InputAnalysisOptions(
        user_consent_global_bypass_enabled=True,
        **{
            f"user_consent_{item.name.lower()}_enabled": item is kind and enabled
            for item in InputInspectorType
        },
    )
    inputs, output, request, inspectors, store, *_ = make_flow(
        tmp_path, actions=actions, options=options
    )
    outcome = asyncio.run(inputs.execute(request))
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
    message = outcome.analysis_result.final_decision.block_message
    assert ("동의합니다" in message) is enabled
    original = records(tmp_path / "bulk", "*.json")[0]
    assert original["output_text"] == logged_block_output_for_result(
        outcome.analysis_result, request.prompt
    )
    assert original["mode"] == "block"
    counts = [item.inspect.await_count for item in inspectors]
    replay = asyncio.run(inputs.execute(replace(request, user_consent=True)))
    assert replay.analysis_result.final_decision.action is InputInspectionAction.ACCEPT
    assert replay.analysis_result.executions == ()
    assert [item.inspect.await_count for item in inspectors] == counts
    assert not records(tmp_path / "bulk", "*.json.consent")
    asyncio.run(output.execute(OutputLogInput(request.message_id, "answer")))
    patch = records(tmp_path / "bulk", "*.json.consent")[-1]
    assert patch["user_consent"] == "on"
    assert "effective_decision" not in patch
    assert "filter_detect" not in patch and "mode" not in patch


def test_consent_phrase_without_flag_does_not_approve_block(tmp_path):
    inputs, _, request, *_ = make_flow(
        tmp_path,
        actions=[
            InputInspectionAction.ACCEPT,
            InputInspectionAction.BLOCK,
            InputInspectionAction.ACCEPT,
            InputInspectionAction.ACCEPT,
        ],
        options=InputAnalysisOptions(user_consent_global_bypass_enabled=True),
    )
    request = replace(request, prompt=request.prompt + "\n동의합니다")
    outcome = asyncio.run(inputs.execute(request))
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK


@pytest.mark.parametrize(
    "grade",
    [
        CsoGradeResult(1, CsoGradeStatus.TAGGED),
        CsoGradeResult(2, CsoGradeStatus.TAGGED),
        CsoGradeResult(None, CsoGradeStatus.UNTAGGED),
        CsoGradeResult(None, CsoGradeStatus.ERROR),
    ],
)
def test_consent_does_not_override_changed_cso_grade(tmp_path, grade):
    reader = Mock(
        read_grades=AsyncMock(return_value=(CsoGradeResult(3, CsoGradeStatus.TAGGED),))
    )
    inputs, _, request, inspectors, store, _, files, notifications = make_flow(
        tmp_path, reader=reader
    )
    source = write_docx(tmp_path / "upload.docx", "original")
    request = replace(
        request, attachments=(InputAttachment(id=str(source), name="a.docx"),)
    )
    asyncio.run(inputs.execute(request))
    reader.read_grades.return_value = (grade,)
    result = asyncio.run(
        inputs.execute(replace(request, user_consent=True))
    ).analysis_result
    assert result.cso_block_message
    assert result.final_decision.action is InputInspectionAction.BLOCK
    inspectors[1].inspect.assert_not_called()
    assert not source.exists()
    files.deliver.assert_awaited_once()
    notifications.enqueue_archived_file.assert_called_once()
    update = records(tmp_path / "bulk", "*.json.consent")[-1]
    assert update["mode"] == "block"
    detections = {item["filter"]: item for item in update["filter_detect"]}
    assert len(detections) == len(update["filter_detect"])
    assert detections["regex"]["mode"] == "block"
    assert detections["slm"]["mode"] == ""
    assert detections["slm"]["slm_content"] == ""
    assert update["cso"]["status"] == (
        "blocked" if grade.status is CsoGradeStatus.TAGGED else grade.status.value
    )
    assert "blocked" not in update["cso"]
    assert "effective_decision" not in update
    assert update["output_text"] == logged_block_output_for_result(
        result, request.prompt
    )
    summary = next(
        item for item in update["filter_detect"] if item["filter"] == "file block"
    )["file_summary"][0]
    assert Path(summary["file_backup_path"]).is_file()
    asyncio.run(inputs.execute(replace(request, user_consent=True)))
    files.deliver.assert_awaited_once()
    reader.read_grades.assert_awaited_with((summary["file_backup_path"],))


def test_changed_cso_grade_reuses_file_already_archived_by_multimodal(
    tmp_path,
):
    reader = Mock(
        read_grades=AsyncMock(return_value=(CsoGradeResult(3, CsoGradeStatus.TAGGED),))
    )
    inputs, _, request, inspectors, store, _, files, notifications = make_flow(
        tmp_path,
        actions=[
            InputInspectionAction.ACCEPT,
            InputInspectionAction.ACCEPT,
            InputInspectionAction.BLOCK,
            InputInspectionAction.ACCEPT,
        ],
        reader=reader,
    )
    source = write_docx(tmp_path / "upload.docx", "contents")
    request = replace(
        request, attachments=(InputAttachment(id=str(source), name="a.docx"),)
    )
    asyncio.run(inputs.execute(request))
    reader.read_grades.return_value = (CsoGradeResult(2, CsoGradeStatus.TAGGED),)
    outcome = asyncio.run(inputs.execute(replace(request, user_consent=True)))
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
    files.deliver.assert_awaited_once()
    notifications.enqueue_archived_file.assert_called_once()
    inspectors[3].inspect.assert_not_called()
    update = records(tmp_path / "bulk", "*.json.consent")[-1]
    detect = next(
        item for item in update["filter_detect"] if item["filter"] == "file block"
    )
    assert detect["mode"] == "accept"
    assert detect["started_at"]
    assert Path(detect["file_summary"][0]["file_backup_path"]).is_file()


def test_http_consent_replaces_block_message_with_ai_response(tmp_path):
    inputs, output, request, *_ = make_flow(tmp_path)
    app = FastAPI()
    app.include_router(InputAnalysisController(inputs).router)
    app.include_router(OutputAnalysisController(output).router)
    body = {
        "message_id": request.message_id,
        "prompt": request.prompt,
        "filter_list": list(request.requested_filter_names),
        "debug": False,
    }
    with TestClient(app) as client:
        initial = client.post("/v1/filter/multiple_filter", json=body)
        assert initial.status_code == 200
        decision = initial.json()["final_decision"]
        assert decision["action"] == "block"
        assert decision["block_message"].endswith(
            "차단된 요청의 전송에 동의하시면, "
            "‘동의합니다’를 입력한 후 다시 전송해 주세요."
        )
        assert records(tmp_path / "bulk", "*.json")[0]["output_text"] == logged_block_output(
            decision["block_message"],
            prompt=request.prompt,
        )
        assert "filter_result" not in initial.json()
        original = records(tmp_path / "bulk", "*.json")[0]
        resumed = client.post(
            "/v1/filter/multiple_filter", json={**body, "user_consent": True}
        )
        assert resumed.status_code == 200
        assert resumed.json()["final_decision"]["action"] == "accept"
        writer = inputs._consent_log_writer
        consented = writer.read_block_log(request.message_id)
        assert consented["output_text"] == original["output_text"]
        assert "user_consent" not in consented
        assert not records(tmp_path / "bulk", "*.json.consent")
        final = client.post(
            "/v1/filter/output_filter",
            json={
                "message_id": request.message_id,
                "llm_output": "answer",
                "user_consent": True,
            },
        )
        assert final.status_code == 200
        assert final.json()["out_result"] == {}
    updated = writer.read_block_log(request.message_id)
    assert updated == {
        **original,
        "output_text": "answer",
        "text_truncated_bytes": len("answer"),
        "user_consent": "on",
    }
    assert len(records(tmp_path / "bulk", "*.json")) == 1
    assert all(
        patch["message_id"] == original["message_id"]
        and patch["@timestamp"] == original["@timestamp"]
        for patch in records(tmp_path / "bulk", "*.json.consent")
    )


@pytest.mark.parametrize("value, enabled", [("true", True), ("false", False)])
def test_global_consent_option_is_typed(value, enabled):
    settings = EnvironmentLoader(environ={}).build_settings(
        {
            "AIVAX_PIPELINE_USER_CONSENT_GLOBAL_BYPASS_ENABLED": value,
        }
    )
    assert settings.user_consent_global_bypass_enabled is enabled


def test_global_consent_option_rejects_invalid_value():
    with pytest.raises(ConfigurationError):
        EnvironmentLoader(environ={}).build_settings(
            {
                "AIVAX_PIPELINE_USER_CONSENT_GLOBAL_BYPASS_ENABLED": "invalid",
            }
        )


@pytest.mark.parametrize("kind", tuple(InputInspectorType))
@pytest.mark.parametrize("value", ("true", "false", "invalid"))
def test_filter_consent_environment_option_and_bootstrap(kind, value):
    loader = EnvironmentLoader(environ={})
    key = f"AIVAX_PIPELINE_USER_CONSENT_{kind.name}_ENABLED"
    values = {
        "AIVAX_PIPELINE_USER_CONSENT_GLOBAL_BYPASS_ENABLED": "true",
        key: value,
    }
    if value == "invalid":
        with pytest.raises(ConfigurationError):
            loader.build_settings(values)
        return
    settings = loader.build_settings(values)
    bootstrap = PipelineBootstrap(settings, http_client_registry=None, logger=Mock())
    options = bootstrap._create_input_analysis_options()
    assert options.is_user_consent_enabled(kind) is (value == "true")


def test_default_filter_consent_settings_enable_only_multimodal():
    settings = EnvironmentLoader(environ={}).build_settings(
        {"AIVAX_PIPELINE_USER_CONSENT_GLOBAL_BYPASS_ENABLED": "true"}
    )
    bootstrap = PipelineBootstrap(settings, http_client_registry=None, logger=Mock())
    options = bootstrap._create_input_analysis_options()
    assert tuple(
        kind for kind in InputInspectorType if options.is_user_consent_enabled(kind)
    ) == (InputInspectorType.MULTIMODAL,)


def test_completed_multimodal_is_skipped_and_its_backup_path_is_reused(tmp_path):
    inputs, _, request, inspectors, store, _, files, notifications = make_flow(
        tmp_path,
        actions=[
            InputInspectionAction.ACCEPT,
            InputInspectionAction.BLOCK,
            InputInspectionAction.BLOCK,
            InputInspectionAction.BLOCK,
        ],
    )
    source = tmp_path / "upload.txt"
    source.write_text("original")
    request = replace(
        request, attachments=(InputAttachment(id=str(source), name="a.txt"),)
    )
    asyncio.run(inputs.execute(request))
    assert not source.exists()
    document = inputs._consent_log_writer.read_block_log(request.message_id)
    backup = document["filter_detect"][1]["file_summary"][0]["file_backup_path"]
    assert Path(backup).read_text() == "original"
    asyncio.run(inputs.execute(replace(request, user_consent=True)))
    inspectors[1].inspect.assert_awaited_once()
    assert (
        files.deliver.await_count == notifications.enqueue_archived_file.call_count == 1
    )


def test_initial_log_failure_is_reported_before_consent(tmp_path, monkeypatch):
    inputs, _, request, inspectors, _, writer, *_ = make_flow(tmp_path)
    delivery = inputs._analysis_log_merge_service._delivery
    monkeypatch.setattr(delivery, "deliver", Mock(return_value=False))
    with pytest.raises(RetryableAnalysisLogDeliveryError):
        asyncio.run(inputs.execute(request))
    assert not records(tmp_path / "bulk", "*.json")
    monkeypatch.setattr(delivery, "deliver", writer.deliver)
    assert (
        asyncio.run(inputs.execute(request)).analysis_result.final_decision.action
        is InputInspectionAction.BLOCK
    )
    assert inspectors[0].inspect.await_count == 2
    for inspector in inspectors[1:]:
        inspector.inspect.assert_not_called()


def test_mirror_does_not_apply_consent_bypass(tmp_path):
    inputs, output, request, inspectors, store, *_ = make_flow(tmp_path, mode="mirror")

    async def scenario():
        await inputs.execute(request)
        await inputs.shutdown()
        await output.execute(OutputLogInput(request.message_id, "mirror response"))
        for inspector in inspectors:
            inspector.inspect.reset_mock()
        resumed = await inputs.execute(replace(request, user_consent=True))
        assert (
            resumed.analysis_result.final_decision.action is InputInspectionAction.BLANK
        )
        assert inputs._background_tasks
        await inputs.shutdown()
        inspectors[0].inspect.assert_awaited_once()
        for inspector in inspectors[1:]:
            inspector.inspect.assert_not_called()
        await output.execute(
            OutputLogInput(request.message_id, "consented response", user_consent=True)
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("global_enabled", [False, True])
def test_consent_bypasses_filters_and_stores_input(tmp_path, global_enabled):
    inputs, _, request, inspectors, *_ = make_flow(
        tmp_path,
        options=InputAnalysisOptions(user_consent_global_bypass_enabled=global_enabled),
    )
    merge_store = Mock(set_input_and_pop_output=Mock(return_value=None))
    inputs._analysis_log_merge_service._store = merge_store
    outcome = asyncio.run(inputs.execute(replace(request, user_consent=True)))
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.ACCEPT
    assert outcome.analysis_result.executions == ()
    assert outcome.analysis_result.write_input_log
    assert not inputs._background_tasks
    merge_store.set_input_and_pop_output.assert_called_once()
    document = merge_store.set_input_and_pop_output.call_args.args[1]
    assert document["user_consent"] == "on"
    assert document["prompt"] == request.prompt
    assert document["filter_detect"] == []
    for inspector in inspectors:
        inspector.inspect.assert_not_called()
    assert not records(tmp_path / "bulk", "*.json*")


def test_consent_preserves_original_detection_and_masking_logs(tmp_path):
    inputs, output, request, inspectors, store, writer, *_ = make_flow(
        tmp_path,
        actions=[
            InputInspectionAction.MASKING,
            InputInspectionAction.BLOCK,
            InputInspectionAction.BLOCK,
            InputInspectionAction.BLOCK,
        ],
    )
    inspectors[0].inspect.return_value = InputInspectionResult(
        InputInspectorType.REGEX,
        InputInspectionAction.MASKING,
        masked_prompt="masked",
        evidence=({"action": "masking", "start": 0, "end": 1},),
    )
    asyncio.run(inputs.execute(request))
    original = records(tmp_path / "bulk", "*.json")[0]
    counts = [item.inspect.await_count for item in inspectors]
    for _ in range(2):
        outcome = asyncio.run(inputs.execute(replace(request, user_consent=True)))
        assert (
            outcome.analysis_result.final_decision.action
            is InputInspectionAction.ACCEPT
        )
        assert outcome.analysis_result.final_decision.masked_prompt == ""
        asyncio.run(output.execute(OutputLogInput(request.message_id, "answer")))
    assert [item.inspect.await_count for item in inspectors] == counts
    updated = writer.read_block_log(request.message_id)
    assert updated["filter_detect"] == original["filter_detect"]
    assert updated["masked_contents"] == original["masked_contents"]
    assert updated["policy"] == original["policy"]
    assert updated["mode"] == "block"
    assert updated["user_consent"] == "on"
    assert "effective_decision" not in updated
    assert len(records(tmp_path / "bulk", "*.json")) == 1


def test_consent_does_not_move_files_when_cso_allows(tmp_path):
    reader = Mock(
        read_grades=AsyncMock(return_value=(CsoGradeResult(3, CsoGradeStatus.TAGGED),))
    )
    inputs, _, request, inspectors, _, _, files, notifications = make_flow(
        tmp_path,
        reader=reader,
    )
    source = write_docx(tmp_path / "upload.docx", "original")
    request = replace(
        request,
        user_consent=True,
        attachments=(InputAttachment(id=str(source), name="a.docx"),),
    )
    outcome = asyncio.run(inputs.execute(request))
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.ACCEPT
    reader.read_grades.assert_awaited_once_with((str(source),))
    assert Document(source).paragraphs[0].text == "original"
    files.deliver.assert_not_called()
    notifications.enqueue_archived_file.assert_not_called()
    for inspector in inspectors:
        inspector.inspect.assert_not_called()


def test_consent_preserves_cso_for_archived_files_without_valkey(tmp_path):
    reader = Mock(
        read_grades=AsyncMock(return_value=(CsoGradeResult(3, CsoGradeStatus.TAGGED),))
    )
    inputs, _, request, inspectors, store, writer, files, _ = make_flow(
        tmp_path,
        reader=reader,
        actions=[
            InputInspectionAction.ACCEPT,
            InputInspectionAction.BLOCK,
            InputInspectionAction.BLOCK,
            InputInspectionAction.BLOCK,
        ],
    )
    source = write_docx(tmp_path / "upload.docx", "original")
    request = replace(
        request, attachments=(InputAttachment(id=str(source), name="a.docx"),)
    )
    asyncio.run(inputs.execute(request))
    document = writer.read_block_log(request.message_id)
    backup = document["filter_detect"][1]["file_summary"][0]["file_backup_path"]
    assert not source.exists()
    for grade in (
        CsoGradeResult(3, CsoGradeStatus.TAGGED),
        CsoGradeResult(2, CsoGradeStatus.TAGGED),
    ):
        reader.read_grades.return_value = (grade,)
        outcome = asyncio.run(inputs.execute(replace(request, user_consent=True)))
        expected = (
            InputInspectionAction.ACCEPT
            if grade.is_open
            else InputInspectionAction.BLOCK
        )
        assert outcome.analysis_result.final_decision.action is expected
        reader.read_grades.assert_awaited_with((backup,))
    files.deliver.assert_awaited_once()
    for inspector in inspectors[:2]:
        inspector.inspect.assert_awaited_once()
    for inspector in inspectors[2:]:
        inspector.inspect.assert_not_called()


@pytest.mark.parametrize("flag", [False, None])
def test_non_consent_keeps_normal_filter_flow(tmp_path, flag):
    inputs, _, request, inspectors, *_ = make_flow(tmp_path)
    outcome = asyncio.run(inputs.execute(replace(request, user_consent=flag)))
    assert outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
    inspectors[0].inspect.assert_awaited_once()
    for inspector in inspectors[1:]:
        inspector.inspect.assert_not_called()


@pytest.mark.parametrize("global_enabled", [False, True])
def test_repeated_normal_request_runs_filters_again(tmp_path, global_enabled):
    inputs, _, request, inspectors, *_ = make_flow(
        tmp_path,
        options=InputAnalysisOptions(user_consent_global_bypass_enabled=global_enabled),
    )
    for _ in range(2):
        outcome = asyncio.run(inputs.execute(request))
        assert (
            outcome.analysis_result.final_decision.action is InputInspectionAction.BLOCK
        )
    inspectors[0].inspect.assert_awaited_with(request)
    assert inspectors[0].inspect.await_count == 2
    for inspector in inspectors[1:]:
        inspector.inspect.assert_not_called()
