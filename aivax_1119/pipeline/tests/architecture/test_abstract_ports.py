"""도메인 port가 런타임에서도 구현 누락을 거부하는지 검증한다."""

import inspect

import pytest

from src.domain.analysis_log_delivery.ports import (
    AnalysisLogBatchDeliveryPort,
    AnalysisLogDeliveryQueuePort,
)
from src.domain.analysis_log_delivery.ports.analysis_log_delivery_port import (
    AnalysisLogDeliveryPort,
)
from src.domain.analysis_log_merge.ports.analysis_log_merge_store import (
    AnalysisLogMergeStorePort,
)
from src.domain.config_aivax.ports import AivaxConfigPort
from src.domain.delivery_file.ports import FileDeliveryPort
from src.domain.inspection_input.ports import InputInspectorPort
from src.domain.inspection_multimodal.ports import (
    MultimodalInspectionPort,
)
from src.domain.inspection_regex.ports import RegexInspectionPort
from src.domain.inspection_slm.ports import SlmInspectionPort
from src.domain.notification_attachment.ports import (
    AttachmentNotificationOutboxPort,
)
from src.domain.notification_audit.ports import AuditNotificationPort
from src.domain.notification_policy.ports.policy_notification_outbox import (
    PolicyNotificationOutboxPort,
)
from src.domain.notification_user.ports.user_notification_outbox import (
    UserNotificationOutboxPort,
)
from src.domain.policy_multimodal.ports import (
    MultimodalPolicyPort,
    MultimodalPolicyProviderPort,
)
from src.domain.policy_regex.ports import RegexPolicyPort, RegexPolicyProviderPort
from src.domain.policy_slm.ports import SlmPolicyPort, SlmPolicyProviderPort

PORT_TYPES = (
    AnalysisLogDeliveryPort,
    AnalysisLogBatchDeliveryPort,
    AnalysisLogDeliveryQueuePort,
    AnalysisLogMergeStorePort,
    AivaxConfigPort,
    InputInspectorPort,
    FileDeliveryPort,
    MultimodalInspectionPort,
    RegexInspectionPort,
    SlmInspectionPort,
    AttachmentNotificationOutboxPort,
    AuditNotificationPort,
    PolicyNotificationOutboxPort,
    UserNotificationOutboxPort,
    MultimodalPolicyProviderPort,
    MultimodalPolicyPort,
    RegexPolicyProviderPort,
    RegexPolicyPort,
    SlmPolicyProviderPort,
    SlmPolicyPort,
)


def test_all_domain_ports_are_abstract() -> None:
    assert all(inspect.isabstract(port_type) for port_type in PORT_TYPES)


def test_incomplete_port_implementation_cannot_be_instantiated() -> None:
    class IncompleteRegexInspection(RegexInspectionPort):
        pass

    with pytest.raises(TypeError, match=r"abstract method .*inspect"):
        IncompleteRegexInspection()
