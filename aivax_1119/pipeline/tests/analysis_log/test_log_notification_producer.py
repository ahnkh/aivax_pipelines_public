from unittest.mock import Mock

import pytest

from src.application.services.analysis_log_merge import AnalysisLogMergeService
from src.core.constants import ERR_FAIL, ERR_OK
from src.domain.analysis_log_delivery.models import MergedAnalysisLogDocument
from src.domain.analysis_log_merge.models.log_contract import AnalysisLogContract


def test_merged_log_enqueues_user_notification_without_user_database_write() -> None:
    user_outbox = Mock()
    service = AnalysisLogMergeService(
        Mock(),
        Mock(),
        user_notification_outbox=user_outbox,
    )

    service._enqueue_notification_events(
        {
            "@timestamp": "2026-09-10T00:00:00Z",
            "message_id": "message-1",
            "client_host": "203.0.113.8",
            "ai_service_id": 1,
            "ai_service": "chatgpt",
            "prompt": "hello",
            "user": {"email": "user@example.com", "name": "홍길동"},
        }
    )

    event = user_outbox.enqueue.call_args.args[0]
    assert event.client_ip == "203.0.113.8"
    assert event.message_id == "message-1"
    assert event.email == "user@example.com"
    assert event.ai_service == 1
    assert event.prompt == "hello"
    assert event.name == "홍길동"


@pytest.mark.parametrize("name", [None, "", "   "])
def test_merged_log_omits_empty_user_name(name) -> None:
    user_outbox = Mock()
    service = AnalysisLogMergeService(
        Mock(), Mock(), user_notification_outbox=user_outbox
    )
    service._enqueue_notification_events(
        {
            "message_id": "message-1",
            "ai_service_id": 1,
            "user": {"email": "user@example.com", "name": name},
        }
    )
    assert user_outbox.enqueue.call_args.args[0].name is None


def test_output_completed_log_enqueues_policy_notification_after_delivery() -> None:
    analysis_log_delivery = Mock()
    analysis_log_delivery.deliver.return_value = True
    user_outbox = Mock()
    policy_outbox = Mock()
    service = AnalysisLogMergeService(
        Mock(),
        analysis_log_delivery,
        user_notification_outbox=user_outbox,
        policy_notification_outbox=policy_outbox,
    )

    result = service.deliver(
        {
            "@timestamp": "2026-09-14T01:02:03Z",
            "message_id": "message-1",
            "ai_service_id": 1,
            "ai_service": "GPT",
            "prompt": "사용자 입력",
            "mode": "block",
            "output_text": "차단됨",
            "policy_name": "주민등록번호 탐지",
            "user": {
                "id": "user-1",
                "email": "user@example.com",
                "uuid": "user-uuid",
            },
        }
    )

    assert result == ERR_OK
    user_outbox.enqueue.assert_called_once()
    policy_event = policy_outbox.enqueue.call_args.args[0]
    assert policy_event.message_id == "message-1"
    assert policy_event.mode == "block"
    assert policy_event.policy_name == "주민등록번호 탐지"
    assert policy_event.user_uuid == "user-uuid"


def test_late_input_merge_emits_policy_notification_after_output_delivery() -> None:
    store = Mock()
    store.set_input_and_pop_output.return_value = {
        "message_id": "message-1",
        "output_text": "차단됨",
    }
    analysis_log_delivery = Mock()
    analysis_log_delivery.deliver.return_value = True
    policy_outbox = Mock()
    service = AnalysisLogMergeService(
        store,
        analysis_log_delivery,
        policy_notification_outbox=policy_outbox,
    )

    result = service.record_input(
        "message-1",
        {
            "@timestamp": "2026-09-14T01:02:03Z",
            "message_id": "message-1",
            "ai_service": "GPT",
            "prompt": "사용자 입력",
            "mode": "block",
            "policy": {"policy_name": "IP - v4"},
            "user": {"id": "user-1"},
        },
    )

    assert result == ERR_OK
    policy_event = policy_outbox.enqueue.call_args.args[0]
    assert policy_event.message_id == "message-1"
    assert policy_event.policy_name == "IP - v4"
    assert policy_event.output_text == "차단됨"


def test_merged_log_is_delivered_to_analysis_log_delivery_port() -> None:
    analysis_log_delivery = Mock()
    analysis_log_delivery.deliver.return_value = True
    service = AnalysisLogMergeService(Mock(), analysis_log_delivery)

    result = service.deliver({"message_id": "message-1"})

    assert result == ERR_OK
    analysis_log = analysis_log_delivery.deliver.call_args.args[0]
    assert isinstance(analysis_log, MergedAnalysisLogDocument)
    assert analysis_log.to_dict() == {
        "message_id": "message-1",
        AnalysisLogContract.FIELD_SCHEMA_VERSION: AnalysisLogContract.SCHEMA_VERSION,
    }


def test_merged_log_delivery_failure_does_not_enqueue_notifications() -> None:
    analysis_log_delivery = Mock()
    analysis_log_delivery.deliver.return_value = False
    user_outbox = Mock()
    service = AnalysisLogMergeService(
        Mock(),
        analysis_log_delivery,
        user_notification_outbox=user_outbox,
    )

    result = service.deliver({"message_id": "message-1"})

    assert result == ERR_FAIL
    user_outbox.enqueue.assert_not_called()
