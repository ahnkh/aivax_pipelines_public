from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass

from valkey import Valkey

import logging
from src.adapters.notification_user.mapper import (
    UserNotificationMapper,
)
from src.core.ddd.domain_exception import DomainException
from src.domain.notification_user.models.user_notification_event import (
    UserNotificationEvent,
)
from src.domain.notification_user.ports.user_notification_outbox import (
    UserNotificationOutboxPort,
)


@dataclass(frozen=True, slots=True)
class _PendingUserNotification:
    """로컬 버퍼의 이벤트와 현재까지 수행한 재시도 횟수."""

    event: UserNotificationEvent
    retry_count: int = 0

    def next_retry(self) -> "_PendingUserNotification":
        return _PendingUserNotification(self.event, self.retry_count + 1)


class ValkeyUserNotificationOutboxAdapter(UserNotificationOutboxPort):
    """Valkey client를 주입받아 사용자 동기화 이벤트를 1초 단위로 발행한다."""

    # 첫 시도 이후 최대 세 번만 재시도한다.
    _MAX_RETRY_COUNT = 3
    _FLUSH_INTERVAL_SECONDS = 1

    def __init__(
        self,
        client: Valkey,
        stream: str,
        *,
        flush_interval_seconds: float = _FLUSH_INTERVAL_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        if not stream.strip():
            raise ValueError("stream은 비어 있을 수 없습니다.")

        if flush_interval_seconds <= 0:
            raise ValueError("flush 주기는 양수여야 합니다.")
        self._logger = (
            logger if logger is not None else logging.getLogger("khan-python")
        )
        self._client = client
        self._stream = stream
        self._mapper = UserNotificationMapper()
        self._flush_interval_seconds = flush_interval_seconds
        self._lock = threading.Lock()
        self._pending: list[_PendingUserNotification] = []
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            name="user-sync-flush",
            target=self._run,
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, event: UserNotificationEvent) -> None:
        """이벤트를 프로세스 로컬 버퍼에 적재한다."""
        with self._lock:
            self._pending.append(_PendingUserNotification(event))

    def flush(self) -> int:
        """
        대기 중인 이벤트를 Valkey Stream에 batch로 발행한다.
        도메인 예외 이벤트는 버리고, 일시 실패 이벤트만 다음 주기에 재시도한다.
        """
        with self._lock:
            # enqueue 중인 새 이벤트와 이번 batch를 분리해 lock 점유 시간을 짧게 유지한다.
            pending = self._pending
            self._pending = []

        if not pending:
            return 0

        persisted_events: list[_PendingUserNotification] = []
        fields_list: list[dict[str, str]] = []
        retry_events: list[_PendingUserNotification] = []
        for pending_event in pending:
            try:
                fields_list.append(self._mapper.toPersistence(pending_event.event))
                persisted_events.append(pending_event)
            except DomainException as error:
                # 도메인 예외는 이벤트를 전송하지않는다.
                self._logger.error(
                    "사용자 동기화 도메인 예외"
                    f"domain={error.domain} code={error.code} details={error.details}",
                )
            except Exception:
                # 일시적인 직렬화 실패는 정상 이벤트 발행을 막지 않고 재시도한다.
                retry_events.append(pending_event)
                self._logger.error(
                    "사용자 동기화 이벤트 직렬화 실패, 다음 주기에 재시도\n"
                    f"{traceback.format_exc()}",
                )

        if retry_events:
            self._requeue(retry_events)

        if not persisted_events:
            return 0

        try:
            pipeline = self._client.pipeline(transaction=False)
            for fields in fields_list:
                pipeline.xadd(self._stream, fields)
            results = pipeline.execute(raise_on_error=False)
        except Exception:
            self._requeue(persisted_events)
            self._logger.error(
                "사용자 동기화 batch flush 실패, 이벤트를 다음 주기에 재시도\n"
                f"{traceback.format_exc()}",
            )
            return 0

        failed_events: list[_PendingUserNotification] = []
        published_count = 0
        for pending_event, result in zip(persisted_events, results):
            if isinstance(result, Exception):
                failed_events.append(pending_event)
            elif result is not None:
                published_count += 1
        if failed_events:
            self._requeue(failed_events)
            self._logger.error(
                f"사용자 동기화 Stream 발행 실패 {len(failed_events)}건, 다음 주기에 재시도",
            )
        return published_count

    def _requeue(self, pending_events: list[_PendingUserNotification]) -> None:
        """최대 세 번까지만 재시도하고 한도를 넘긴 이벤트는 폐기한다."""
        retry_events: list[_PendingUserNotification] = []
        for pending_event in pending_events:
            if pending_event.retry_count >= self._MAX_RETRY_COUNT:
                self._logger.error(
                    "사용자 동기화 이벤트 최대 재시도 횟수 초과로 폐기 "
                    f"retry_count={pending_event.retry_count}",
                )
                continue
            retry_events.append(pending_event.next_retry())

        if retry_events:
            with self._lock:
                self._pending = retry_events + self._pending

    def close(self) -> None:
        """타이머를 중단하고 남은 이벤트를 한 번 발행한다."""
        self._stopped.set()
        self._thread.join(timeout=self._flush_interval_seconds + 1.0)
        try:
            self.flush()
        except Exception:
            self._logger.error(
                f"최종 사용자 동기화 flush 실패\n{traceback.format_exc()}"
            )

    def _run(self) -> None:
        # Event.wait는 interval 동안 block되므로 CPU를 점유하는 busy loop가 아니다.
        while not self._stopped.wait(self._flush_interval_seconds):
            try:
                self.flush()
            except Exception:
                self._logger.error(
                    "사용자 동기화 batch flush 실패, 다음 주기에 재시도\n"
                    f"{traceback.format_exc()}",
                )
