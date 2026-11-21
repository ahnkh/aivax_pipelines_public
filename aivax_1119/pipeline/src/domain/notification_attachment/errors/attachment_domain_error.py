"""첨부파일 도메인 예외."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from src.core.ddd.domain_exception import DomainException


AttachmentDomainErrorCode = Literal[
    "required-fields-missing",
    "invalid-file-size",
    "invalid-file-backup-path",
]


class AttachmentDomainError(DomainException):
    """첨부파일 도메인의 불변식 위반을 나타낸다."""

    def __init__(
        self,
        code: AttachmentDomainErrorCode,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("attachment", code, message, details)

    @classmethod
    def required_fields_missing(cls, fields: list[str]) -> "AttachmentDomainError":
        return cls(
            "required-fields-missing",
            f"첨부파일 이벤트 필수 값이 비어 있습니다: {', '.join(fields)}",
            {"fields": fields},
        )

    @classmethod
    def invalid_file_size(cls, size: object) -> "AttachmentDomainError":
        return cls(
            "invalid-file-size",
            "file_size_bytes는 0 이상의 정수여야 합니다.",
            {"fileSizeBytes": size},
        )

    @classmethod
    def invalid_file_backup_path(
        cls,
        path: object,
    ) -> "AttachmentDomainError":
        return cls(
            "invalid-file-backup-path",
            "file_backup_path는 절대 경로여야 합니다.",
            {"fileBackupPath": path},
        )
