"""첨부파일 알림을 로컬 버퍼에서 Valkey Stream으로 전달하는 outbox."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import traceback

from valkey import Valkey

from src.adapters.notification_attachment.mapper import AttachmentNotificationMapper
from src.core.ddd.domain_exception import DomainException
from src.domain.notification_attachment.models import AttachmentNotificationEvent
from src.domain.notification_attachment.ports import AttachmentNotificationOutboxPort


_DEDUPLICATED_XADD_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    return false
end
local stream_id = redis.call('XADD', KEYS[2], '*', unpack(ARGV, 2))
redis.call('SET', KEYS[1], '1', 'EX', ARGV[1])
return stream_id
"""


@dataclass(frozen=True, slots=True)
class _PendingAttachmentNotification:
    event: AttachmentNotificationEvent
    retry_count: int = 0

    def next_retry(self) -> "_PendingAttachmentNotification":
        return _PendingAttachmentNotification(self.event, self.retry_count + 1)


class ValkeyAttachmentNotificationOutboxAdapter(
    AttachmentNotificationOutboxPort,
):
    """첨부파일 이벤트를 발행하되 Stream과 consumer group은 관리하지 않는다."""

    _MAX_RETRY_COUNT = 3

    def __init__(
        self,
        client: Valkey,
        stream: str,
        *,
        flush_interval_seconds: float = 1,
        event_dedupe_ttl_seconds: int = 86_400,
        logger: logging.Logger | None = None,
    ) -> None:
        if not stream.strip():
            raise ValueError("stream은 비어 있을 수 없습니다.")
        if flush_interval_seconds <= 0 or event_dedupe_ttl_seconds <= 0:
            raise ValueError("flush 주기와 dedupe TTL은 양수여야 합니다.")

        self._logger = logger or logging.getLogger("khan-python")
        self._client = client
        self._stream = stream
        self._mapper = AttachmentNotificationMapper()
        self._flush_interval_seconds = flush_interval_seconds
        self._event_dedupe_ttl_seconds = event_dedupe_ttl_seconds
        self._lock = threading.Lock()
        self._pending: list[_PendingAttachmentNotification] = []
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            name="attachment-notification-flush",
            target=self._run,
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, event: AttachmentNotificationEvent) -> None:
        with self._lock:
            self._pending.append(_PendingAttachmentNotification(event))

    def flush(self) -> int:
        with self._lock:
            pending = self._pending
            self._pending = []
        if not pending:
            return 0

        persisted_events: list[_PendingAttachmentNotification] = []
        fields_list: list[dict[str, str | int]] = []
        retry_events: list[_PendingAttachmentNotification] = []
        for pending_event in pending:
            try:
                fields_list.append(self._mapper.toPersistence(pending_event.event))
                persisted_events.append(pending_event)
            except DomainException as error:
                self._logger.error(
                    "첨부파일 알림 도메인 이벤트 폐기 "
                    f"domain={error.domain} code={error.code} details={error.details}",
                )
            except Exception:
                retry_events.append(pending_event)
                self._logger.error(
                    "첨부파일 알림 이벤트 직렬화 실패, 다음 주기에 재시도\n"
                    f"{traceback.format_exc()}",
                )

        if retry_events:
            self._requeue(retry_events)
        if not persisted_events:
            return 0

        try:
            pipeline = self._client.pipeline(transaction=False)
            for pending_event, fields in zip(persisted_events, fields_list):
                # Stream trim/delete와 consumer group 관리는 Management 소유다.
                stream_args: list[str] = [
                    str(self._event_dedupe_ttl_seconds),
                ]
                for name, value in fields.items():
                    # Valkey Stream field/value는 wire에서 byte string이 된다.
                    stream_args.extend((name, str(value)))
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
                "첨부파일 알림 batch flush 실패, 다음 주기에 재시도\n"
                f"{traceback.format_exc()}",
            )
            return 0

        failed_events: list[_PendingAttachmentNotification] = []
        published_count = 0
        for pending_event, result in zip(persisted_events, results):
            if isinstance(result, Exception):
                failed_events.append(pending_event)
            elif result is not None:
                published_count += 1
        if failed_events:
            self._requeue(failed_events)
            self._logger.error(
                f"첨부파일 알림 Stream 발행 실패 {len(failed_events)}건, "
                "다음 주기에 재시도",
            )
        return published_count

    def _requeue(
        self,
        pending_events: list[_PendingAttachmentNotification],
    ) -> None:
        retry_events: list[_PendingAttachmentNotification] = []
        for pending_event in pending_events:
            if pending_event.retry_count >= self._MAX_RETRY_COUNT:
                self._logger.error(
                    "첨부파일 알림 이벤트 최대 재시도 횟수 초과로 폐기 "
                    f"event_id={pending_event.event.event_id} "
                    f"retry_count={pending_event.retry_count}",
                )
                continue
            retry_events.append(pending_event.next_retry())

        if retry_events:
            with self._lock:
                self._pending = retry_events + self._pending

    def _dedupe_key(self, event: AttachmentNotificationEvent) -> str:
        return f"{self._stream}:dedupe:{event.event_id}"

    def close(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=self._flush_interval_seconds + 1.0)
        try:
            self.flush()
        except Exception:
            self._logger.error(
                f"최종 첨부파일 알림 flush 실패\n{traceback.format_exc()}",
            )

    def _run(self) -> None:
        while not self._stopped.wait(self._flush_interval_seconds):
            try:
                self.flush()
            except Exception:
                self._logger.error(
                    "첨부파일 알림 batch flush 실패, 다음 주기에 재시도\n"
                    f"{traceback.format_exc()}",
                )
