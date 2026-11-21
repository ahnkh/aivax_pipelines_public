"""완성된 분석 로그에서 첨부파일 알림 이벤트를 생성한다."""

import logging
from pathlib import Path

from src.core.file import FileMetadata, FileMetadataReader, LocalFileMetadataReader
from src.core.time import TimeNormalizer
from src.domain.notification_attachment.models import AttachmentNotificationEvent
from src.domain.notification_attachment.ports import AttachmentNotificationOutboxPort


class AttachmentNotificationService:
    """백업된 파일을 찾아 domain event로 만들고 outbox에 적재한다."""

    _SHARED_BACKUP_ROOTS = (
        Path("/data/backup/attach_file_backup"),
        Path("/backup/attach_file_backup"),
    )

    def __init__(
        self,
        outbox: AttachmentNotificationOutboxPort,
        index_name: str,
        *,
        metadata_reader: FileMetadataReader | None = None,
        shared_backup_roots: tuple[str | Path, ...] = _SHARED_BACKUP_ROOTS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._outbox = outbox
        self._index_name = index_name.strip() or None
        self._metadata_reader = metadata_reader or LocalFileMetadataReader()
        self._shared_backup_roots = tuple(
            Path(root).resolve() for root in shared_backup_roots
        )
        self._logger = logger or logging.getLogger("khan-python")

    def enqueue_archived_file(
        self,
        *,
        message_id: str,
        file_name: str,
        file_backup_path: str,
        source_ip: str | None = None,
        occurred_at: str | None = None,
    ) -> bool:
        """저장 완료된 백업 파일 한 건을 하나의 동기화 이벤트로 적재한다."""
        try:
            backup_path, metadata = self._read_archived_file_metadata(
                file_backup_path,
            )
            # 시간대가 없는 과거 요청은 추정하지 않고 발생 시각을 비워 둔다.
            normalized_time = None
            if occurred_at:
                try:
                    normalized_time = TimeNormalizer.to_utc_iso(occurred_at)
                except (ValueError, TypeError):
                    pass
            self._outbox.enqueue(
                AttachmentNotificationEvent(
                    message_id=message_id,
                    source_ip=source_ip.strip() or None if source_ip else None,
                    occurred_at=normalized_time,
                    index_name=self._index_name,
                    file_name=file_name,
                    file_backup_path=backup_path,
                    file_size_bytes=metadata.size_bytes,
                    detected_mime_type=metadata.mime_type,
                )
            )
            return True
        except Exception:
            self._logger.exception(
                "첨부파일 알림 이벤트 생성 실패 "
                f"message_id={message_id} file_path={file_backup_path}",
            )
            return False

    def _read_archived_file_metadata(
        self,
        value: object,
    ) -> tuple[str, FileMetadata]:
        """공유 백업 루트에 실제 저장된 파일의 현재 메타데이터를 읽는다."""
        backup_path = Path(str(value)).resolve(strict=True)
        if not backup_path.is_file():
            raise ValueError("backup path is not a regular file")
        if not any(
            backup_path.is_relative_to(root) for root in self._shared_backup_roots
        ):
            raise ValueError("backup path is outside Management shared roots")
        return str(backup_path), self._metadata_reader.read(backup_path)
