from unittest.mock import Mock

from src.application.services.analysis_log_merge import (
    AnalysisLogMergeService,
)
from src.core.constants import ERR_OK
from src.domain.analysis_log_delivery.ports import (
    AnalysisLogDeliveryPort,
)
from src.domain.analysis_log_merge.ports import (
    AnalysisLogMergeStorePort,
)


def _completed_document() -> dict:
    return {
        "@timestamp": "2026-09-23T00:27:46Z",
        "message_id": "message-1",
        "client_host": "127.0.0.1",
        "user": {
            "email": "tester@example.com",
        },
        "mode": "block",
        "policy": {
            "policy_name": "이메일",
            "category": "개인정보",
        },
        "output_text": "차단되었습니다.",
    }


def test_completed_log_is_enqueued_to_cds() -> None:
    store = Mock(spec=AnalysisLogMergeStorePort)
    delivery = Mock(spec=AnalysisLogDeliveryPort)
    delivery.deliver.return_value = True
    cds_delivery = Mock(spec=AnalysisLogDeliveryPort)
    cds_delivery.deliver.return_value = True

    service = AnalysisLogMergeService(
        store,
        delivery,
        additional_deliveries=(cds_delivery,),
    )

    document = _completed_document()

    assert service.deliver(document) == ERR_OK
    cds_delivery.deliver.assert_called_once()

    delivered_document = cds_delivery.deliver.call_args.args[0]
    assert delivered_document.to_dict()["message_id"] == "message-1"


def test_cds_failure_does_not_block_analysis_log_delivery() -> None:
    store = Mock(spec=AnalysisLogMergeStorePort)
    delivery = Mock(spec=AnalysisLogDeliveryPort)
    delivery.deliver.return_value = True
    cds_delivery = Mock(spec=AnalysisLogDeliveryPort)
    cds_delivery.deliver.side_effect = RuntimeError("CDS failure")

    service = AnalysisLogMergeService(
        store,
        delivery,
        additional_deliveries=(cds_delivery,),
        logger=Mock(),
    )

    assert service.deliver(_completed_document()) == ERR_OK
    delivery.deliver.assert_called_once()
