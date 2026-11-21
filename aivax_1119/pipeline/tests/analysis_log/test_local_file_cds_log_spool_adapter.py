from pathlib import Path
import json

from src.adapters.analysis_log_delivery import (
    LocalFileCdsAnalysisLogQueueAdapter,
)
from src.domain.analysis_log_delivery.models import MergedAnalysisLogDocument


def _traffic_log() -> MergedAnalysisLogDocument:
    return MergedAnalysisLogDocument(
        {
            "@timestamp": "2026-09-23T00:27:46Z",
            "message_id": "message-1",
            "client_host": "127.0.0.1",
            "mode": "block",
        }
    )


def test_enqueue_is_idempotent_and_readable(tmp_path: Path) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)

    first = spool.enqueue("message-1:request", _traffic_log())
    second = spool.enqueue("message-1:request", _traffic_log())

    assert first == second
    with spool.claim_batch(1000) as batch:
        assert len(batch.entries) == 1
        entry = batch.entries[0]
    assert entry.event_key == "message-1:request"
    assert entry.analysis_log.to_dict()["result"] == "B"

    assert spool.acknowledge((entry,)) == 1
    with spool.claim_batch(1000) as batch:
        assert batch.entries == ()


def test_corrupted_file_does_not_prevent_other_file_reads(
    tmp_path: Path,
) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)

    valid_reference = spool.enqueue(
        "valid-message:request",
        _traffic_log(),
    )
    invalid_reference = "cds_log#broken.json"
    (tmp_path / invalid_reference).write_text(
        "{broken",
        encoding="utf-8",
    )

    with spool.claim_batch(1000) as batch:
        assert batch.unreadable_count == 1
        assert len(batch.entries) == 1
        assert batch.entries[0].event_key == "valid-message:request"
    assert (tmp_path / "failed" / invalid_reference).exists()


def test_claim_batch_is_available_as_context_manager(
    tmp_path: Path,
) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)

    with spool.claim_batch(1000) as batch:
        assert batch.entries == ()


def test_reschedule_persists_retry_count(tmp_path: Path) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)
    spool.enqueue("message-1:request", _traffic_log())

    with spool.claim_pending() as batch:
        assert batch.entries[0].retry_count == 0
        assert spool.reschedule(batch.entries) == 1

    with spool.claim_pending() as batch:
        assert batch.entries[0].retry_count == 1


def test_reads_legacy_prepared_payload(tmp_path: Path) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)
    reference = "cds_log#legacy.json"
    (tmp_path / reference).write_text(
        json.dumps(
            {
                "event_key": "legacy:request",
                "payload": {
                    "occurredAt": "2026-09-23T09:27:46",
                    "protocol": "HTTPS",
                    "msgType": 0,
                    "detect": 4,
                    "inspectResult": "탐지: 이메일 처리: 차단",
                    "srcIp": "127.0.0.1",
                    "srcAccount": "",
                    "srcPort": 0,
                    "dst": "example.com",
                    "dstPort": 443,
                    "result": "B",
                },
            }
        ),
        encoding="utf-8",
    )

    with spool.claim_batch(1000) as batch:
        entry = batch.entries[0]

    assert entry.event_key == "legacy:request"
    assert entry.analysis_log.to_dict()["protocol"] == "HTTPS"


def test_invalid_log_is_rejected_before_spool_creation(tmp_path: Path) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)

    try:
        spool.enqueue(
            "invalid:request",
            MergedAnalysisLogDocument(
                {
                    "message_id": "invalid",
                    "mode": "unknown",
                }
            ),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("invalid CDS log must be rejected")

    assert tuple(tmp_path.glob("cds_log#*.json")) == ()


def test_corrupted_head_does_not_starve_valid_entry(tmp_path: Path) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)
    (tmp_path / "cds_log#000-broken.json").write_text(
        "{broken",
        encoding="utf-8",
    )
    spool.enqueue("valid:request", _traffic_log())

    with spool.claim_batch(1) as batch:
        assert batch.unreadable_count == 1
        assert len(batch.entries) == 1
        assert batch.entries[0].event_key == "valid:request"
