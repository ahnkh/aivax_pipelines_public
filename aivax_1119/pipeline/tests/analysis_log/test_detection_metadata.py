from src.domain.analysis_log_delivery.services.detection_metadata import (
    detection_metadata,
)


def test_projects_only_actual_detections_and_keeps_filter_category_pairs():
    result = detection_metadata(
        {
            "filter_detect": [
                {"filter": "regex", "mode": "", "category": "safe"},
                {
                    "filter": "file block",
                    "mode": "accept",
                    "category": "shared",
                    "file_summary": [{"action": "accept", "category": "file-only"}],
                },
                {"filter": "slm", "mode": "masking", "category": "shared"},
            ]
        }
    )
    assert result == {
        "detected_filters": ["file block", "slm"],
        "detected_filter_categories": [
            "file block|file-only",
            "file block|shared",
            "slm|shared",
        ],
    }


def test_empty_inspections_do_not_become_detections():
    assert detection_metadata(
        {"filter_detect": [{"filter": "slm", "mode": "", "slm_content": "safe"}]}
    ) == {
        "detected_filters": [],
        "detected_filter_categories": [],
    }
