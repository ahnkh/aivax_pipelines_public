from pathlib import Path
from unittest.mock import Mock

from src.adapters.analysis_log_delivery import (
    LocalFileCdsAnalysisLogQueueAdapter,
)
from src.application.use_cases.send_pending_cds_logs import (
    SendPendingCdsLogsUseCase,
)
from src.domain.analysis_log_delivery.errors import (
    RetryableAnalysisLogDeliveryError,
)
from src.domain.analysis_log_delivery.models import (
    AnalysisLogBatchDeliveryResult,
    AnalysisLogDeliveryRow,
    MergedAnalysisLogDocument,
)
from src.domain.analysis_log_delivery.ports import AnalysisLogBatchDeliveryPort
from src.domain.notification_audit.ports import AuditNotificationPort


def _traffic_log() -> MergedAnalysisLogDocument:
    return MergedAnalysisLogDocument(
        {
            "@timestamp": "2026-09-23T00:27:46Z",
            "message_id": "message-1",
            "client_host": "127.0.0.1",
            "mode": "block",
        }
    )


def test_sends_valid_files_and_keeps_corrupted_file(
    tmp_path: Path,
) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)
    first = spool.enqueue("message-1:request", _traffic_log())
    second = spool.enqueue("message-2:request", _traffic_log())

    corrupted = "cds_log#broken.json"
    (tmp_path / corrupted).write_text(
        "{broken",
        encoding="utf-8",
    )

    delivery = Mock(spec=AnalysisLogBatchDeliveryPort)
    delivery.deliver.return_value = AnalysisLogBatchDeliveryResult(
        rows=(
            AnalysisLogDeliveryRow(0, True),
            AnalysisLogDeliveryRow(1, True),
        )
    )
    audit = Mock(spec=AuditNotificationPort)
    logger = Mock()

    use_case = SendPendingCdsLogsUseCase(
        spool,
        delivery,
        audit=audit,
        logger=logger,
    )

    assert use_case.execute() == 2
    assert tuple(tmp_path.glob("cds_log#*.json")) == ()
    assert (tmp_path / "failed" / corrupted).exists()
    assert not (tmp_path / first).exists()
    assert not (tmp_path / second).exists()
    audit.notify.assert_called_once()


def test_invalid_delivery_result_quarantines_all_files(tmp_path: Path) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)
    first = spool.enqueue("message-1:request", _traffic_log())
    second = spool.enqueue("message-2:request", _traffic_log())
    delivery = Mock(spec=AnalysisLogBatchDeliveryPort)
    delivery.deliver.return_value = AnalysisLogBatchDeliveryResult(
        rows=(
            AnalysisLogDeliveryRow(0, True),
            AnalysisLogDeliveryRow(0, True),
        )
    )

    use_case = SendPendingCdsLogsUseCase(
        spool,
        delivery,
        logger=Mock(),
    )

    assert use_case.execute() == 0
    assert (tmp_path / "failed" / first).exists()
    assert (tmp_path / "failed" / second).exists()


def test_partial_acknowledge_failure_reports_actual_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)
    first = spool.enqueue("message-1:request", _traffic_log())
    second = spool.enqueue("message-2:request", _traffic_log())
    delivery = Mock(spec=AnalysisLogBatchDeliveryPort)
    delivery.deliver.return_value = AnalysisLogBatchDeliveryResult(
        rows=(
            AnalysisLogDeliveryRow(0, True),
            AnalysisLogDeliveryRow(1, True),
        )
    )
    audit = Mock(spec=AuditNotificationPort)
    original_unlink = Path.unlink

    def fail_second(path: Path, *args, **kwargs):
        if path.name == second:
            raise PermissionError("test failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second)
    use_case = SendPendingCdsLogsUseCase(
        spool,
        delivery,
        audit=audit,
        logger=Mock(),
    )

    assert use_case.execute() == 1
    assert not (tmp_path / first).exists()
    assert (tmp_path / second).exists()
    event = audit.notify.call_args.args[0]
    assert "실패 1건" in event.message


def test_deletes_only_successful_rows(tmp_path: Path) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)
    first = spool.enqueue("message-1:request", _traffic_log())
    second = spool.enqueue("message-2:request", _traffic_log())

    delivery = Mock(spec=AnalysisLogBatchDeliveryPort)
    delivery.deliver.return_value = AnalysisLogBatchDeliveryResult(
        rows=(
            AnalysisLogDeliveryRow(0, True),
            AnalysisLogDeliveryRow(1, False),
        )
    )
    audit = Mock(spec=AuditNotificationPort)

    use_case = SendPendingCdsLogsUseCase(
        spool,
        delivery,
        audit=audit,
        logger=Mock(),
    )

    assert use_case.execute() == 1
    assert not (tmp_path / first).exists()
    assert (tmp_path / "failed" / second).exists()
    audit.notify.assert_called_once()


def test_sends_all_pending_files_in_limited_batches(tmp_path: Path) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)
    for index in range(5):
        spool.enqueue(f"message-{index}:request", _traffic_log())

    delivery = Mock(spec=AnalysisLogBatchDeliveryPort)
    delivery.deliver.side_effect = lambda logs: AnalysisLogBatchDeliveryResult(
        rows=tuple(AnalysisLogDeliveryRow(index, True) for index in range(len(logs))),
    )
    use_case = SendPendingCdsLogsUseCase(
        spool,
        delivery,
        batch_size=2,
        logger=Mock(),
    )

    assert use_case.execute() == 5
    assert [len(call.args[0]) for call in delivery.deliver.call_args_list] == [2, 2, 1]


def test_retries_500_three_times_then_quarantines(tmp_path: Path) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)
    receipt = spool.enqueue("message-1:request", _traffic_log())
    delivery = Mock(spec=AnalysisLogBatchDeliveryPort)
    delivery.deliver.side_effect = RetryableAnalysisLogDeliveryError(
        "status=500",
    )
    use_case = SendPendingCdsLogsUseCase(
        spool,
        delivery,
        logger=Mock(),
    )

    for expected_retry_count in (1, 2, 3):
        assert use_case.execute() == 0
        with spool.claim_batch(1) as batch:
            assert batch.entries[0].retry_count == expected_retry_count

    assert use_case.execute() == 0
    assert not (tmp_path / receipt).exists()
    assert (tmp_path / "failed" / receipt).exists()
    assert delivery.deliver.call_count == 4


def test_non_retryable_error_is_quarantined_immediately(
    tmp_path: Path,
) -> None:
    spool = LocalFileCdsAnalysisLogQueueAdapter(tmp_path)
    receipt = spool.enqueue("message-1:request", _traffic_log())
    delivery = Mock(spec=AnalysisLogBatchDeliveryPort)
    delivery.deliver.side_effect = RuntimeError("status=400")
    use_case = SendPendingCdsLogsUseCase(
        spool,
        delivery,
        logger=Mock(),
    )

    assert use_case.execute() == 0
    assert (tmp_path / "failed" / receipt).exists()
    assert delivery.deliver.call_count == 1
