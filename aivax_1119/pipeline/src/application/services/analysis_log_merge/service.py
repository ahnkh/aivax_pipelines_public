"""INPUT/OUTPUT 분석 로그를 병합하고 후속 전달을 오케스트레이션한다."""

import logging
import traceback
from collections.abc import Mapping, Sequence
from typing import Any, cast

from src.core.constants import ERR_FAIL, ERR_OK
from src.core.ddd.domain_exception import DomainException
from src.domain.analysis_log_delivery.models.merged_analysis_log_document import (
    MergedAnalysisLogDocument,
)
from src.domain.analysis_log_delivery.models.user_consent import is_user_consent_on
from src.domain.analysis_log_delivery.ports.analysis_log_delivery_port import (
    AnalysisLogDeliveryPort,
)
from src.domain.analysis_log_merge.models.analysis_log_message_key import (
    AnalysisLogMessageKey,
)
from src.domain.analysis_log_merge.models.log_contract import (
    AnalysisLogContract as LOG_INDEX_DEFINE,
)
from src.domain.analysis_log_merge.ports.analysis_log_merge_store import (
    AnalysisLogMergeStorePort,
)
from src.domain.analysis_log_merge.services import AnalysisLogMerger
from src.domain.notification_policy.models.occurred_at import OccurredAt
from src.domain.notification_policy.models.policy_notification_event import (
    PolicyNotificationEvent,
    PolicyNotificationMode,
)
from src.domain.notification_policy.ports.policy_notification_outbox import (
    PolicyNotificationOutboxPort,
)
from src.domain.notification_user.models.user_notification_event import (
    UserNotificationEvent,
)
from src.domain.notification_user.ports.user_notification_outbox import (
    UserNotificationOutboxPort,
)


class AnalysisLogMergeService:
    """INPUT/OUTPUT을 message_id로 병합해 분석 로그 전달 흐름에 넘긴다."""

    def __init__(
        self,
        store: AnalysisLogMergeStorePort,
        delivery: AnalysisLogDeliveryPort,
        *,
        additional_deliveries: Sequence[AnalysisLogDeliveryPort] = (),
        policy_notification_outbox: PolicyNotificationOutboxPort | None = None,
        user_notification_outbox: UserNotificationOutboxPort | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = (
            logger if logger is not None else logging.getLogger("khan-python")
        )
        self._store = store
        self._merger = AnalysisLogMerger()
        self._delivery = delivery
        self._policy_notification_outbox = policy_notification_outbox
        self._user_notification_outbox = user_notification_outbox
        self._additional_deliveries = tuple(additional_deliveries)

    def record(self, log_type: int, message_id: str, document: dict) -> int:
        """호환용 진입점: INPUT 또는 OUTPUT 분석 로그를 기록한다."""
        if LOG_INDEX_DEFINE.TYPE_LOG_INPUT == log_type:
            self._record_input(message_id, document)
        elif LOG_INDEX_DEFINE.TYPE_LOG_OUTPUT == log_type:
            self._record_output(message_id, document)
        else:
            self._logger.error(f"invalid log type {log_type}")
            return ERR_FAIL
        return ERR_OK

    def deliver(self, document: dict) -> int:
        """병합 또는 만료 처리로 완성된 분석 로그를 외부 전달 흐름에 태운다."""
        return self._deliver(document)

    def record_output(self, message_id: str, analysis_log: dict) -> int:
        """출력 로그를 기록하거나 대기 중인 입력과 병합한다."""
        return self._record_output(message_id, analysis_log)

    def revise_output(self, message_id: str, analysis_log: dict) -> int:
        """이미 전달된 응답이 길어지면 그 문서의 본문만 갱신한다."""
        document = dict(analysis_log)
        document["message_id"] = message_id
        writer = getattr(self._delivery, "write_output_revision", None)
        if writer is None:
            self._logger.error("output revision writer is not configured")
            return ERR_FAIL
        try:
            if not writer(document):
                return ERR_FAIL
        except Exception:
            self._logger.error(
                "OUTPUT 로그 본문 갱신 실패 "
                f"messageid={message_id}\n{traceback.format_exc()}",
            )
            return ERR_FAIL
        return ERR_OK

    def record_input(self, message_id: str, analysis_log: dict) -> int:
        """입력 로그를 기록하거나 먼저 도착한 출력과 병합한다."""
        return self._record_input(message_id, analysis_log)

    def _record_input(self, strLogMessageKey: str, dictLogData: dict) -> int:
        try:
            output_log = self._store.set_input_and_pop_output(
                AnalysisLogMessageKey(strLogMessageKey),
                dictLogData,
            )
            if output_log is not None:
                self._logger.info(
                    "late input merge with stored output, messageid=%s",
                    strLogMessageKey,
                )
                dictMerged = self._merger.merge(
                    dictLogData,
                    output_log,
                )
                return self._deliver(dictMerged)
            return ERR_OK
        except Exception:
            self._logger.error(
                "INPUT 로그 병합 저장 실패 "
                f"messageid={strLogMessageKey}\n{traceback.format_exc()}",
            )
            return ERR_FAIL

    def _record_output(self, strLogMessageKey: str, dictLogData: dict) -> int:
        try:
            # 동의 후 실제 모델 응답: 이미 전달된 BLOCK 행의 output_text를 덮어쓴다.
            # pending INPUT이 없어도 late-output으로 쌓아 두면 만료 시 폐기되므로
            # 여기서 바로 consent patch를 전달한다.
            if is_user_consent_on(dictLogData.get("user_consent")):
                input_log = self._store.pop_input(
                    AnalysisLogMessageKey(strLogMessageKey),
                )
                document = dict(dictLogData)
                document["message_id"] = strLogMessageKey
                if input_log is not None and is_user_consent_on(
                    input_log.get("user_consent")
                ):
                    document = self._merger.merge(input_log, document)
                    document["user_consent"] = "on"
                    self._logger.info(
                        "consent output merge messageid=%s",
                        strLogMessageKey,
                    )
                    return self._deliver(document)
                if input_log is not None:
                    # MASKING 등 일반 INPUT이 대기 중이면 동의 패치가 아니다.
                    document.pop("user_consent", None)
                    return self._deliver(self._merger.merge(input_log, document))
                document["user_consent"] = "on"
                self._logger.info(
                    "consent output patch without pending input messageid=%s",
                    strLogMessageKey,
                )
                return self._deliver(document)

            input_log = self._store.pop_input_or_set_output(
                AnalysisLogMessageKey(strLogMessageKey),
                dictLogData,
            )
            if input_log is not None:
                dictPromptLog = self._merger.merge(
                    input_log,
                    dictLogData,
                )
                return self._deliver(dictPromptLog)

            self._logger.info(
                "output stored for late input, "
                f"messageid={strLogMessageKey}, "
                f"service={dictLogData.get('ai_service')}",
            )
            return ERR_OK
        except Exception:
            self._logger.error(
                "OUTPUT 로그 병합 처리 실패 "
                f"messageid={strLogMessageKey}\n{traceback.format_exc()}",
            )
            return ERR_FAIL

    def _deliver(self, dictPromptLog: dict) -> int:
        """완성 분석 로그를 외부 전달 흐름에 태운다."""
        dictPromptLog[LOG_INDEX_DEFINE.FIELD_SCHEMA_VERSION] = (
            LOG_INDEX_DEFINE.SCHEMA_VERSION
        )

        # CDS는 Fluent Bit 전달 성공 여부와 독립적으로 저장한다.
        # CDS 적재 실패는 기존 분석 로그 전달 결과에 영향을 주지 않는다.
        # 동의 갱신은 최초 차단 로그의 CDS·사용자·정책 이벤트를 재발행하지 않는다.
        consent_update = is_user_consent_on(dictPromptLog.get("user_consent"))
        if not consent_update:
            self._enqueue_additional_deliveries(dictPromptLog)

        try:
            delivered = self._delivery.deliver(
                MergedAnalysisLogDocument(dictPromptLog),
            )
            if not delivered:
                self._logger.error(
                    "병합 분석 로그 파일 전달 실패",
                )
                return ERR_FAIL

            if not consent_update:
                self._enqueue_notification_events(dictPromptLog)
            return ERR_OK
        except Exception:
            self._logger.error(
                f"병합 분석 로그 전달 대기 적재 실패\n{traceback.format_exc()}",
            )
            return ERR_FAIL

    def _enqueue_additional_deliveries(
        self,
        document: Mapping[str, Any],
    ) -> None:
        """완성 분석 로그를 선택된 보조 전달 흐름에 각각 인계한다."""
        analysis_log = MergedAnalysisLogDocument(document)
        for delivery in self._additional_deliveries:
            try:
                if not delivery.deliver(analysis_log):
                    self._logger.error("보조 분석 로그 전달 대기 적재 실패")
            except Exception:
                self._logger.error(
                    f"보조 분석 로그 전달 대기 적재 실패\n{traceback.format_exc()}",
                )

    def _enqueue_notification_events(self, document: Mapping[str, Any]) -> None:
        """저장된 병합 로그에서 파생한 이벤트만 각 outbox에 적재한다."""
        self._enqueue_user_notification(document)
        self._enqueue_policy_notification(document)

    def _enqueue_user_notification(self, document: Mapping[str, Any]) -> None:
        """최종 전달된 병합 로그의 사용자 동기화 이벤트를 outbox에 적재한다."""
        if self._user_notification_outbox is None:
            return
        user = document.get("user")
        user = user if isinstance(user, Mapping) else {}
        ai_service = document.get("ai_service_id")
        if ai_service is None:
            ai_service = user.get("ai_service_id")
        try:
            self._user_notification_outbox.enqueue(
                UserNotificationEvent(
                    message_id=str(document.get("message_id") or "").strip(),
                    client_ip=self._optional_text(
                        document.get("client_host") or user.get("client_host"),
                    ),
                    email=str(user.get("email") or document.get("user_email") or ""),
                    # 표시명(GPT 등)이 아닌 원본 숫자 ID를 그대로 전달한다.
                    ai_service=ai_service,
                    prompt=self._optional_text(document.get("prompt")),
                    name=self._optional_text(user.get("name")),
                )
            )
        except DomainException as error:
            self._logger.error(
                "사용자 알림 도메인 이벤트 폐기 "
                f"domain={error.domain} code={error.code} details={error.details}",
            )
        except Exception:
            self._logger.error(
                f"사용자 알림 이벤트 적재 실패\n{traceback.format_exc()}",
            )

    @staticmethod
    def _optional_text(value: object) -> str | None:
        """동기화 의도가 없는 빈 선택 필드를 Stream에서 생략할 값으로 바꾼다."""
        if value is None:
            return None
        text = str(value)
        return text if text.strip() else None

    def _enqueue_policy_notification(self, document: Mapping[str, Any]) -> None:
        """병합 로그의 정책 탐지 결과를 정책 알림 outbox에 적재한다."""
        if self._policy_notification_outbox is None:
            return
        try:
            event = self._create_policy_notification_event(document)
            if event is not None:
                self._policy_notification_outbox.enqueue(event)
        except DomainException as error:
            self._logger.error(
                "정책 알림 도메인 이벤트 폐기 "
                f"domain={error.domain} code={error.code} details={error.details}",
            )
        except Exception:
            self._logger.error(
                f"정책 알림 이벤트 적재 실패\n{traceback.format_exc()}",
            )

    @staticmethod
    def _create_policy_notification_event(
        document: Mapping[str, Any],
    ) -> PolicyNotificationEvent | None:
        """저장된 병합 로그에서 정책 알림 도메인 이벤트를 만든다."""
        mode = str(document.get("mode") or "").strip().lower()
        if mode not in {"block", "masking", "accept"}:
            return None

        user = document.get("user")
        user = user if isinstance(user, Mapping) else {}
        policy = document.get("policy")
        policy = policy if isinstance(policy, Mapping) else {}
        policy_name = policy.get("policy_name") or document.get("policy_name")
        return PolicyNotificationEvent(
            occurred_at=OccurredAt(str(document.get("@timestamp") or "").strip()),
            message_id=str(document.get("message_id") or "").strip(),
            user_id=str(user.get("id") or "").strip(),
            user_role=user.get("role") if isinstance(user.get("role"), str) else None,
            user_email=str(user.get("email") or ""),
            user_uuid=user.get("uuid") if isinstance(user.get("uuid"), str) else None,
            ai_service=str(document.get("ai_service") or "").strip(),
            prompt=str(document.get("prompt") or "").strip(),
            mode=cast(PolicyNotificationMode, mode),
            output_text=str(document.get("output_text") or ""),
            policy_name=(
                policy_name.strip()
                if isinstance(policy_name, str) and policy_name.strip()
                else None
            ),
        )
