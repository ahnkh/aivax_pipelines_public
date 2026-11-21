"""Valkey를 사용하는 입력·출력 로그 병합 저장소."""

from __future__ import annotations

import time
import traceback
import logging
from typing import Any, cast

import orjson
from valkey import Valkey

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


class ValkeyAnalysisLogMergeStore(LogMergeStorePort):
    """Lua script로 다중 worker 환경의 로그 병합 상태 전이를 보장한다."""

    _SET_INPUT_AND_POP_OUTPUT = """
local input_key = KEYS[1]
local pending_output_key = KEYS[2]
local input_expiry_index_key = KEYS[3]
local output_expiry_index_key = KEYS[4]
local late_output_key = KEYS[5]
local flushed_key = KEYS[6]
local payload = ARGV[1]
local ttl_seconds = tonumber(ARGV[2])
local message_key = ARGV[3]
local expiry_deadline = tonumber(ARGV[4])

redis.call('SET', input_key, payload, 'NX', 'EX', ttl_seconds)
if expiry_deadline > 0 then
    redis.call('ZADD', input_expiry_index_key, 'NX', expiry_deadline, message_key)
end

local output = redis.call('GETDEL', pending_output_key)
if not output then
    output = redis.call('GETDEL', late_output_key)
end
if output then
    redis.call('DEL', late_output_key)
    redis.call('DEL', flushed_key)
    redis.call('GETDEL', input_key)
    redis.call('ZREM', input_expiry_index_key, message_key)
    redis.call('ZREM', output_expiry_index_key, message_key)
    return {1, output}
end

return {0, false}
"""

    _POP_INPUT_OR_SET_OUTPUT = """
local input_key = KEYS[1]
local pending_output_key = KEYS[2]
local input_expiry_index_key = KEYS[3]
local output_expiry_index_key = KEYS[4]
local late_output_key = KEYS[5]
local payload = ARGV[1]
local ttl_seconds = tonumber(ARGV[2])
local message_key = ARGV[3]
local expiry_deadline = tonumber(ARGV[4])

local input = redis.call('GETDEL', input_key)
if input then
    redis.call('DEL', late_output_key)
    redis.call('ZREM', input_expiry_index_key, message_key)
    redis.call('ZREM', output_expiry_index_key, message_key)
    return {1, input}
end

redis.call('DEL', late_output_key)
redis.call('SET', pending_output_key, payload, 'EX', ttl_seconds)
if expiry_deadline > 0 then
    redis.call('ZADD', output_expiry_index_key, expiry_deadline, message_key)
end
return {0, false}
"""

    _RESOLVE_UNMATCHED_OUTPUT = """
local input_key = KEYS[1]
local late_output_key = KEYS[2]
local flushed_key = KEYS[3]
local input_expiry_index_key = KEYS[4]
local output_expiry_index_key = KEYS[5]
local payload = ARGV[1]
local late_ttl_seconds = tonumber(ARGV[2])
local message_key = ARGV[3]

local input = redis.call('GETDEL', input_key)
if input then
    redis.call('DEL', late_output_key)
    redis.call('DEL', flushed_key)
    redis.call('ZREM', input_expiry_index_key, message_key)
    redis.call('ZREM', output_expiry_index_key, message_key)
    return {1, input}
end
if redis.call('EXISTS', flushed_key) == 1 then
    return {2, false}
end
redis.call('SET', late_output_key, payload, 'EX', late_ttl_seconds)
return {0, false}
"""

    _CLAIM_EXPIRED_INPUT = """
local input_key = KEYS[1]
local late_output_key = KEYS[2]
local flushed_key = KEYS[3]
local input_expiry_index_key = KEYS[4]
local message_key = ARGV[1]
local flushed_ttl_seconds = tonumber(ARGV[2])

if redis.call('ZREM', input_expiry_index_key, message_key) == 0 then
    return {0, false, false}
end
local input = redis.call('GETDEL', input_key)
local late = redis.call('GETDEL', late_output_key)
if input then
    redis.call('SET', flushed_key, '1', 'EX', flushed_ttl_seconds)
end
return {1, input or false, late or false}
"""

    _POP_INPUT = """
local input_key = KEYS[1]
local input_expiry_index_key = KEYS[2]
local message_key = ARGV[1]

local input = redis.call('GETDEL', input_key)
if input then
    redis.call('ZREM', input_expiry_index_key, message_key)
    return {1, input}
end
return {0, false}
"""

    def __init__(
        self,
        client: Valkey,
        *,
        key_prefix: str,
        ttl_seconds: int,
        input_flush_after_seconds: int,
        late_output_ttl_seconds: int = 0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._logger = logger or logging.getLogger("khan-python")
        self._key_prefix = key_prefix
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._input_flush_after_seconds = max(
            0,
            min(
                int(input_flush_after_seconds),
                self._ttl_seconds - 1,
            ),
        )
        self._late_output_ttl_seconds = max(
            self._ttl_seconds, int(late_output_ttl_seconds)
        )
        self._set_input_and_pop_output_script = self._client.register_script(
            self._SET_INPUT_AND_POP_OUTPUT,
        )
        self._pop_input_or_set_output_script = self._client.register_script(
            self._POP_INPUT_OR_SET_OUTPUT,
        )
        self._pop_input_script = self._client.register_script(self._POP_INPUT)
        self._resolve_unmatched_output_script = self._client.register_script(
            self._RESOLVE_UNMATCHED_OUTPUT,
        )
        self._claim_expired_input_script = self._client.register_script(
            self._CLAIM_EXPIRED_INPUT,
        )

    def set_input_and_pop_output(
        self,
        message_key: LogMessageKey,
        input_log: LogPayload,
    ) -> LogPayload | None:
        """INPUT을 저장하고 먼저 도착한 OUTPUT이 있으면 원자적으로 회수한다."""
        try:
            result = self._set_input_and_pop_output_script(
                keys=(
                    self._input_key(message_key),
                    self._pending_output_key(message_key),
                    self._input_expiry_index_key(),
                    self._output_expiry_index_key(),
                    self._late_output_key(message_key),
                    self._flushed_input_key(message_key),
                ),
                args=(
                    self._serialize(input_log),
                    str(self._ttl_seconds),
                    message_key.value,
                    str(self._expiry_deadline()),
                ),
            )
            return self._payload_from_result(result)
        except Exception:
            self._log_operation_error("INPUT 저장 및 OUTPUT 회수", message_key)
            raise

    def pop_input_or_set_output(
        self,
        message_key: LogMessageKey,
        output_log: LogPayload,
    ) -> LogPayload | None:
        """INPUT을 회수하거나 아직 없으면 OUTPUT을 대기 상태로 저장한다."""
        try:
            result = self._pop_input_or_set_output_script(
                keys=(
                    self._input_key(message_key),
                    self._pending_output_key(message_key),
                    self._input_expiry_index_key(),
                    self._output_expiry_index_key(),
                    self._late_output_key(message_key),
                ),
                args=(
                    self._serialize(output_log),
                    str(self._ttl_seconds),
                    message_key.value,
                    str(self._expiry_deadline()),
                ),
            )
            return self._payload_from_result(result)
        except Exception:
            self._log_operation_error("INPUT 회수 또는 OUTPUT 대기 저장", message_key)
            raise

    def pop_input(self, message_key: LogMessageKey) -> LogPayload | None:
        """대기 중인 INPUT만 회수한다. OUTPUT은 건드리지 않는다."""
        try:
            result = self._pop_input_script(
                keys=(
                    self._input_key(message_key),
                    self._input_expiry_index_key(),
                ),
                args=(message_key.value,),
            )
            return self._payload_from_result(result)
        except Exception:
            self._log_operation_error("INPUT 회수", message_key)
            raise

    def pop_expired_inputs(self, limit: int = 200) -> list[ExpiredLog]:
        """만료된 INPUT을 다른 worker와 중복 없이 회수한다."""
        if self._input_flush_after_seconds <= 0:
            return []

        try:
            expired: list[ExpiredLog] = []
            index_key = self._input_expiry_index_key()
            members = cast(
                list[str | bytes],
                self._client.zrangebyscore(
                    index_key,
                    "-inf",
                    time.time(),
                    start=0,
                    num=max(1, int(limit)),
                ),
            )
            for member in members or []:
                message_key = LogMessageKey(self._decode_member(member))
                result = self._claim_expired_input_script(
                    keys=(
                        self._input_key(message_key),
                        self._late_output_key(message_key),
                        self._flushed_input_key(message_key),
                        index_key,
                    ),
                    args=(message_key.value, str(self._late_output_ttl_seconds)),
                )
                if not isinstance(result, (list, tuple)) or int(result[0] or 0) != 1:
                    continue
                expired.append(
                    ExpiredLog(
                        message_key,
                        self._deserialize_or_none(result[1] if len(result) > 1 else None),
                        self._deserialize_or_none(result[2] if len(result) > 2 else None),
                    )
                )
            return expired
        except Exception:
            self._logger.error(
                f"Valkey 만료 INPUT 회수 실패\n{traceback.format_exc()}",
            )
            raise

    def pop_expired_outputs(self, limit: int = 200) -> list[ExpiredLog]:
        """만료된 대기 OUTPUT을 다른 worker와 중복 없이 회수한다."""
        if self._input_flush_after_seconds <= 0:
            return []

        try:
            expired: list[ExpiredLog] = []
            index_key = self._output_expiry_index_key()
            members = cast(
                list[str | bytes],
                self._client.zrangebyscore(
                    index_key,
                    "-inf",
                    time.time(),
                    start=0,
                    num=max(1, int(limit)),
                ),
            )
            for member in members or []:
                message_key = LogMessageKey(self._decode_member(member))
                if not self._client.zrem(index_key, member):
                    continue
                raw_payload = self._client.getdel(self._pending_output_key(message_key))
                expired.append(
                    ExpiredLog(message_key, self._deserialize_or_none(raw_payload))
                )
            return expired
        except Exception:
            self._logger.error(
                f"Valkey 만료 OUTPUT 회수 실패\n{traceback.format_exc()}",
            )
            raise

    def park_late_output(
        self,
        message_key: LogMessageKey,
        output_log: LogPayload,
    ) -> None:
        """만료된 OUTPUT을 늦은 입력 TTL 동안 보관한다. 만료 색인에는 넣지 않는다."""
        try:
            self._client.set(
                self._late_output_key(message_key),
                self._serialize(output_log),
                ex=self._late_output_ttl_seconds,
            )
        except Exception:
            self._log_operation_error("늦은 OUTPUT 보관", message_key)
            raise

    def resolve_unmatched_output(
        self,
        message_key: LogMessageKey,
        output_log: LogPayload,
    ) -> UnmatchedOutputResolution:
        """대기 INPUT, 적재 표시, 보관 중 하나를 Lua script 한 번으로 고른다."""
        try:
            result = self._resolve_unmatched_output_script(
                keys=(
                    self._input_key(message_key),
                    self._late_output_key(message_key),
                    self._flushed_input_key(message_key),
                    self._input_expiry_index_key(),
                    self._output_expiry_index_key(),
                ),
                args=(
                    self._serialize(output_log),
                    str(self._late_output_ttl_seconds),
                    message_key.value,
                ),
            )
        except Exception:
            self._log_operation_error("만료 OUTPUT 처리", message_key)
            raise
        if not isinstance(result, (list, tuple)) or not result:
            raise ValueError("Valkey 만료 OUTPUT 처리 결과가 비어 있습니다.")
        action = int(result[0] or 0)
        if action == 1:
            input_log = self._deserialize_or_none(result[1] if len(result) > 1 else None)
            if input_log is None:
                raise ValueError("Valkey 병합 성공 결과에 상대 로그가 없습니다.")
            return UnmatchedOutputResolution("merge", input_log)
        if action == 2:
            return UnmatchedOutputResolution("revise")
        return UnmatchedOutputResolution("park")

    def _expiry_deadline(self) -> float:
        if self._input_flush_after_seconds <= 0:
            return 0.0
        return time.time() + self._input_flush_after_seconds

    def _input_key(self, message_key: LogMessageKey) -> str:
        return f"{self._key_prefix}{message_key.value}"

    def _pending_output_key(self, message_key: LogMessageKey) -> str:
        return f"{self._key_prefix}outpend:{message_key.value}"

    def _late_output_key(self, message_key: LogMessageKey) -> str:
        return f"{self._key_prefix}outlate:{message_key.value}"

    def _flushed_input_key(self, message_key: LogMessageKey) -> str:
        return f"{self._key_prefix}flushed:{message_key.value}"

    def _input_expiry_index_key(self) -> str:
        return f"{self._key_prefix}index"

    def _output_expiry_index_key(self) -> str:
        return f"{self._key_prefix}outpend_index"

    @staticmethod
    def _serialize(payload: LogPayload) -> bytes:
        # 외부 호출자가 넘긴 Mapping은 저장 직전에 snapshot으로 고정한다.
        return orjson.dumps(dict(payload))

    @staticmethod
    def _deserialize_or_none(raw_payload: object) -> dict[str, Any] | None:
        if raw_payload is None or raw_payload is False:
            return None
        if isinstance(raw_payload, str):
            raw_payload = raw_payload.encode("utf-8")
        if not isinstance(raw_payload, (bytes, bytearray)):
            raise ValueError("Valkey 로그 payload는 bytes 또는 문자열이어야 합니다.")
        payload = orjson.loads(raw_payload)
        if not isinstance(payload, dict):
            raise ValueError("Valkey 로그 payload는 JSON object여야 합니다.")
        return payload

    def _payload_from_result(self, result: object) -> LogPayload | None:
        if not isinstance(result, (list, tuple)) or not result:
            return None
        matched = int(result[0] or 0) == 1
        if not matched:
            return None
        counterpart_log = self._deserialize_or_none(
            result[1] if len(result) > 1 else None,
        )
        if counterpart_log is None:
            raise ValueError("Valkey 병합 성공 결과에 상대 로그가 없습니다.")
        return counterpart_log

    @staticmethod
    def _decode_member(member: object) -> str:
        if isinstance(member, bytes):
            return member.decode("utf-8", "replace")
        return str(member)

    def _log_operation_error(
        self,
        operation: str,
        message_key: LogMessageKey,
    ) -> None:
        self._logger.error(
            f"Valkey 로그 병합 {operation} 실패 "
            f"message_key={message_key.value}\n{traceback.format_exc()}",
        )
