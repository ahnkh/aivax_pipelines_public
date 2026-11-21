"""기존 bulk 백업 조건·이동 실패 및 writer 간 경합을 검증한다."""

import os
from pathlib import Path

import pytest

from src.adapters.analysis_log_delivery import FluentBitAnalysisLogDeliveryAdapter
from src.bootstrap.env_loader import EnvironmentLoader


def old_file(root, name, content=b"record\n", mtime=0):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))
    return path


def test_archive_uses_mtime_boundary_and_filename_date_for_both_log_types(
    tmp_path, monkeypatch
):
    root = tmp_path / "bulk"
    backup = tmp_path / "backup"
    original = old_file(root, "aivax_log#20260901#1.json")
    consent = old_file(root, "aivax_log#20260901#1.json.consent", b"update\n")
    revision = old_file(root, "aivax_log#20260901#1.json.revision", b"revision\n")
    fresh = old_file(root, "aivax_log#20260902#2.json", mtime=1)
    state = old_file(root / ".consent-state", "state.json")
    invalid = old_file(root, "aivax_log#20260230#3.json")
    foreign = old_file(root, "other.json")
    outside = old_file(tmp_path, "outside.json")
    link = root / "aivax_log#20260901#4.json"
    link.symlink_to(outside)
    monkeypatch.setattr("time.time", lambda: 87400)
    adapter = FluentBitAnalysisLogDeliveryAdapter(root)
    assert adapter.archive_bulk_logs(str(backup)) == 3
    assert (backup / "20260901" / original.name).read_bytes() == b"record\n"
    assert (backup / "20260901" / consent.name).read_bytes() == b"update\n"
    assert (backup / "20260901" / revision.name).read_bytes() == b"revision\n"
    assert not original.exists() and not consent.exists() and not revision.exists()
    assert all(
        path.exists() for path in (fresh, state, invalid, foreign, link, outside)
    )
    assert adapter.archive_bulk_logs(str(backup)) == 0


def test_archive_does_not_overwrite_existing_backup(tmp_path):
    root = tmp_path / "bulk"
    backup = tmp_path / "backup"
    original = old_file(root, "aivax_log#20260901#1.json", b"new\n")
    existing = old_file(backup / "20260901", original.name, b"previous\n")
    assert FluentBitAnalysisLogDeliveryAdapter(root).archive_bulk_logs(str(backup)) == 0
    assert original.read_bytes() == b"new\n"
    assert existing.read_bytes() == b"previous\n"


def test_partial_copy_failure_keeps_source_and_other_files_are_archived(
    tmp_path, monkeypatch
):
    import shutil

    root = tmp_path / "bulk"
    backup = tmp_path / "backup"
    failed = old_file(root, "aivax_log#20260901#1.json")
    good = old_file(root, "aivax_log#20260901#2.json")
    copy = shutil.copyfileobj

    def failing_copy(input_file, output_file):
        if Path(input_file.name).name == failed.name:
            output_file.write(b"partial")
            raise OSError("disk full")
        return copy(input_file, output_file)

    monkeypatch.setattr(shutil, "copyfileobj", failing_copy)
    assert FluentBitAnalysisLogDeliveryAdapter(root).archive_bulk_logs(str(backup)) == 1
    assert failed.read_bytes() == b"record\n"
    assert not (backup / "20260901" / failed.name).exists()
    assert (backup / "20260901" / good.name).read_bytes() == b"record\n"
    assert not list(backup.rglob("*.tmp"))


def test_archive_skips_an_active_writer_lock(tmp_path):
    root = tmp_path / "bulk"
    backup = tmp_path / "backup"
    original = old_file(root, "aivax_log#20260901#1.json")
    writer = FluentBitAnalysisLogDeliveryAdapter(root)
    other_worker = FluentBitAnalysisLogDeliveryAdapter(root)
    with writer._bulk_log_lock(shared=True):
        assert other_worker.archive_bulk_logs(str(backup)) == 0
        assert original.exists()
    assert other_worker.archive_bulk_logs(str(backup)) == 1


def test_archive_rejects_same_directory(tmp_path):
    old_file(tmp_path, "aivax_log#20260901#1.json")
    with pytest.raises(ValueError, match="must differ"):
        FluentBitAnalysisLogDeliveryAdapter(tmp_path).archive_bulk_logs(str(tmp_path))


def test_archive_backup_path_is_loaded_from_environment():
    settings = EnvironmentLoader().build_settings(
        {"AIVAX_PIPELINE_LOG_BULK_BACKUP_DIR": "/data/backup/prompt_log_backup"}
    )
    assert settings.log_bulk_backup_dir == "/data/backup/prompt_log_backup"
