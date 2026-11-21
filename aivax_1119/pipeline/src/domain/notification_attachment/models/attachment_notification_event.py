"""백업된 첨부파일의 Management 알림 이벤트."""

from dataclasses import dataclass, field
from pathlib import Path

from src.core.ddd.domain_entity import DomainEntity
from src.core.hash import HashGenerator
from src.domain.notification_attachment.errors import AttachmentDomainError


@dataclass(frozen=True, slots=True)
class AttachmentNotificationEvent(DomainEntity):
    message_id: str
    index_name: str | None
    file_name: str
    file_backup_path: str
    file_size_bytes: int
    detected_mime_type: str
    source_ip: str | None = None
    occurred_at: str | None = None
    event_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.validate()
        object.__setattr__(self, "event_id", self._create_event_id())

    def validate(self) -> None:
        required_fields = {
            "message_id": self.message_id,
            "file_name": self.file_name,
            "file_backup_path": self.file_backup_path,
            "detected_mime_type": self.detected_mime_type,
        }
        invalid_fields = [
            name
            for name, value in required_fields.items()
            if not isinstance(value, str) or not value.strip()
        ]
        if invalid_fields:
            raise AttachmentDomainError.required_fields_missing(invalid_fields)
        if not isinstance(self.file_size_bytes, int) or self.file_size_bytes < 0:
            raise AttachmentDomainError.invalid_file_size(self.file_size_bytes)
        if not Path(self.file_backup_path).is_absolute():
            raise AttachmentDomainError.invalid_file_backup_path(
                self.file_backup_path,
            )

    def _create_event_id(self) -> str:
        """동일 백업 파일을 재시도해도 유지되는 중복 제거 ID를 반환한다."""
        return HashGenerator.blake2s(
            {
                "detectedMimeType": self.detected_mime_type,
                "fileBackupPath": self.file_backup_path,
                "fileName": self.file_name,
                "fileSizeBytes": self.file_size_bytes,
                "indexName": self.index_name or "",
                "messageId": self.message_id,
            }
        )
