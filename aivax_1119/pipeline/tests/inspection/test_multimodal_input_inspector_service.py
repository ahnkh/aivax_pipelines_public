"""공용 멀티모달 서비스와 input-analysis 변환 계약."""

from __future__ import annotations

from tests.ai_service_catalog import make_ai_service_catalog
import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import fitz
import pytest

from src.adapters.delivery_file.local_file_delivery_adapter import (
    LocalFileDeliveryAdapter,
)
from src.adapters.inspection_multimodal import LocalMultimodalInspectionAdapter
from src.application.services.inspection_multimodal import MultimodalInspectionService
from src.application.services.notification_attachment import (
    AttachmentNotificationService,
)
from src.application.use_cases.input_analysis import (
    InputAnalysisLogBuilder,
    MultimodalInputInspector,
)
from src.application.use_cases.test_filter_rule import (
    FilterRuleTestAttachment,
    FilterRuleTestRequest,
    FilterRuleTestUseCase,
)
from src.controllers.test_filter_rule.mapper import TestFilterRuleMapper
from src.domain.analysis_log_delivery.services.detection_metadata import (
    detection_metadata,
)
from src.domain.delivery_file.models import FileDeliveryResult
from src.domain.inspection_input.models import (
    InputAnalysisOptions,
    InputAnalysisRequest,
    InputAnalysisResult,
    InputAnalysisUser,
    InputAttachment,
    InputInspectionAction,
)
from src.domain.inspection_input.services.final_decision_resolver import (
    InputFinalDecisionResolver,
)
from src.domain.inspection_multimodal.models import (
    DocumentEvidence,
    DocumentSource,
    FileMetadata,
    MultimodalFileInspectionResult,
    MultimodalFileLimitReason,
    MultimodalInspectionOptions,
    MultimodalInspectionResult,
    MultimodalInspectionTarget,
    MultimodalMatchedPolicy,
    MultimodalPolicyMatch,
    WatermarkCriteria,
)
from src.domain.policy_multimodal.models import (
    MultimodalPolicyRule,
    MultimodalPolicySnapshot,
)


class StubInspection:
    def __init__(self, result: MultimodalInspectionResult) -> None:
        self.result = result
        self.request = None

    async def inspect(self, request):
        self.request = request
        return self.result


class StubPolicy:
    def __init__(self, snapshot: MultimodalPolicySnapshot) -> None:
        self.snapshot = snapshot

    def get_policy(self) -> MultimodalPolicySnapshot:
        return self.snapshot


class StubDelivery:
    def __init__(self) -> None:
        self.calls = 0

    async def deliver(self, request) -> FileDeliveryResult:
        self.calls += 1
        return FileDeliveryResult(
            delivered=True,
            destination=f"/archive/{request.file_name}",
        )


def _options() -> MultimodalInspectionOptions:
    return MultimodalInspectionOptions(
        bypass=False,
        read_timeout_seconds=30,
        unsupported_file_handling="skip",
        oversized_file_handling="skip",
        content_chunk_size=20,
        detail_reason_directory="/tmp/detail",
        watermark_criteria=WatermarkCriteria(
            drm_enabled=True,
            drm_pattern="DRM",
            drm_pattern_flags=0,
            drm_header_limit=1024,
            ocr_enabled=True,
            ocr_patterns=("confidential",),
            ocr_pattern_flags=0,
            ocr_hit_threshold=1,
            ocr_pages=(0,),
        ),
    )


def _request(path: Path) -> InputAnalysisRequest:
    return InputAnalysisRequest(
        requested_filter_names=("file_block_filter",),
        prompt="",
        attachments=(
            InputAttachment(
                id=str(path),
                size=path.stat().st_size,
                name="document.pdf",
                mime_type="application/pdf",
            ),
        ),
    )


@pytest.mark.parametrize("backup_enabled", [True, False])
def test_maps_shared_analysis_result_to_input_contract(
    tmp_path: Path, backup_enabled: bool
) -> None:
    path = tmp_path / "upload-id"
    path.write_text("contains secret")
    policy = MultimodalMatchedPolicy(
        id="file-rule",
        name="Confidential document",
        action="block",
        target="file",
        category="security",
        scope="default",
        matched_text="secret",
        expression="secret",
    )
    inspection = StubInspection(
        MultimodalInspectionResult(
            action="block",
            selected_policy=policy,
            files=(
                MultimodalFileInspectionResult(
                    source=DocumentSource.from_path(path),
                    file_name="document.pdf",
                    metadata=FileMetadata(
                        "application/pdf", "pdf", path.stat().st_size
                    ),
                    matches=(
                        MultimodalPolicyMatch(
                            policy,
                            (DocumentEvidence(1, 2, 3, "contains secret", "secret"),),
                        ),
                    ),
                    file_hash="hash",
                    content_chunk="contains secret",
                    content_inspected=True,
                ),
            ),
        )
    )
    archive = StubDelivery()
    service = MultimodalInspectionService(
        inspection,
        StubPolicy(MultimodalPolicySnapshot()),
        _options(),
    )
    inspector = MultimodalInputInspector(
        service, archive, backup_skipped_oversized_files=backup_enabled
    )

    result = asyncio.run(inspector.inspect(_request(path)))
    summary = result.details["file_summary"][0]

    assert result.action is InputInspectionAction.BLOCK
    assert result.block_message.startswith("[AIVAX] 첨부파일 차단\n")
    assert "'Confidential document'" in result.block_message
    assert "요청이 차단되었습니다." in result.block_message
    assert result.policy and result.policy.id == "file-rule"
    assert summary["policy_name"] == "Confidential document"
    assert summary["regex_pattern"] == "secret"
    assert summary["file_info"]["hash"] == "hash"
    assert summary["file_backup_path"] == "/archive/document.pdf"
    assert summary["detail_reason"]["evidence"][0]["page_no"] == 2
    assert archive.calls == 1


@pytest.mark.parametrize("last_action", ["accept", "block"])
def test_multimodal_policy_matches_reach_test_result_and_log_once(
    tmp_path, last_action
):
    path = tmp_path / "multiple.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), "alpha beta")
        pdf.save(path)
    snapshot = MultimodalPolicySnapshot(
        rules=(
            MultimodalPolicyRule(
                id="alpha",
                name="Alpha",
                expression="alpha",
                action="accept",
                target="file",
                category="first",
            ),
            MultimodalPolicyRule(
                id="beta",
                name="Beta",
                expression="beta",
                action=last_action,
                target="file",
                category="second",
            ),
        )
    )
    options = replace(
        _options(),
        watermark_criteria=replace(
            _options().watermark_criteria, drm_enabled=False, ocr_enabled=False
        ),
    )
    service = MultimodalInspectionService(
        LocalMultimodalInspectionAdapter(pdf_evidence_engine="pymupdf"),
        StubPolicy(snapshot),
        options,
    )
    archive = StubDelivery()
    notifications = Mock()
    request = replace(_request(path), message_id="multi-policy")

    execution = asyncio.run(
        MultimodalInputInspector(service, archive, notifications).inspect(request)
    )

    assert execution.action.value == last_action
    assert [item["policy_id"] for item in execution.evidence] == ["alpha", "beta"]
    summaries = execution.details["file_summary"]
    assert len(summaries) == 1
    assert [item["policy_id"] for item in summaries[0]["matches"]] == ["alpha", "beta"]
    assert archive.calls == 1
    notifications.enqueue_archived_file.assert_called_once()

    builder = InputAnalysisLogBuilder(make_ai_service_catalog())
    document = builder.build(
        request,
        InputAnalysisResult(
            executions=(execution,),
            final_decision=InputFinalDecisionResolver.resolve(
                (execution,),
                InputAnalysisOptions(),
                request.prompt,
            ),
        ),
    )
    detection = document["filter_detect"][0]
    assert [(item["id"], item["action"]) for item in detection["evidence"]] == [
        ("alpha", "accept"),
        ("beta", last_action),
    ]
    assert detection_metadata(document)["detected_filter_categories"] == [
        "file block|first",
        "file block|second",
    ]
    restored = builder.restore_executions(document)[0]
    assert restored.evidence == tuple(detection["evidence"])
    assert restored.details["file_summary"] == summaries

    tested = asyncio.run(
        FilterRuleTestUseCase(Mock(), service, Mock()).execute(
            FilterRuleTestRequest(
                prompt="",
                attachments=(FilterRuleTestAttachment(str(path), "document.pdf"),),
                multimodal_policy=snapshot,
            ),
        )
    )
    response = TestFilterRuleMapper.to_success_response(tested)
    file_result = response["multimodal_result"]["files"][0]
    assert file_result["action"] == last_action
    assert [item["policy_id"] for item in file_result["matches"]] == ["alpha", "beta"]
    assert [item["matched_text"] for item in file_result["matches"][1]["evidence"]] == [
        "beta"
    ]


def test_archived_file_event_is_enqueued_immediately_after_backup_delivery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upload-id"
    path.write_text("content")
    file_result = MultimodalFileInspectionResult(
        source=DocumentSource.from_path(path),
        file_name="document.pdf",
        metadata=FileMetadata("application/pdf", "pdf", path.stat().st_size),
    )
    calls: list[str] = []

    class OrderedDelivery:
        async def deliver(self, request) -> FileDeliveryResult:
            calls.append("delivery")
            return FileDeliveryResult(
                delivered=True,
                destination=f"/archive/{request.file_name}",
            )

    archive = OrderedDelivery()
    attachment_notifications = Mock()
    attachment_notifications.enqueue_archived_file.side_effect = (
        lambda **_kwargs: calls.append("event") or True
    )
    service = MultimodalInspectionService(
        StubInspection(MultimodalInspectionResult(files=(file_result,))),
        StubPolicy(MultimodalPolicySnapshot()),
        _options(),
    )
    request = _request(path)
    request = InputAnalysisRequest(
        requested_filter_names=request.requested_filter_names,
        prompt=request.prompt,
        attachments=request.attachments,
        message_id="message-1",
        user=InputAnalysisUser(client_host="123.123.123.123"),
        event_time="2026-10-06T10:00:00Z",
    )

    asyncio.run(
        MultimodalInputInspector(
            service,
            archive,
            attachment_notifications,
        ).inspect(request),
    )

    attachment_notifications.enqueue_archived_file.assert_called_once_with(
        message_id="message-1",
        source_ip="123.123.123.123",
        occurred_at="2026-10-06T10:00:00Z",
        file_name="document.pdf",
        file_backup_path="/archive/document.pdf",
    )
    assert calls == ["delivery", "event"]


def test_failed_backup_does_not_enqueue_an_attachment_event(tmp_path: Path) -> None:
    path = tmp_path / "upload-id"
    path.write_text("content")
    file_result = MultimodalFileInspectionResult(
        source=DocumentSource.from_path(path),
        file_name="document.pdf",
        metadata=FileMetadata("application/pdf", "pdf", path.stat().st_size),
    )

    class FailedDelivery:
        async def deliver(self, _request) -> FileDeliveryResult:
            return FileDeliveryResult()

    attachment_notifications = Mock()
    service = MultimodalInspectionService(
        StubInspection(MultimodalInspectionResult(files=(file_result,))),
        StubPolicy(MultimodalPolicySnapshot()),
        _options(),
    )

    asyncio.run(
        MultimodalInputInspector(
            service,
            FailedDelivery(),
            attachment_notifications,
        ).inspect(_request(path)),
    )

    attachment_notifications.enqueue_archived_file.assert_not_called()


def test_skipped_file_is_not_archived(tmp_path: Path) -> None:
    path = tmp_path / "upload-id"
    path.write_text("content")
    file_result = MultimodalFileInspectionResult(
        source=DocumentSource.from_path(path),
        file_name="document.exe",
        metadata=FileMetadata("application/octet-stream", "exe", path.stat().st_size),
        inspection_status="skipped",
        skip_reason="파일 확장자 제한 (exe)",
        limit_reason=MultimodalFileLimitReason.UNSUPPORTED_EXTENSION,
    )
    archive = StubDelivery()
    service = MultimodalInspectionService(
        StubInspection(MultimodalInspectionResult(files=(file_result,))),
        StubPolicy(MultimodalPolicySnapshot()),
        _options(),
    )

    result = asyncio.run(
        MultimodalInputInspector(service, archive).inspect(_request(path))
    )
    summary = result.details["file_summary"][0]

    assert result.action is InputInspectionAction.BLANK
    assert result.block_message == ""
    assert summary["file_info"]["inspection_status"] == "skipped"
    assert archive.calls == 0


@pytest.mark.parametrize("limit", ["size", "extension"])
@pytest.mark.parametrize("handling", ["skip", "block"])
@pytest.mark.parametrize("backup_enabled", [True, False])
@pytest.mark.parametrize("backup_fails", [True, False])
def test_file_limit_backup_and_management_notification(
    tmp_path: Path,
    limit: str,
    handling: str,
    backup_enabled: bool,
    backup_fails: bool,
) -> None:
    source = tmp_path / "upload-id"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "File backup regression")
        document.save(source)
    original = source.read_bytes()
    archive = tmp_path / "archive"
    if backup_fails:
        # 저장 경로에 일반 파일을 두어 실제 백업의 디렉터리 생성을 실패시킨다.
        archive.write_text("not a directory")
    adapter = LocalMultimodalInspectionAdapter()
    adapter._watermark_policy = Mock(side_effect=AssertionError("watermark ran"))
    adapter._inspect_contents = Mock(side_effect=AssertionError("content ran"))
    service = MultimodalInspectionService(
        adapter,
        StubPolicy(
            MultimodalPolicySnapshot(
                allowed_extensions=("pdf",) if limit == "size" else ("txt",),
                max_size_bytes=len(original) - 1,
            )
        ),
        replace(
            _options(),
            oversized_file_handling=handling,
            unsupported_file_handling=handling,
        ),
    )
    outbox = Mock()
    notifications = AttachmentNotificationService(
        outbox,
        "aivax_analysis_log",
        shared_backup_roots=(archive,),
    )
    request = replace(
        _request(source),
        message_id="oversized-backup-test",
        event_time="2026-10-27T00:00:00Z",
        user=InputAnalysisUser(client_host="10.0.55.150"),
    )

    result = asyncio.run(
        MultimodalInputInspector(
            service,
            LocalFileDeliveryAdapter(archive),
            notifications,
            backup_skipped_oversized_files=backup_enabled,
        ).inspect(request)
    )

    summary = result.details["file_summary"][0]
    assert result.action is (
        InputInspectionAction.BLOCK
        if handling == "block"
        else InputInspectionAction.BLANK
    )
    assert summary["file_info"]["size"] == len(original)
    assert "hash" not in summary["file_info"]
    assert "chunk" not in summary["file_info"]
    adapter._watermark_policy.assert_not_called()
    adapter._inspect_contents.assert_not_called()
    if handling == "skip":
        assert result.block_message == ""
        assert summary["file_info"]["inspection_status"] == "skipped"
        assert summary["file_info"]["skip_reason"].startswith(
            "파일 사이즈 제한" if limit == "size" else "파일 확장자 제한"
        )
    else:
        assert summary["policy_name"].startswith(
            "파일 사이즈 제한" if limit == "size" else "파일 확장자 제한"
        )
    should_deliver = handling == "block" or (backup_enabled and limit == "size")
    if should_deliver and not backup_fails:
        destination = Path(summary["file_backup_path"])
        assert destination.is_relative_to(archive)
        assert destination.read_bytes() == original
        assert not source.exists()
        outbox.enqueue.assert_called_once()
        event = outbox.enqueue.call_args.args[0]
        assert event.message_id == request.message_id
        assert event.source_ip == "10.0.55.150"
        assert event.file_name == "document.pdf"
        assert event.file_backup_path == str(destination)
        assert event.file_size_bytes == len(original)
        assert event.detected_mime_type == "application/pdf"
    else:
        assert source.read_bytes() == original
        assert summary["file_backup_path"] == ""
        outbox.enqueue.assert_not_called()


def test_service_uses_explicit_test_policy_without_replacing_live_policy() -> None:
    live = MultimodalPolicySnapshot(max_size_bytes=10)
    draft = MultimodalPolicySnapshot(max_size_bytes=20)
    inspection = StubInspection(MultimodalInspectionResult())
    service = MultimodalInspectionService(inspection, StubPolicy(live), _options())

    target = MultimodalInspectionTarget(
        DocumentSource.from_path(Path("upload.png")), "upload.png"
    )
    asyncio.run(service.inspect((target,), policy_override=draft))

    assert inspection.request.policy is draft


def test_jpg_name_is_logged_while_jpeg_detection_stays(tmp_path: Path) -> None:
    path = tmp_path / "upload"
    path.write_bytes(b"jpeg")
    result = MultimodalFileInspectionResult(
        source=DocumentSource.from_path(path),
        file_name="계좌번호 - 신한은행.jpg",
        metadata=FileMetadata("image/jpeg", "jpeg", 4),
    )
    inspector = MultimodalInputInspector.__new__(MultimodalInputInspector)

    summary = inspector._to_summary(result, "")

    assert summary["file_info"]["file_ext"] == "jpg"
    assert summary["file_info"]["mime_type"] == "image/jpeg"
    renamed = replace(result, file_name="photo.png")
    assert inspector._to_summary(renamed, "")["file_info"]["file_ext"] == "jpeg"
