"""첨부파일 알림 이벤트와 Valkey Stream field의 변환기."""

from src.core.ddd.mapper import Mapper
from src.domain.notification_attachment.models import AttachmentNotificationEvent

AttachmentStreamFields = dict[str, str | int]


class AttachmentNotificationMapper(
    Mapper[
        AttachmentNotificationEvent,
        AttachmentStreamFields,
        AttachmentNotificationEvent,
    ],
):
    """Management 파일 자산 Stream field 계약으로 직렬화한다."""

    def toPersistence(
        self,
        entity: AttachmentNotificationEvent,
    ) -> AttachmentStreamFields:
        fields: AttachmentStreamFields = {
            "messageId": entity.message_id,
            "fileName": entity.file_name,
            "fileBackupPath": entity.file_backup_path,
            "fileSizeBytes": entity.file_size_bytes,
            "detectedMimeType": entity.detected_mime_type,
        }
        if entity.index_name is not None:
            fields["indexName"] = entity.index_name
        if entity.source_ip is not None:
            fields["sourceIp"] = entity.source_ip
        if entity.occurred_at is not None:
            fields["occurredAt"] = entity.occurred_at
        return fields

    def toDomain(
        self,
        record: AttachmentStreamFields,
    ) -> AttachmentNotificationEvent:
        return AttachmentNotificationEvent(
            message_id=str(record["messageId"]),
            source_ip=str(record["sourceIp"]) if record.get("sourceIp") else None,
            occurred_at=str(record["occurredAt"]) if record.get("occurredAt") else None,
            index_name=str(record["indexName"]) if record.get("indexName") else None,
            file_name=str(record["fileName"]),
            file_backup_path=str(record["fileBackupPath"]),
            file_size_bytes=int(record["fileSizeBytes"]),
            detected_mime_type=str(record["detectedMimeType"]),
        )
