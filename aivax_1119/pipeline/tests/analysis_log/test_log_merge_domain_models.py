import pytest

from src.domain.analysis_log_merge.errors import AnalysisLogMergeDomainError
from src.domain.analysis_log_merge.models import (
    AnalysisLogMessageKey,
    ExpiredAnalysisLog,
)
from src.domain.analysis_log_merge.services import AnalysisLogMerger


def test_log_message_key_rejects_blank_value():
    with pytest.raises(AnalysisLogMergeDomainError) as error:
        AnalysisLogMessageKey("  ")

    assert error.value.code == "empty-message-key"


def test_expired_log_marks_missing_payload_as_lost():
    expired = ExpiredAnalysisLog(AnalysisLogMessageKey("message-1"), None)

    assert expired.is_lost is True


def test_merge_uses_output_identity_to_complete_anonymous_input():
    merged = AnalysisLogMerger().merge(
        {
            "user": {
                "id": "user-1",
                "email": "",
                "uuid": "input-uuid",
            },
            "prompt": "question",
        },
        {
            "user_id": "user-1",
            "user_email": "user@example.com",
            "user_uuid": "output-uuid",
            "output_text": "answer",
        },
    )

    assert merged["user"] == {
        "id": "user-1",
        "email": "user@example.com",
        "uuid": "output-uuid",
    }
    assert merged["output_text"] == "answer"


def test_merge_keeps_input_identity_and_does_not_mutate_source_logs():
    input_log = {
        "user": {"id": "user-1", "name": "홍길동", "email": "input@example.com"}
    }
    output_log = {"user": {"email": ""}, "output_text": "answer"}

    merged = AnalysisLogMerger().merge(input_log, output_log)

    assert merged["user"] == {
        "id": "user-1",
        "name": "홍길동",
        "email": "input@example.com",
    }
    assert input_log == {
        "user": {"id": "user-1", "name": "홍길동", "email": "input@example.com"}
    }
    assert output_log == {"user": {"email": ""}, "output_text": "answer"}


def test_generated_only_appends_file_without_replacing_saved_output():
    merged = AnalysisLogMerger().merge(
        {
            "user": {"id": "user-1", "email": "user@example.com"},
            "output_text": "기존 PPT 응답",
            "filter_detect": [{"filter": "file block", "file_summary": []}],
        },
        {
            "generated_only": True,
            "output_text": "",
            "user": {"id": "", "email": ""},
            "generated_files": [
                {
                    "file_name": "slides.pptx",
                    "file_backup_path": (
                        "/data/backup/attach_file_backup/generated/slides.pptx"
                    ),
                    "mime_type": (
                        "application/vnd.openxmlformats-officedocument"
                        ".presentationml.presentation"
                    ),
                    "size": 123,
                }
            ],
        },
    )

    assert merged["output_text"] == "기존 PPT 응답"
    assert merged["user"]["email"] == "user@example.com"
    assert merged["filter_detect"][0]["file_summary"][0]["file_name"] == "slides.pptx"
    assert merged["filter_detect"][0]["file_summary"][0]["action"] == ""


def test_merge_appends_generated_image_as_file_summary():
    merged = AnalysisLogMerger().merge(
        {
            "prompt": "배경화면",
            "filter_detect": [{"filter": "regex", "mode": ""}],
        },
        {
            "output_text": "[이미지 생성] file_abc image/png",
            "generated_files": [
                {
                    "file_name": "file_abc.png",
                    "file_backup_path": (
                        "/data/backup/attach_file_backup/generated/file_abc.png"
                    ),
                    "mime_type": "image/png",
                    "size": 12,
                }
            ],
        },
    )

    file_block = next(
        item for item in merged["filter_detect"] if item["filter"] == "file block"
    )
    assert file_block["file_summary"][0]["file_name"] == "file_abc.png"
    assert file_block["file_summary"][0]["file_info"]["mime_type"] == "image/png"
    assert file_block["file_summary"][0]["action"] == ""
    assert "generated_files" not in merged
    assert merged["filter_detect"][0]["filter"] == "regex"
