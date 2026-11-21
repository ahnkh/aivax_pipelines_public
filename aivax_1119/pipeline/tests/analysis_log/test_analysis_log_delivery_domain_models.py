import pytest

from src.domain.analysis_log_delivery.errors import AnalysisLogDeliveryDomainError
from src.domain.analysis_log_delivery.models import MergedAnalysisLogDocument


def test_merged_analysis_log_document_copies_source_document() -> None:
    source = {"message_id": "message-1", "user": {"email": "user@example.com"}}

    document = MergedAnalysisLogDocument(source)
    source["user"]["email"] = "changed@example.com"

    assert document.to_dict()["user"]["email"] == "user@example.com"


@pytest.mark.parametrize("value", [None, [], "not-a-document"])
def test_merged_analysis_log_document_rejects_non_mapping(value: object) -> None:
    with pytest.raises(AnalysisLogDeliveryDomainError, match="key/value"):
        MergedAnalysisLogDocument(value)  # type: ignore[arg-type]


def test_merged_analysis_log_document_rejects_empty_document() -> None:
    with pytest.raises(AnalysisLogDeliveryDomainError, match="빈 병합"):
        MergedAnalysisLogDocument({})
