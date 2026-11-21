import orjson
import pytest

from src.adapters.analysis_log_delivery import FluentBitAnalysisLogDeliveryAdapter
from src.bootstrap.env_loader import PipelineSettings
from src.domain.analysis_log_delivery.models import MergedAnalysisLogDocument


def test_deliver_writes_merged_analysis_log_as_ndjson_immediately(tmp_path) -> None:
    settings = PipelineSettings(log_bulk_dir=str(tmp_path))
    adapter = FluentBitAnalysisLogDeliveryAdapter(settings.log_bulk_dir)

    delivered = adapter.deliver(MergedAnalysisLogDocument({"message_id": "message-1"}))

    assert delivered is True
    files = list(tmp_path.glob("aivax_log#*.json"))
    assert len(files) == 1
    assert orjson.loads(files[0].read_bytes()) == {"message_id": "message-1"}


def test_write_output_revision_keeps_only_the_response_fields(tmp_path) -> None:
    adapter = FluentBitAnalysisLogDeliveryAdapter(str(tmp_path))

    assert adapter.write_output_revision(
        {
            "message_id": "message-1",
            "@timestamp": "2026-10-19T01:31:16.000000Z",
            "output_text": "완성된 답변",
            "text_truncated_bytes": 7,
            "prompt": "질문",
        }
    )

    files = list(tmp_path.glob("*.json.revision"))
    assert len(files) == 1
    assert orjson.loads(files[0].read_bytes()) == {
        "@timestamp": "2026-10-19T01:31:16.000000Z",
        "message_id": "message-1",
        "output_text": "완성된 답변",
        "text_truncated_bytes": 7,
        "merge_state": "",
    }


def test_deliver_reports_file_write_failure(tmp_path) -> None:
    blocked_path = tmp_path / "not-a-directory"
    blocked_path.write_text("file")
    settings = PipelineSettings(log_bulk_dir=str(blocked_path))
    adapter = FluentBitAnalysisLogDeliveryAdapter(settings.log_bulk_dir)

    delivered = adapter.deliver(MergedAnalysisLogDocument({"message_id": "message-1"}))

    assert delivered is False


@pytest.mark.parametrize("legacy_mode", ["pass", "fail", "failed"])
def test_deliver_normalizes_pending_legacy_modes_before_opensearch(
    tmp_path, legacy_mode
) -> None:
    document = {
        "message_id": "failed-message",
        "mode": legacy_mode,
        "filter_detect": [
            {
                "filter": "slm",
                "mode": legacy_mode,
                "error": {"code": "UPSTREAM_UNAVAILABLE"},
            },
            {"filter": "regex", "mode": "accept"},
        ],
    }
    analysis_log = MergedAnalysisLogDocument(document)

    assert FluentBitAnalysisLogDeliveryAdapter(tmp_path).deliver(analysis_log)

    files = list(tmp_path.glob("aivax_log#*.json"))
    assert len(files) == 1
    stored = orjson.loads(files[0].read_bytes())
    assert stored["mode"] == ""
    assert stored["filter_detect"][0]["mode"] == ""
    assert stored["filter_detect"][0]["error"] == {"code": "UPSTREAM_UNAVAILABLE"}
    assert stored["filter_detect"][1]["mode"] == "accept"
    assert analysis_log.to_dict() == document


def test_deliver_normalizes_pending_pass_attachments_before_opensearch(tmp_path):
    actions = ["pass", "", "accept", "masking", "block"]
    document = {
        "message_id": "generated-message",
        "mode": "",
        "filter_detect": [
            {
                "filter": "file block",
                "mode": "block",
                "file_summary": [{"action": action} for action in actions],
            }
        ],
    }
    analysis_log = MergedAnalysisLogDocument(document)

    assert FluentBitAnalysisLogDeliveryAdapter(tmp_path).deliver(analysis_log)

    stored = orjson.loads(next(tmp_path.glob("aivax_log#*.json")).read_bytes())
    detection = stored["filter_detect"][0]
    assert stored["mode"] == ""
    assert detection["mode"] == "block"
    assert [item["action"] for item in detection["file_summary"]] == [
        "",
        "",
        "accept",
        "masking",
        "block",
    ]
    assert analysis_log.to_dict() == document
