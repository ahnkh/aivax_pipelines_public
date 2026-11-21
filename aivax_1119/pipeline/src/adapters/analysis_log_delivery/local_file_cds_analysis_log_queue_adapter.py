"""로컬 파일 기반 CDS 로그 spool adapter."""

from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import uuid
import logging

from src.adapters.analysis_log_delivery.cds_payload_mapper import (
    CdsAnalysisLogPayloadMapper,
)
from src.domain.analysis_log_delivery.models import (
    MergedAnalysisLogDocument,
    PendingAnalysisLog,
    PendingAnalysisLogBatch,
)
from src.domain.analysis_log_delivery.ports import (
    AnalysisLogDeliveryPort,
    AnalysisLogDeliveryQueuePort,
)


class LocalFileCdsAnalysisLogQueueAdapter(
    AnalysisLogDeliveryPort,
    AnalysisLogDeliveryQueuePort,
):
    """CDS 로그를 전송 전까지 로컬 파일로 보관한다."""

    _FILE_PREFIX = "cds_log#"
    _FILE_SUFFIX = ".json"
    _LOCK_FILE_NAME = ".cds_send.lock"
    _FAILED_DIRECTORY_NAME = "failed"

    def __init__(
        self,
        log_directory: str | Path,
        *,
        src_port: int = 0,
        dst_host: str = "example.com",
        dst_port: int = 443,
        logger: logging.Logger | None = None,
    ) -> None:
        self._log_directory = Path(log_directory)
        self._log_directory.mkdir(parents=True, exist_ok=True)
        self._logger = logger or logging.getLogger("khan-python")
        self._mapper = CdsAnalysisLogPayloadMapper(
            src_port=src_port,
            dst_host=dst_host,
            dst_port=dst_port,
        )

    def deliver(self, analysis_log: MergedAnalysisLogDocument) -> bool:
        """완성 분석 로그를 CDS 전송 대기열에 적재한다."""
        document = analysis_log.to_dict()
        message_id = str(document.get("message_id") or "").strip()
        if not message_id:
            return False
        self.enqueue(f"{message_id}:request", analysis_log)
        return True

    def enqueue(
        self,
        event_key: str,
        analysis_log: MergedAnalysisLogDocument,
    ) -> str:
        normalized_event_key = event_key.strip()
        if not normalized_event_key:
            raise ValueError("CDS 로그 이벤트 식별자가 필요합니다.")

        file_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"aivax-cds:{normalized_event_key}",
        ).hex
        final_path = self._log_directory / (
            f"{self._FILE_PREFIX}{file_id}{self._FILE_SUFFIX}"
        )

        document = {
            "event_key": normalized_event_key,
            "retry_count": 0,
            # enqueue 시점에 외부 계약 검증과 설정값 적용을 끝낸다. 잘못된
            # 로그 한 건이 나중에 정상 batch 전체를 막지 않게 한다.
            "payload": self._mapper.toPersistence(analysis_log),
        }

        temporary_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._log_directory,
                prefix=".cds_log#",
                suffix=".tmp",
                delete=False,
            ) as output_file:
                temporary_path = Path(output_file.name)
                json.dump(
                    document,
                    output_file,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                output_file.write("\n")
                output_file.flush()
                os.fsync(output_file.fileno())

            try:
                # 같은 이벤트 파일이 있으면 기존 파일을 유지한다.
                os.link(temporary_path, final_path)
            except FileExistsError:
                pass

            return final_path.name
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def acknowledge(self, entries: Sequence[PendingAnalysisLog]) -> int:
        deleted_count = 0
        for entry in entries:
            try:
                path = self._entry_path(entry.receipt)
                path.unlink(missing_ok=True)
                deleted_count += 1
            except Exception:
                # 한 파일의 삭제 실패가 나머지 성공 파일의 acknowledge를
                # 막거나 실제 삭제 수를 잃게 하지 않는다.
                self._logger.exception(
                    "CDS 성공 파일 삭제 실패 receipt=%s",
                    entry.receipt,
                )
        return deleted_count

    def claim_batch(
        self,
        limit: int,
    ) -> AbstractContextManager[PendingAnalysisLogBatch]:
        return self._claim(limit)

    def claim_pending(
        self,
    ) -> AbstractContextManager[PendingAnalysisLogBatch]:
        return self._claim(None)

    @contextmanager
    def _claim(
        self,
        limit: int | None,
    ) -> Generator[PendingAnalysisLogBatch, None, None]:
        lock_path = self._log_directory / self._LOCK_FILE_NAME

        with lock_path.open("a", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(
                    lock_file.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                yield PendingAnalysisLogBatch(entries=())
                return

            try:
                entries: list[PendingAnalysisLog] = []
                unreadable_count = 0
                for receipt in self._list_pending():
                    if limit is not None and len(entries) >= limit:
                        break
                    try:
                        entries.append(self._read(receipt))
                    except Exception:
                        unreadable_count += 1
                        self._logger.exception(
                            "CDS 로그 파일 읽기 실패 receipt=%s",
                            receipt,
                        )
                        self._quarantine_receipt(receipt)
                yield PendingAnalysisLogBatch(
                    entries=tuple(entries),
                    unreadable_count=unreadable_count,
                )
            finally:
                fcntl.flock(
                    lock_file.fileno(),
                    fcntl.LOCK_UN,
                )

    def reschedule(self, entries: Sequence[PendingAnalysisLog]) -> int:
        updated_count = 0
        for entry in entries:
            path = self._entry_path(entry.receipt)
            try:
                with path.open("r", encoding="utf-8") as input_file:
                    document = json.load(input_file)
                if not isinstance(document, dict):
                    raise ValueError("CDS 로그 파일은 JSON 객체여야 합니다.")
                document["retry_count"] = entry.retry_count + 1
                self._replace_document(path, document)
                updated_count += 1
            except Exception:
                self._logger.exception(
                    "CDS 로그 재시도 횟수 갱신 실패 receipt=%s",
                    entry.receipt,
                )
        return updated_count

    def quarantine(self, entries: Sequence[PendingAnalysisLog]) -> int:
        moved_count = 0
        for entry in entries:
            if self._quarantine_receipt(entry.receipt):
                moved_count += 1
        return moved_count

    def _list_pending(self) -> tuple[str, ...]:
        paths = sorted(
            self._log_directory.glob(
                f"{self._FILE_PREFIX}*{self._FILE_SUFFIX}",
            ),
        )

        return tuple(path.name for path in paths)

    def _read(self, reference: str) -> PendingAnalysisLog:
        path = self._entry_path(reference)

        with path.open("r", encoding="utf-8") as input_file:
            document = json.load(input_file)

        if not isinstance(document, dict):
            raise ValueError(
                "CDS 로그 파일은 JSON 객체여야 합니다.",
            )

        event_key = document.get("event_key")
        if not isinstance(event_key, str) or not event_key.strip():
            raise ValueError(
                "CDS 로그 파일에 이벤트 식별자가 없습니다.",
            )

        retry_count = document.get("retry_count", 0)
        if type(retry_count) is not int or retry_count < 0:
            raise ValueError(
                "CDS 로그 파일의 재시도 횟수가 올바르지 않습니다.",
            )

        payload = document.get("payload")
        if isinstance(payload, dict):
            # 배포 전에 쌓인 CDS payload도 현재 검증 규칙을 통과시킨다.
            prepared = self._mapper.toPersistence(
                MergedAnalysisLogDocument(payload),
            )
        else:
            # 리팩터링 중간 버전은 원본 분석 로그를 저장했다.
            analysis_log = document.get("analysis_log")
            if not isinstance(analysis_log, dict):
                raise ValueError(
                    "CDS 로그 파일에 분석 로그가 없습니다.",
                )
            prepared = self._mapper.toPersistence(
                MergedAnalysisLogDocument(analysis_log),
            )
        if not isinstance(prepared, dict):
            raise ValueError(
                "CDS 로그 payload 변환 결과가 올바르지 않습니다.",
            )

        return PendingAnalysisLog(
            event_key=event_key,
            receipt=reference,
            analysis_log=MergedAnalysisLogDocument(prepared),
            retry_count=retry_count,
        )

    def _quarantine_receipt(self, receipt: str) -> bool:
        source = self._entry_path(receipt)
        failed_directory = self._log_directory / self._FAILED_DIRECTORY_NAME
        try:
            failed_directory.mkdir(parents=True, exist_ok=True)
            os.replace(source, failed_directory / receipt)
            return True
        except Exception:
            self._logger.exception(
                "CDS 로그 격리 실패 receipt=%s",
                receipt,
            )
            return False

    def _replace_document(self, path: Path, document: dict) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._log_directory,
                prefix=".cds_retry#",
                suffix=".tmp",
                delete=False,
            ) as output_file:
                temporary_path = Path(output_file.name)
                json.dump(
                    document,
                    output_file,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                output_file.write("\n")
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _entry_path(self, reference: str) -> Path:
        if (
            not reference.startswith(self._FILE_PREFIX)
            or not reference.endswith(self._FILE_SUFFIX)
            or Path(reference).name != reference
        ):
            raise ValueError("CDS spool reference가 올바르지 않습니다.")

        return self._log_directory / reference
