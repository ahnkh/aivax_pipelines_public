"""동의 상태의 30분 만료 및 worker 간 정리 경합 검증."""

import hashlib
import os
from unittest.mock import Mock

import pytest

from src.adapters.analysis_log_delivery import FluentBitAnalysisLogDeliveryAdapter
from src.domain.analysis_log_delivery.errors.consent_log_not_found_error import (
    ConsentLogNotFoundError,
)
from src.domain.analysis_log_delivery.models import MergedAnalysisLogDocument
from src.schedulers.cleanup_cron import CleanupCron


def blocked_state(tmp_path, message_id="mid"):
    adapter = FluentBitAnalysisLogDeliveryAdapter(tmp_path)
    assert adapter.deliver(
        MergedAnalysisLogDocument({"message_id": message_id, "mode": "block"})
    )
    key = hashlib.sha256(message_id.encode()).hexdigest()
    return adapter, tmp_path / ".consent-state" / f"{key}.json"


def test_state_survives_restart_until_thirty_minutes(tmp_path, monkeypatch):
    adapter, state = blocked_state(tmp_path)
    created = state.stat().st_mtime
    monkeypatch.setattr("time.time", lambda: created + 1799)
    restarted = FluentBitAnalysisLogDeliveryAdapter(tmp_path)
    restarted.write_consent_update("mid", "answer", 0)
    assert state.stat().st_mtime == created
    # 같은 BLOCK 로그를 재전달해도 보관기간을 연장하지 않는다.
    assert adapter.deliver(
        MergedAnalysisLogDocument({"message_id": "mid", "mode": "block"})
    )
    assert state.stat().st_mtime == created


@pytest.mark.parametrize("operation", ["require", "update"])
def test_expired_state_is_rejected_before_scheduler_runs(
    tmp_path, monkeypatch, operation
):
    adapter, state = blocked_state(tmp_path)
    state_created = state.stat().st_mtime
    monkeypatch.setattr("time.time", lambda: state_created + 1800)
    with pytest.raises(ConsentLogNotFoundError, match="expired"):
        if operation == "require":
            adapter.require_block_log("mid")
        else:
            adapter.write_consent_update("mid", "answer", 0)
    assert not state.exists()
    assert not list(tmp_path.glob("*.json.consent"))
    assert len(list(tmp_path.glob("aivax_log#*.json"))) == 1


def test_sweep_removes_only_expired_state_files(tmp_path, monkeypatch):
    adapter, expired = blocked_state(tmp_path, "expired")
    _, fresh = blocked_state(tmp_path, "fresh")
    now = fresh.stat().st_mtime
    os.utime(expired, (now - 1800, now - 1800))
    monkeypatch.setattr("time.time", lambda: now)
    original = next(tmp_path.glob("aivax_log#*.json"))
    consent = tmp_path / "aivax_log#20000101#1.json.consent"
    consent.write_text("unchanged")
    foreign = expired.parent / "foreign.json"
    foreign.write_text("unchanged")
    os.utime(foreign, (0, 0))
    assert adapter.expire_consent_states() == 1
    assert not expired.exists()
    assert all(path.exists() for path in (fresh, original, consent, foreign))
    assert adapter.expire_consent_states() == 0


def test_sweep_skips_directory_owned_by_another_worker(tmp_path):
    writer, expired = blocked_state(tmp_path)
    os.utime(expired, (0, 0))
    other_worker = FluentBitAnalysisLogDeliveryAdapter(tmp_path)
    with writer._consent_state_lock():
        assert other_worker.expire_consent_states() == 0
        assert expired.exists()
    assert other_worker.expire_consent_states() == 1


def test_scheduler_sweeps_on_start_and_stops_without_waiting_a_minute():
    import threading

    called = threading.Event()
    use_case = Mock(execute=Mock(side_effect=lambda **_: called.set()))
    cron = CleanupCron(use_case)
    cron.start()
    thread = cron._thread
    try:
        assert called.wait(2)
        cron.start()
        assert cron._thread is thread
    finally:
        assert cron.shutdown() == 1
    assert not thread.is_alive()
