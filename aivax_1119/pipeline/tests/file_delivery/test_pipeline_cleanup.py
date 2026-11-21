"""하나의 cron에서 실행하는 임시파일 정리와 활성 검사 보호."""

import os
import re
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from src.adapters.inspection_multimodal.local_multimodal_inspection_adapter import (
    LocalMultimodalInspectionAdapter,
)
from src.application.use_cases.cleanup_pipeline_files import CleanupPipelineFilesUseCase
from src.domain.inspection_multimodal.models import (
    DocumentEvidenceResult,
    DocumentSource,
    FileMetadata,
)
from src.schedulers.cleanup_cron import CleanupCron


def test_cleanup_runs_both_jobs_and_isolates_failure():
    writer = Mock(expire_consent_states=Mock(side_effect=OSError("unavailable")))
    inspection = Mock(expire_temporary_evidence=Mock(return_value=2))
    log_delivery = Mock(archive_bulk_logs=Mock(return_value=3))
    use_case = CleanupPipelineFilesUseCase(
        writer,
        inspection,
        "/tmp/evidence",
        log_delivery=log_delivery,
        log_backup_directory="/tmp/backup",
    )
    assert use_case.execute() == {"consent_states": 0, "evidence_entries": 2}
    inspection.expire_temporary_evidence.assert_called_once_with("/tmp/evidence")
    writer.expire_consent_states.side_effect = None
    writer.expire_consent_states.return_value = 1
    inspection.expire_temporary_evidence.side_effect = OSError("unavailable")
    assert use_case.execute() == {"consent_states": 1, "evidence_entries": 0}
    log_delivery.archive_bulk_logs.assert_not_called()
    assert use_case.execute(archive_bulk=True) == {
        "consent_states": 1,
        "evidence_entries": 0,
        "bulk_logs": 3,
    }
    log_delivery.archive_bulk_logs.assert_called_once_with("/tmp/backup")


def test_bulk_archive_runs_hourly_at_five_minutes_and_catches_delayed_ticks():
    cron = CleanupCron(Mock())
    tz = ZoneInfo("Asia/Seoul")
    assert not cron._archive_due(datetime(2026, 10, 6, 20, 4, tzinfo=tz))
    assert cron._archive_due(datetime(2026, 10, 6, 20, 5, tzinfo=tz))
    assert not cron._archive_due(datetime(2026, 10, 6, 20, 5, 50, tzinfo=tz))
    assert not cron._archive_due(datetime(2026, 10, 6, 21, 4, tzinfo=tz))
    assert cron._archive_due(datetime(2026, 10, 6, 21, 6, tzinfo=tz))
    assert cron._archive_due(datetime(2026, 10, 6, 23, 20, tzinfo=tz))
    assert not cron._archive_due(datetime(2026, 10, 6, 23, 21, tzinfo=tz))


def test_bulk_archive_waits_for_next_five_minutes_after_startup():
    cron = CleanupCron(Mock())
    tz = ZoneInfo("Asia/Seoul")
    assert not cron._archive_due(datetime(2026, 10, 6, 23, 30, tzinfo=tz))
    assert not cron._archive_due(datetime(2026, 10, 7, 0, 4, tzinfo=tz))
    assert cron._archive_due(datetime(2026, 10, 7, 0, 5, tzinfo=tz))


def test_cleanup_wakes_at_hourly_archive_time(monkeypatch):
    clock = Mock()
    clock.now.return_value = datetime(
        2026, 10, 6, 20, 4, 45, tzinfo=ZoneInfo("Asia/Seoul")
    )
    monkeypatch.setattr("src.schedulers.cleanup_cron.datetime", clock)
    use_case = Mock()
    cron = CleanupCron(use_case)
    cron._stop = Mock(
        is_set=Mock(side_effect=[False, True]), wait=Mock(return_value=False)
    )
    cron._run()
    use_case.execute.assert_called_once_with(archive_bulk=False)
    cron._stop.wait.assert_called_once_with(15)


def test_cleanup_deletes_old_evidence_and_preserves_other_files(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    root.mkdir()
    expired = root / "evidence-old"
    expired.mkdir()
    (expired / "converted.pdf").write_bytes(b"pdf")
    fresh = root / "evidence-fresh"
    fresh.mkdir()
    legacy_pdf = root / "legacy.pdf"
    legacy_pdf.write_bytes(b"pdf")
    foreign = root / "keep.txt"
    foreign.write_text("keep")
    foreign_directory = root / "other-directory"
    foreign_directory.mkdir()
    target = tmp_path / "outside.pdf"
    target.write_bytes(b"pdf")
    link = root / "linked.pdf"
    link.symlink_to(target)
    for path in (expired, legacy_pdf, foreign, foreign_directory, target):
        os.utime(path, (0, 0))
    monkeypatch.setattr("time.time", lambda: 1800)
    adapter = LocalMultimodalInspectionAdapter()
    assert adapter.expire_temporary_evidence(str(root)) == 2
    assert not expired.exists() and not legacy_pdf.exists()
    assert all(
        path.exists() for path in (fresh, foreign, foreign_directory, link, target)
    )
    assert adapter.expire_temporary_evidence(str(root)) == 0


def test_active_evidence_is_protected_and_requests_use_separate_directories(
    tmp_path, monkeypatch
):
    source = tmp_path / "document.docx"
    source.write_bytes(b"source")
    root = tmp_path / "evidence"
    adapter = LocalMultimodalInspectionAdapter()
    other_worker = LocalMultimodalInspectionAdapter()
    directories = []

    def convert(path, directory, _timeout, extension=""):
        directory = Path(directory)
        directories.append(directory)
        pdf = directory / f"{path.stem}.pdf"
        pdf.write_bytes(b"converted")
        os.utime(directory, (0, 0))
        # 만료 기준을 넘어도 검사 중인 파일은 삭제하지 않는다.
        assert other_worker.expire_temporary_evidence(str(root)) == 0
        assert pdf.exists()
        return pdf

    monkeypatch.setattr(adapter._text_extractor, "convert_to_pdf", convert)
    expected = DocumentEvidenceResult()
    monkeypatch.setattr(adapter, "_collect_pdf_evidence", lambda *_: expected)
    for _ in range(2):
        assert (
            adapter._collect_evidence(
                DocumentSource.from_path(str(source)),
                FileMetadata("application/msword", "doc", 6),
                re.compile("secret"),
                str(root),
                30,
            )
            == expected
        )
    assert directories[0] != directories[1]
    # 삭제 시점은 처리 완료 후부터 계산한다.
    assert adapter.expire_temporary_evidence(str(root)) == 0
    for directory in directories:
        os.utime(directory, (0, 0))
    assert adapter.expire_temporary_evidence(str(root)) == 2
    assert source.read_bytes() == b"source"


def test_cleanup_of_missing_directory_does_not_create_it(tmp_path):
    root = tmp_path / "missing"
    assert LocalMultimodalInspectionAdapter().expire_temporary_evidence(str(root)) == 0
    assert not root.exists()


def test_cleanup_shutdown_timeout_warns_and_allows_process_shutdown():
    logger = Mock()
    cron = CleanupCron(Mock(), logger=logger)
    cron._thread = Mock(is_alive=Mock(return_value=True))
    assert cron.shutdown() == 1
    logger.warning.assert_called_once()
    logger.error.assert_not_called()
