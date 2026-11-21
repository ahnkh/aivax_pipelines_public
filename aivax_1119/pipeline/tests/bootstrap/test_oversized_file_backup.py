"""환경 설정을 실제 입력 분석까지 전달해 용량 초과 파일의 보관을 제어한다."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import fitz
import pytest

from src.adapters.delivery_file import LocalFileDeliveryAdapter
from src.adapters.inspection_multimodal import LocalMultimodalInspectionAdapter
from src.application.services.inspection_multimodal import MultimodalInspectionService
from src.application.services.inspection_slm import SlmAnalysisResult
from src.application.services.notification_attachment import (
    AttachmentNotificationService,
)
from src.bootstrap.env_loader import EnvironmentLoader
from src.bootstrap.pipeline_bootstrap import PipelineBootstrap
from src.domain.config_aivax.models import AivaxConfig
from src.domain.inspection_input.models import (
    InputAnalysisRequest,
    InputAttachment,
    InputInspectionAction,
)
from src.domain.policy_multimodal.models import MultimodalPolicySnapshot


@pytest.mark.parametrize(
    "value, expected_backup", [(None, True), ("true", True), ("false", False)]
)
def test_environment_controls_backup_without_stopping_following_filters(
    tmp_path: Path, value: str | None, expected_backup: bool
) -> None:
    env = "AIVAX_PIPELINE_OVERSIZED_FILE_HANDLING=skip\n"
    if value is not None:
        env += f"AIVAX_PIPELINE_BACKUP_SKIPPED_OVERSIZED_FILES={value}\n"
    (tmp_path / ".env").write_text(env)
    _, settings = EnvironmentLoader(tmp_path, environ={}).bootstrap()
    bootstrap = PipelineBootstrap(settings, None, Mock())
    source = tmp_path / "upload"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "Oversized PDF")
        document.save(source)
    original = source.read_bytes()
    archive = tmp_path / "archive"
    outbox = Mock()
    merge = Mock()
    runtime = SimpleNamespace(
        ai_service_catalog=Mock(),
        aivax_config=SimpleNamespace(get_config=lambda: AivaxConfig()),
        regex_inspection=Mock(),
        file_delivery=LocalFileDeliveryAdapter(archive),
        attachment_notification_service=AttachmentNotificationService(
            outbox, "aivax_analysis_log", shared_backup_roots=(archive,)
        ),
        analysis_log_merge_service=merge,
        analysis_log_delivery=Mock(),
    )
    slm = SimpleNamespace(
        inspect=AsyncMock(return_value=SlmAnalysisResult(action="accept"))
    )
    policy = MultimodalPolicySnapshot(max_size_bytes=1, allowed_extensions=("pdf",))
    services = SimpleNamespace(
        multimodal=MultimodalInspectionService(
            LocalMultimodalInspectionAdapter(),
            SimpleNamespace(get_policy=lambda: policy),
            bootstrap._create_multimodal_inspection_options(),
        ),
        slm=slm,
    )
    use_case = bootstrap._create_input_analysis_use_case(runtime, services)

    outcome = asyncio.run(
        use_case.execute(
            InputAnalysisRequest(
                requested_filter_names=(
                    "input_filter",
                    "file_block_filter",
                    "slm_filter",
                ),
                prompt="Summarize the document.",
                message_id="oversized-backup-wiring-test",
                attachments=(InputAttachment(id=str(source), name="document.pdf"),),
            )
        )
    )

    assert outcome.analysis_result.final_decision.action is InputInspectionAction.ACCEPT
    slm.inspect.assert_awaited_once_with("Summarize the document.")
    merge.record_input.assert_called_once()
    document = merge.record_input.call_args.args[1]
    summary = next(
        item for item in document["filter_detect"] if item["filter"] == "file block"
    )["file_summary"][0]
    assert summary["file_info"]["inspection_status"] == "skipped"
    if expected_backup:
        assert Path(summary["file_backup_path"]).read_bytes() == original
        assert not source.exists()
        outbox.enqueue.assert_called_once()
    else:
        assert source.read_bytes() == original
        assert summary["file_backup_path"] == ""
        outbox.enqueue.assert_not_called()
