from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass

from valkey import Valkey

import logging
from src.adapters.notification_policy.mapper import PolicyNotificationMapper
from src.core.ddd.domain_exception import DomainException
from src.domain.notification_policy.models.policy_notification_event import (
    PolicyNotificationEvent,
)
from src.domain.notification_policy.ports.policy_notification_outbox import (
    PolicyNotificationOutboxPort,
)


_DEDUPLICATED_XADD_SCRIPT = """
-- Lua script는 실행 중 다른 Valkey 명령과 섞이지 않는다.
-- marker는 XADD 뒤에 저장해야 Stream 쓰기 실패가 재시도를 막지 않는다.
if redis.call('EXISTS', KEYS[1]) == 1 then
    return false
end
local stream_id = redis.call('XADD', KEYS[2], '*', unpack(ARGV, 2))
redis.call('SET', KEYS[1], '1', 'EX', ARGV[1])
return stream_id
"""


@dataclass(frozen=True, slots=True)
class _PendingPolicyNotification:
    """로컬 버퍼의 이벤트와 현재까지 수행한 재시도 횟수."""

    event: PolicyNotificationEvent
    retry_count: int = 0

    def next_retry(self) -> "_PendingPolicyNotification":
        return _PendingPolicyNotification(self.event, self.retry_count + 1)


class ValkeyPolicyNotificationOutboxAdapter(PolicyNotificationOutboxPort):
    """정책 알림을 발행하되 Stream 수명주기와 consumer group은 관리하지 않는다."""

    # 첫 시도 이후 최대 세 번만 재시도한다.
    _MAX_RETRY_COUNT = 3

    def __init__(
        self,
        client: Valkey,
        stream: str,
        *,
        flush_interval_seconds: float = 1,
        event_dedupe_ttl_seconds: int = 86400,
        logger: logging.Logger | None = None,
    ) -> None:
        if not stream.strip():
            raise ValueError("stream은 비어 있을 수 없습니다.")
        if flush_interval_seconds <= 0 or event_dedupe_ttl_seconds <= 0:
            raise ValueError("flush 주기와 dedupe TTL은 양수여야 합니다.")
        self._logger = (
            logger if logger is not None else logging.getLogger("khan-python")
        )
        self._client = client
        self._stream = stream
        self._mapper = PolicyNotificationMapper()
        # timer와 dedupe marker 보존 기간은 조립부에서 전달한 환경 설정을 따른다.
        self._flush_interval_seconds = flush_interval_seconds
        self._event_dedupe_ttl_seconds = event_dedupe_ttl_seconds
        self._lock = threading.Lock()
        self._pending: list[_PendingPolicyNotification] = []
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            name="policy-notification-flush",
            target=self._run,
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, event: PolicyNotificationEvent) -> None:
        """이벤트를 프로세스 로컬 버퍼에 적재한다."""
        with self._lock:
            self._pending.append(_PendingPolicyNotification(event))

    def flush(self) -> int:
        """대기 중인 이벤트를 Valkey Stream에 batch로 발행한다."""
        with self._lock:
            # enqueue 중인 새 이벤트와 이번 batch를 분리해 lock 점유 시간을 짧게 유지한다.
            pending = self._pending
            self._pending = []
        if not pending:
            return 0
        persisted_events: list[_PendingPolicyNotification] = []
        fields_list: list[dict[str, str]] = []
        retry_events: list[_PendingPolicyNotification] = []
        for pending_event in pending:
            try:
                fields_list.append(self._mapper.toPersistence(pending_event.event))
                persisted_events.append(pending_event)
            except DomainException as error:
                # 불변식 위반은 재시도해도 성공하지 않으므로 영구 폐기한다.
                self._logger.error(
                    "정책 알림 도메인 이벤트 폐기 "
                    f"domain={error.domain} code={error.code} details={error.details}",
                )
            except Exception:
                # 일시적인 직렬화 실패는 정상 이벤트 발행을 막지 않고 재시도한다.
                retry_events.append(pending_event)
                self._logger.error(
                    "정책 알림 이벤트 직렬화 실패, 다음 주기에 재시도\n"
                    f"{traceback.format_exc()}",
                )

        if retry_events:
            self._requeue(retry_events)

        if not persisted_events:
            return 0

        try:
            pipeline = self._client.pipeline(transaction=False)
            for pending_event, fields in zip(persisted_events, fields_list):
                # EVAL 하나가 marker 확인·XADD·TTL 저장을 원자적으로 처리한다.
                # Stream trim/delete와 consumer group 관리는 Management 소유다.
                stream_args = [str(self._event_dedupe_ttl_seconds)]
                for name, value in fields.items():
                    stream_args.extend((name, value))
                pipeline.eval(
                    _DEDUPLICATED_XADD_SCRIPT,
                    2,
                    self._dedupe_key(pending_event.event),
                    self._stream,
                    *stream_args,
                )
            results = pipeline.execute(raise_on_error=False)
        except Exception:
            self._requeue(persisted_events)
            self._logger.error(
                "정책 알림 batch flush 실패, 이벤트를 다음 주기에 재시도\n"
                f"{traceback.format_exc()}",
            )
            return 0

        failed_events: list[_PendingPolicyNotification] = []
        published_count = 0
        for pending_event, result in zip(persisted_events, results):
            if isinstance(result, Exception):
                failed_events.append(pending_event)
            elif result is not None:
                published_count += 1
            # None은 이미 marker가 존재한 중복 이벤트다. 재시도하지 않는다.
        if failed_events:
            self._requeue(failed_events)
            self._logger.error(
                f"정책 알림 Stream 발행 실패 {len(failed_events)}건, 다음 주기에 재시도",
            )
        return published_count

    def _requeue(self, pending_events: list[_PendingPolicyNotification]) -> None:
        """최대 세 번까지만 재시도하고 한도를 넘긴 이벤트는 폐기한다."""
        retry_events: list[_PendingPolicyNotification] = []
        for pending_event in pending_events:
            if pending_event.retry_count >= self._MAX_RETRY_COUNT:
                self._logger.error(
                    "정책 알림 이벤트 최대 재시도 횟수 초과로 폐기 "
                    f"event_id={pending_event.event.event_id} "
                    f"retry_count={pending_event.retry_count}",
                )
                continue
            retry_events.append(pending_event.next_retry())

        if retry_events:
            with self._lock:
                self._pending = retry_events + self._pending

    def _dedupe_key(self, event: PolicyNotificationEvent) -> str:
        """Stream별로 event_id를 구분하는 TTL 중복 제거 marker key를 만든다."""
        return f"{self._stream}:dedupe:{event.event_id}"

    def close(self) -> None:
        """타이머를 중단하고 남은 이벤트를 한 번 발행한다."""
        self._stopped.set()
        self._thread.join(timeout=self._flush_interval_seconds + 1.0)
        try:
            self.flush()
        except Exception:
            self._logger.error(f"최종 정책 알림 flush 실패\n{traceback.format_exc()}")

    def _run(self) -> None:
        # Event.wait는 interval 동안 block되므로 CPU를 점유하는 busy loop가 아니다.
        while not self._stopped.wait(self._flush_interval_seconds):
            try:
                self.flush()
            except Exception:
                self._logger.error(
                    "정책 알림 batch flush 실패, 다음 주기에 재시도\n"
                    f"{traceback.format_exc()}",
                )
