"""프로세스 로컬 메모리를 사용하는 분석 로그 병합 저장소."""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass
from typing import Any

from src.domain.analysis_log_merge.models.analysis_log_message_key import (
    AnalysisLogMessageKey as LogMessageKey,
)
from src.domain.analysis_log_merge.models.analysis_log_payload import (
    AnalysisLogPayload as LogPayload,
)
from src.domain.analysis_log_merge.models.expired_analysis_log import (
    ExpiredAnalysisLog as ExpiredLog,
)
from src.domain.analysis_log_merge.models.unmatched_output_resolution import (
    UnmatchedOutputResolution,
)
from src.domain.analysis_log_merge.ports.analysis_log_merge_store import (
    AnalysisLogMergeStorePort as LogMergeStorePort,
)


@dataclass(slots=True)
class _StoredLog:
    """메모리 저장소가 보관하는 로그 snapshot과 Valkey TTL 대응 정보."""

    payload: dict[str, Any]
    expires_at: float


class MemoryAnalysisLogMergeStore(LogMergeStorePort):
    """
    단일 process에서 분석 로그를 저장하는 스토어.

    모든 상태 전이는 하나의 lock 안에서 수행한다. 따라서 이 구현체는
    multi-worker/multi-node 공유 저장소로 사용할 수 없다. 이 backend를 선택하면
    Pipeline은 단일 worker로 실행해야 한다.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        input_flush_after_seconds: int = 0,
        late_output_ttl_seconds: int = 0,
    ) -> None:
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._input_flush_after_seconds = max(
            0, min(int(input_flush_after_seconds), self._ttl_seconds - 1)
        )
        # 입력 검사가 병합 창보다 길어도 응답을 잃지 않도록 보관 시간을 더 둔다.
        self._late_output_ttl_seconds = max(
            self._ttl_seconds, int(late_output_ttl_seconds)
        )
        self._lock = threading.Lock()
        self._inputs: dict[str, _StoredLog] = {}
        self._pending_outputs: dict[str, _StoredLog] = {}
        self._late_outputs: dict[str, _StoredLog] = {}
        self._flushed_inputs: dict[str, float] = {}
        self._input_expiry_deadlines: dict[str, float] = {}
        self._output_expiry_deadlines: dict[str, float] = {}

    def set_input_and_pop_output(
        self,
        message_key: LogMessageKey,
        input_log: LogPayload,
    ) -> LogPayload | None:
        """INPUT을 저장하고 대기 중인 OUTPUT이 있으면 함께 회수한다."""
        key = message_key.value
        now = time.monotonic()
        with self._lock:
            self._read_active(self._inputs, key, now)
            # Valkey SET NX와 같게 먼저 들어온 INPUT은 보존한다.
            if key not in self._inputs:
                self._inputs[key] = self._stored_log(input_log, now)
            if self._input_flush_after_seconds > 0:
                self._input_expiry_deadlines.setdefault(
                    key,
                    now + self._input_flush_after_seconds,
                )

            output = self._pop_active(self._pending_outputs, key, now)
            if output is None:
                output = self._pop_active(self._late_outputs, key, now)
            if output is None:
                return None

            self._late_outputs.pop(key, None)
            self._flushed_inputs.pop(key, None)
            self._inputs.pop(key, None)
            self._input_expiry_deadlines.pop(key, None)
            self._output_expiry_deadlines.pop(key, None)
            return output

    def pop_input_or_set_output(
        self,
        message_key: LogMessageKey,
        output_log: LogPayload,
    ) -> LogPayload | None:
        """INPUT을 회수하거나 없으면 OUTPUT을 저장한다."""
        key = message_key.value
        now = time.monotonic()
        with self._lock:
            input_log = self._pop_active(self._inputs, key, now)
            if input_log is not None:
                self._late_outputs.pop(key, None)
                self._input_expiry_deadlines.pop(key, None)
                self._output_expiry_deadlines.pop(key, None)
                return input_log

            # 새로 도착한 OUTPUT이 이전 보관분보다 우선한다.
            self._late_outputs.pop(key, None)
            self._set_output(key, output_log, now)
            return None

    def pop_input(self, message_key: LogMessageKey) -> LogPayload | None:
        """대기 중인 INPUT만 회수한다. OUTPUT은 그대로 둔다."""
        key = message_key.value
        now = time.monotonic()
        with self._lock:
            input_log = self._pop_active(self._inputs, key, now)
            if input_log is not None:
                self._input_expiry_deadlines.pop(key, None)
            return input_log

    def pop_expired_inputs(self, limit: int = 200) -> list[ExpiredLog]:
        """상대 OUTPUT 없이 만료된 INPUT을 회수한다."""
        if self._input_flush_after_seconds <= 0:
            return []

        now = time.monotonic()
        with self._lock:
            self._drop_expired_flushed(now)
            expired: list[ExpiredLog] = []
            for key in self._due_keys(self._input_expiry_deadlines, now, limit):
                self._input_expiry_deadlines.pop(key, None)
                payload = self._pop_active(self._inputs, key, now)
                late_output = self._pop_active(self._late_outputs, key, now)
                if payload is not None:
                    self._flushed_inputs[key] = now + self._late_output_ttl_seconds
                expired.append(ExpiredLog(LogMessageKey(key), payload, late_output))
            return expired

    def pop_expired_outputs(self, limit: int = 200) -> list[ExpiredLog]:
        """상대 INPUT 없이 만료된 OUTPUT을 회수한다."""
        if self._input_flush_after_seconds <= 0:
            return []

        now = time.monotonic()
        with self._lock:
            for key in [
                key
                for key, entry in self._late_outputs.items()
                if entry.expires_at <= now
            ]:
                self._late_outputs.pop(key, None)
            expired: list[ExpiredLog] = []
            for key in self._due_keys(self._output_expiry_deadlines, now, limit):
                self._output_expiry_deadlines.pop(key, None)
                payload = self._pop_active(self._pending_outputs, key, now)
                expired.append(ExpiredLog(LogMessageKey(key), payload))
            return expired

    def park_late_output(
        self,
        message_key: LogMessageKey,
        output_log: LogPayload,
    ) -> None:
        """만료된 OUTPUT을 늦은 입력 TTL 동안 보관한다. 만료 일정에는 넣지 않는다."""
        now = time.monotonic()
        with self._lock:
            self._late_outputs[message_key.value] = self._stored_late_log(
                output_log, now
            )

    def resolve_unmatched_output(
        self,
        message_key: LogMessageKey,
        output_log: LogPayload,
    ) -> UnmatchedOutputResolution:
        """대기 INPUT, 적재 표시, 보관 중 하나를 한 lock 안에서 고른다."""
        key = message_key.value
        now = time.monotonic()
        with self._lock:
            self._drop_expired_flushed(now)
            input_log = self._pop_active(self._inputs, key, now)
            if input_log is not None:
                self._late_outputs.pop(key, None)
                self._flushed_inputs.pop(key, None)
                self._input_expiry_deadlines.pop(key, None)
                self._output_expiry_deadlines.pop(key, None)
                return UnmatchedOutputResolution("merge", input_log)
            if key in self._flushed_inputs:
                return UnmatchedOutputResolution("revise")
            self._late_outputs[key] = self._stored_late_log(output_log, now)
            return UnmatchedOutputResolution("park")

    def _set_output(self, key: str, output_log: LogPayload, now: float) -> None:
        """Valkey SET EX와 같게 OUTPUT을 덮어쓰고 만료 일정을 갱신한다."""
        self._pending_outputs[key] = self._stored_log(output_log, now)
        if self._input_flush_after_seconds > 0:
            self._output_expiry_deadlines[key] = now + self._input_flush_after_seconds

    def _stored_log(self, payload: LogPayload, now: float) -> _StoredLog:
        """호출자 변경이 대기 로그에 영향을 주지 않도록 snapshot을 저장한다."""
        return self._snapshot(payload, now + self._ttl_seconds)

    def _stored_late_log(self, payload: LogPayload, now: float) -> _StoredLog:
        """입력 검사가 병합 창을 넘겨도 응답이 남도록 더 긴 TTL로 보관한다."""
        return self._snapshot(payload, now + self._late_output_ttl_seconds)

    @staticmethod
    def _snapshot(payload: LogPayload, expires_at: float) -> _StoredLog:
        return _StoredLog(
            payload=copy.deepcopy(dict(payload)),
            expires_at=expires_at,
        )

    def _drop_expired_flushed(self, now: float) -> None:
        for key in [
            key for key, expires_at in self._flushed_inputs.items() if expires_at <= now
        ]:
            self._flushed_inputs.pop(key, None)

    @staticmethod
    def _due_keys(
        deadlines: dict[str, float],
        now: float,
        limit: int,
    ) -> list[str]:
        due = [key for key, deadline in deadlines.items() if deadline <= now]
        due.sort(key=deadlines.__getitem__)
        return due[: max(1, int(limit))]

    def _read_active(
        self,
        entries: dict[str, _StoredLog],
        key: str,
        now: float,
    ) -> dict[str, Any] | None:
        entry = entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= now:
            entries.pop(key, None)
            return None
        return entry.payload

    def _pop_active(
        self,
        entries: dict[str, _StoredLog],
        key: str,
        now: float,
    ) -> dict[str, Any] | None:
        payload = self._read_active(entries, key, now)
        if payload is not None:
            entries.pop(key, None)
        return payload
