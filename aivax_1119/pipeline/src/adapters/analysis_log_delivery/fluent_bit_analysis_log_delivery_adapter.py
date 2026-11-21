"""Fluent Bit 감시 폴더에 분석 로그를 즉시 기록하는 adapter."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import orjson

from src.core.time import TimeNormalizer
from src.domain.analysis_log_delivery.errors import RetryableAnalysisLogDeliveryError
from src.domain.analysis_log_delivery.errors.consent_log_not_found_error import (
    ConsentLogNotFoundError,
)
from src.domain.analysis_log_delivery.models.merged_analysis_log_document import (
    MergedAnalysisLogDocument,
)
from src.domain.analysis_log_delivery.models.user_consent import (
    CONSENT_LOG_TIMESTAMP_FIELD,
    is_user_consent_on,
)
from src.domain.analysis_log_delivery.ports.analysis_log_consent_writer_port import (
    CONSENT_STATE_TTL_SECONDS,
    AnalysisLogConsentWriterPort,
)
from src.domain.analysis_log_delivery.ports.analysis_log_delivery_port import (
    BULK_LOG_ARCHIVE_AGE_SECONDS,
    AnalysisLogDeliveryPort,
)
from src.domain.analysis_log_delivery.services.detection_metadata import (
    detection_metadata,
)


class FluentBitAnalysisLogDeliveryAdapter(
    AnalysisLogDeliveryPort,
    AnalysisLogConsentWriterPort,
):
    """Fluent Bit 감시 경로에 병합 분석 로그를 NDJSON으로 즉시 기록한다."""

    _BULK_FILE_NAME = re.compile(
        r"^aivax_log#(\d{8})#\d+\.json(?:\.(?:consent|revision))?$"
    )

    def __init__(
        self, bulk_directory: str | Path, *, logger: logging.Logger | None = None
    ) -> None:
        self._bulk_directory = Path(bulk_directory)
        self._logger = (
            logger if logger is not None else logging.getLogger("khan-python")
        )

    def deliver(self, analysis_log: MergedAnalysisLogDocument) -> bool:
        """분석 로그 한 건을 Fluent Bit 감시 파일에 즉시 append한다."""
        document = analysis_log.to_dict()
        try:
            # 배포 전에 병합 저장소에 대기하던 로그도 저장 계약에 맞춘다.
            if document.get("mode") in ("pass", "fail", "failed"):
                document["mode"] = ""
            for detection in document.get("filter_detect") or []:
                if not isinstance(detection, dict):
                    continue
                if detection.get("mode") in (
                    "pass",
                    "fail",
                    "failed",
                ):
                    detection["mode"] = ""
                for summary in detection.get("file_summary") or []:
                    if isinstance(summary, dict) and summary.get("action") == "pass":
                        summary["action"] = ""
            if "filter_detect" in document:
                document.update(detection_metadata(document))
            if document.get("@timestamp"):
                document["@timestamp"] = TimeNormalizer.to_utc_iso(
                    document["@timestamp"], timespec="microseconds"
                )
            self._bulk_directory.mkdir(parents=True, exist_ok=True)
            message_id = str(document.get("message_id") or "").strip()
            original_timestamp = document.pop(CONSENT_LOG_TIMESTAMP_FIELD, None)
            if is_user_consent_on(document.get("user_consent")):
                try:
                    timestamp = original_timestamp or self._block_timestamp(message_id)
                except ConsentLogNotFoundError:
                    self._logger.info(
                        "동의 병합 로그 갱신 생략: 원본 상태 없음 message_id=%s",
                        message_id,
                    )
                    return True
                fields = {
                    key: document[key]
                    for key in ("output_text", "text_truncated_bytes", "cso")
                    if key in document
                }
                # 화면에는 동의라고 보여도, 저장 프롬프트는 그때 전송한 원문이다.
                stored_prompt = str(document.get("prompt") or "").strip()
                if stored_prompt and stored_prompt not in {"동의", "동의합니다"}:
                    fields["prompt"] = stored_prompt
                # 출력 없이 만료된 동의 입력은 기존 차단 문구를 지우지 않는다.
                if document.get("merge_state") == "input_only":
                    fields.pop("output_text", None)
                    fields.pop("text_truncated_bytes", None)
                self._append(
                    self._file_path().with_suffix(".json.consent"),
                    {
                        **fields,
                        "@timestamp": TimeNormalizer.to_utc_iso(
                            timestamp, timespec="microseconds"
                        ),
                        "message_id": message_id,
                        "user_consent": "on",
                    },
                )
                return True
            if document.get("mode") == "block" and message_id:
                # Keep only the original timestamp, not another copy of prompt
                # or evidence. This survives restarts and is shared by workers.
                state_path = self._state_path(message_id)
                with self._consent_state_lock(), state_path.open("a+") as state_file:
                    state_file.seek(0)
                    raw = state_file.read()
                    timestamp = (
                        TimeNormalizer.to_utc_iso(
                            orjson.loads(raw)["timestamp"], timespec="microseconds"
                        )
                        if raw
                        else TimeNormalizer.to_utc_iso(
                            document.get("@timestamp") or TimeNormalizer.utc_now(),
                            timespec="microseconds",
                        )
                    )
                    document["@timestamp"] = timestamp
                    self._append(self._file_path(), document)
                    if not raw:
                        state_file.write(
                            orjson.dumps({"timestamp": timestamp}).decode()
                        )
                        state_file.flush()
                        os.fsync(state_file.fileno())
            else:
                self._append(self._file_path(), document)
        except Exception:
            self._logger.error(
                f"Fluent Bit 분석 로그 파일 기록 실패\n{traceback.format_exc()}",
            )
            return False
        return True

    def write_output_revision(self, document: Mapping[str, object]) -> bool:
        """Replace output_text on an existing analysis log. Other fields stay."""
        message_id = str(document.get("message_id") or "").strip()
        if not message_id:
            return False
        timestamp = document.get("@timestamp") or TimeNormalizer.to_utc_iso(
            TimeNormalizer.utc_now(), timespec="microseconds"
        )
        try:
            self._append(
                self._file_path().with_suffix(".json.revision"),
                {
                    "@timestamp": TimeNormalizer.to_utc_iso(
                        timestamp, timespec="microseconds"
                    ),
                    "message_id": message_id,
                    "output_text": document.get("output_text"),
                    "text_truncated_bytes": document.get("text_truncated_bytes") or 0,
                    # 프롬프트 단독으로 먼저 적재된 문서가 응답을 갖게 되면
                    # 더 이상 input_only가 아니다.
                    "merge_state": "",
                },
            )
        except (OSError, ValueError, TypeError):
            self._logger.exception(
                "output revision file delivery failed message_id=%s", message_id
            )
            return False
        return True

    def write_consent_update(
        self, message_id: str, output_text: str | None, text_truncated_bytes: int
    ) -> None:
        """Append a partial update; Fluent Bit must never upsert this record."""
        mid = message_id.strip()
        if not mid:
            raise ConsentLogNotFoundError("Original BLOCK message_id is required")
        timestamp = self._block_timestamp(mid)
        try:
            self._append(
                self._file_path().with_suffix(".json.consent"),
                {
                    "@timestamp": timestamp,
                    "message_id": mid,
                    "output_text": output_text,
                    "text_truncated_bytes": text_truncated_bytes,
                    "user_consent": "on",
                },
            )
        except (OSError, ValueError, TypeError) as error:
            self._logger.exception(
                "Consent log file delivery failed message_id=%s", mid
            )
            raise RetryableAnalysisLogDeliveryError(
                "Consent log file delivery failed"
            ) from error

    def require_block_log(self, message_id: str) -> None:
        self._block_timestamp(message_id.strip())

    def read_block_log(self, message_id: str) -> dict[str, object]:
        mid = message_id.strip()
        timestamp = self._block_timestamp(mid)
        date = datetime.fromisoformat(timestamp).astimezone().strftime("%Y%m%d")
        document = None
        updates = []
        try:
            with self._bulk_log_lock(shared=True):
                for path in sorted(self._bulk_directory.glob("aivax_log#*.json*")):
                    match = self._BULK_FILE_NAME.fullmatch(path.name)
                    if match is None or match.group(1) < date:
                        continue
                    with path.open("rb") as source:
                        fcntl.flock(source, fcntl.LOCK_SH)
                        for line in source:
                            record = orjson.loads(line)
                            if (
                                record.get("message_id") != mid
                                or record.get("@timestamp") != timestamp
                            ):
                                continue
                            if path.suffix == ".consent":
                                updates.append(record)
                            elif record.get("mode") == "block":
                                document = record
            if document is None:
                raise ConsentLogNotFoundError("기존 차단 분석 로그를 찾을 수 없습니다.")
            # 갱신 파일은 worker별로 나뉘므로 필터 완료 시각 순서로 누적한다.
            updates.sort(
                key=lambda item: max(
                    (
                        detection.get("completed_at", "")
                        for detection in item.get("filter_detect", [])
                    ),
                    default="",
                )
            )
            for update in updates:
                for field in (
                    "filter_detect",
                    "masked_contents",
                    "cso",
                    "user_consent",
                    "prompt",
                    "output_text",
                    "text_truncated_bytes",
                ):
                    if field in update:
                        document[field] = update[field]
            return document
        except (OSError, ValueError, TypeError) as error:
            raise RetryableAnalysisLogDeliveryError(
                "기존 차단 분석 로그 조회에 실패했습니다."
            ) from error

    def write_analysis_update(
        self, message_id: str, fields: Mapping[str, object]
    ) -> None:
        timestamp = self._block_timestamp(message_id.strip())
        try:
            self._append(
                self._file_path().with_suffix(".json.consent"),
                {**fields, "message_id": message_id, "@timestamp": timestamp},
            )
        except (OSError, ValueError, TypeError) as error:
            raise RetryableAnalysisLogDeliveryError(
                "동의 분석 로그 갱신에 실패했습니다."
            ) from error

    def _block_timestamp(self, message_id: str) -> str:
        if not message_id:
            raise ConsentLogNotFoundError("Original BLOCK message_id is required")
        try:
            with self._consent_state_lock():
                state_path = self._state_path(message_id)
                with state_path.open() as state_file:
                    # 조회와 동의 재전송으로 보관기간을 연장하지 않는다.
                    if time.time() - os.fstat(state_file.fileno()).st_mtime >= (
                        CONSENT_STATE_TTL_SECONDS
                    ):
                        state_path.unlink()
                        raise ConsentLogNotFoundError(
                            "User consent state expired after 30 minutes"
                        )
                    state = orjson.loads(state_file.read())
            return TimeNormalizer.to_utc_iso(
                state["timestamp"], timespec="microseconds"
            )
        except FileNotFoundError as error:
            raise ConsentLogNotFoundError(
                "User consent requires a previously blocked request; "
                "no original BLOCK log is available. "
                "Check the original decision: "
                "masking/accept use the normal output flow."
            ) from error
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._logger.exception(
                "Consent log state read failed message_id=%s", message_id
            )
            raise RetryableAnalysisLogDeliveryError(
                "Consent log state read failed"
            ) from error

    def expire_consent_states(self) -> int:
        """원본 로그·동의 출력과 분리해 오래된 상태 파일만 정리한다."""
        directory = self._bulk_directory / ".consent-state"
        if not directory.is_dir():
            return 0
        deleted = 0
        with self._consent_state_lock(blocking=False) as locked:
            if not locked:
                return 0
            cutoff = time.time() - CONSENT_STATE_TTL_SECONDS
            for path in directory.glob("*.json"):
                if (
                    len(path.stem) != 64
                    or any(char not in "0123456789abcdef" for char in path.stem)
                    or path.is_symlink()
                ):
                    continue
                try:
                    if path.is_file() and path.stat().st_mtime <= cutoff:
                        path.unlink()
                        deleted += 1
                except FileNotFoundError:
                    continue
                except OSError:
                    self._logger.exception("Consent state cleanup failed path=%s", path)
        return deleted

    @contextmanager
    def _consent_state_lock(self, *, blocking: bool = True) -> Iterator[bool]:
        # 파일별 lock은 unlink 후 같은 이름으로 생성된 inode를 보호하지 못한다.
        # worker와 재시작 사이에서도 같은 directory lock을 사용한다.
        directory = self._bulk_directory / ".consent-state"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ".lock").open("a") as lock_file:
            try:
                fcntl.flock(
                    lock_file,
                    fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB),
                )
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _state_path(self, message_id: str) -> Path:
        key = hashlib.sha256(message_id.encode()).hexdigest()
        return self._bulk_directory / ".consent-state" / f"{key}.json"

    def _append(self, path: Path, document: dict) -> None:
        # A record can exceed PIPE_BUF. Serialize appends across threads and
        # processes so Fluent Bit never reads interleaved NDJSON records.
        with self._bulk_log_lock(shared=True), path.open("ab") as output_file:
            fcntl.flock(output_file, fcntl.LOCK_EX)
            output_file.write(orjson.dumps(document) + b"\n")
            output_file.flush()

    def archive_bulk_logs(self, backup_directory: str) -> int:
        """87400초 지난 원본·동의 로그를 파일명 날짜별로 이동한다."""
        if not self._bulk_directory.is_dir():
            return 0
        backup_root = Path(backup_directory)
        if backup_root.resolve() == self._bulk_directory.resolve():
            raise ValueError("Bulk log source and backup directories must differ")
        archived = 0
        with self._bulk_log_lock(shared=False, blocking=False) as locked:
            if not locked:
                return 0
            cutoff = time.time() - BULK_LOG_ARCHIVE_AGE_SECONDS
            for source in self._bulk_directory.iterdir():
                match = self._BULK_FILE_NAME.fullmatch(source.name)
                if match is None or source.is_symlink():
                    continue
                try:
                    if not source.is_file():
                        continue
                    source_stat = source.stat()
                    if source_stat.st_mtime > cutoff:
                        continue
                    date = match.group(1)
                    datetime.strptime(date, "%Y%m%d")
                    destination = backup_root / date / source.name
                    if destination.exists():
                        self._logger.warning(
                            "Bulk log archive destination already exists: %s",
                            destination,
                        )
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    self._move_to_archive(source, destination, source_stat)
                    archived += 1
                    self._logger.info("Bulk log archived %s => %s", source, destination)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError):
                    self._logger.exception("Bulk log archive failed source=%s", source)
        return archived

    @staticmethod
    def _move_to_archive(
        source: Path, destination: Path, source_stat: os.stat_result
    ) -> None:
        # 다른 파티션으로 이동해도 복사를 마치기 전에 원본을 삭제하지 않는다.
        temporary_path: Path | None = None
        try:
            with (
                source.open("rb") as input_file,
                tempfile.NamedTemporaryFile(
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as output_file,
            ):
                temporary_path = Path(output_file.name)
                shutil.copyfileobj(input_file, output_file)
                output_file.flush()
                os.fsync(output_file.fileno())
                current = source.stat()
                if (
                    output_file.tell() != source_stat.st_size
                    or current.st_size != source_stat.st_size
                    or current.st_mtime_ns != source_stat.st_mtime_ns
                ):
                    raise OSError("Bulk log changed during archive")
                os.fchmod(output_file.fileno(), source_stat.st_mode & 0o777)
                os.utime(
                    temporary_path,
                    ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
                )
                # 기존 백업을 덮어쓰지 않고 복사를 마친 파일만 공개한다.
                os.link(temporary_path, destination)
                source.unlink()
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @contextmanager
    def _bulk_log_lock(self, *, shared: bool, blocking: bool = True) -> Iterator[bool]:
        self._bulk_directory.mkdir(parents=True, exist_ok=True)
        with (self._bulk_directory / ".archive.lock").open("a") as lock_file:
            try:
                fcntl.flock(
                    lock_file,
                    (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                    | (0 if blocking else fcntl.LOCK_NB),
                )
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _file_path(self) -> Path:
        date = datetime.now().strftime("%Y%m%d")
        return self._bulk_directory / f"aivax_log#{date}#{os.getpid()}.json"
