"""로컬 파일의 MIME 타입, 확장자와 크기를 조회한다."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import filetype
import magic
import olefile


@dataclass(frozen=True, slots=True)
class FileMetadata:
    mime_type: str
    extension: str
    size_bytes: int


class FileMetadataReader(Protocol):
    def read(self, path: str | Path) -> FileMetadata:
        """파일의 메타데이터를 조회한다."""
        ...


class LocalFileMetadataReader(FileMetadataReader):
    """파일 내용과 파일 시스템 정보로 메타데이터를 조회한다."""

    _HWP_SIGNATURE = b"HWP Document File"
    _OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
    _ZIP_MAGIC = b"PK\x03\x04"
    _MARKUP_PREFIX_BYTES = 8192
    # libmagic이 한글·구형 오피스 문서에 MIME을 붙이지 못하면
    # octet-stream/CDFV2로 떨어진다. 이 값만으로 DOCX를 단정하면
    # HWP가 ZIP 파서로 열려 깨진다.
    _GENERIC_DOCUMENT_MIMES = frozenset(
        {
            "application/octet-stream",
            "application/CDFV2",
            "application/x-ole-storage",
            "application/vnd.ms-office",
        }
    )
    # 엑셀이 HTML·SpreadsheetML로 저장된 .xls는 text/html, text/xml,
    # octet-stream으로 남는다. 표식은 본문 앞부분의 ASCII라 인코딩과 무관하다.
    _EXCEL_MARKUP_MIMES = frozenset(
        {
            "text/html",
            "text/xml",
            "application/xml",
            "application/xhtml+xml",
        }
    )
    _EXCEL_MARKUP_MARKERS = (
        "urn:schemas-microsoft-com:office:excel",
        "urn:schemas-microsoft-com:office:spreadsheet",
        "application/vnd.ms-excel",
        'progid="excel.sheet"',
        "progid='excel.sheet'",
        "<x:excelworkbook",
    )
    _OLE_STREAM_MIMES = (
        ("Workbook", "application/vnd.ms-excel"),
        ("Book", "application/vnd.ms-excel"),
        ("WordDocument", "application/msword"),
        ("PowerPoint Document", "application/vnd.ms-powerpoint"),
    )
    _MIME_TO_EXTENSION = {
        "text/plain": "txt",
        "application/msword": "doc",
        (
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        ): "docx",
        "application/x-hwp": "hwp",
        "application/zip": "hwpx",
        "application/vnd.hancom.hwpx": "hwpx",
        "application/pdf": "pdf",
        "application/vnd.ms-powerpoint": "ppt",
        (
            "application/vnd.openxmlformats-officedocument"
            ".presentationml.presentation"
        ): "pptx",
        "application/vnd.ms-excel": "xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/bmp": "bmp",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/avif": "avif",
    }

    def read(self, path: str | Path) -> FileMetadata:
        file_path = Path(path)
        self._restore_truncated_ole(file_path)
        kind = filetype.guess(str(file_path))
        if kind and kind.mime.startswith(("image/", "video/", "audio/")):
            mime_type = kind.mime
        else:
            mime_type = str(magic.from_file(str(file_path), mime=True))
        mime_type = mime_type.split(";", 1)[0].strip()
        mime_type = self._refine_mime(file_path, mime_type)

        return FileMetadata(
            mime_type=mime_type,
            extension=self._MIME_TO_EXTENSION.get(mime_type, "unknown"),
            size_bytes=file_path.stat().st_size,
        )

    def _refine_mime(self, path: Path, mime_type: str) -> str:
        header = self._header(path)
        if header.startswith(self._HWP_SIGNATURE):
            return "application/x-hwp"
        if mime_type in self._GENERIC_DOCUMENT_MIMES and self._is_hwp5(path, header):
            return "application/x-hwp"
        if mime_type in self._GENERIC_DOCUMENT_MIMES:
            office_mime = self._sniff_ole_office(path, header)
            if office_mime:
                return office_mime
        if mime_type == "application/octet-stream":
            return (
                self._sniff_ooxml(path, header)
                or self._sniff_excel_markup(path, header)
                or mime_type
            )
        if mime_type in self._EXCEL_MARKUP_MIMES:
            return self._sniff_excel_markup(path, header) or mime_type
        return mime_type

    @staticmethod
    def _header(path: Path, size: int = 64) -> bytes:
        try:
            with path.open("rb") as file_object:
                return file_object.read(size)
        except OSError:
            return b""

    def _is_hwp5(self, path: Path, header: bytes) -> bool:
        if not header.startswith(self._OLE_MAGIC):
            return False
        try:
            if not olefile.isOleFile(str(path)):
                return False
            with olefile.OleFileIO(str(path)) as compound:
                signature = b""
                if compound.exists("FileHeader"):
                    signature = compound.openstream("FileHeader").read(
                        len(self._HWP_SIGNATURE)
                    )
                if signature == self._HWP_SIGNATURE:
                    return True
                # 잘린 OLE는 디렉터리만 남고 FileHeader 바이트는 파일 밖이다.
                short_header = len(signature) < len(self._HWP_SIGNATURE)
                return short_header and self._has_hwp_storage_names(compound)
        except Exception:
            return False
        return False

    @staticmethod
    def _has_hwp_storage_names(compound: olefile.OleFileIO) -> bool:
        """스트림을 읽지 못한 채 이름만 남은 HWP 디렉터리를 구분한다."""
        if compound.exists("\005HwpSummaryInformation"):
            return True
        return (
            compound.exists("FileHeader")
            and compound.exists("BodyText")
            and compound.exists("DocInfo")
        )

    def _restore_truncated_ole(self, path: Path) -> None:
        """OLE 매직 앞 3바이트 D0 CF 11이 잘린 복합 문서를 원래대로 되돌린다."""
        header = self._header(path, 8)
        if len(header) < 5 or header.startswith(self._OLE_MAGIC):
            return
        if not header.startswith(self._OLE_MAGIC[3:]):
            return
        try:
            repaired = self._OLE_MAGIC[:3] + path.read_bytes()
            with olefile.OleFileIO(io.BytesIO(repaired)) as compound:
                names = [name for name, _mime in self._OLE_STREAM_MIMES]
                names.append("FileHeader")
                if not any(compound.exists(name) for name in names):
                    return
        except Exception:
            return
        try:
            path.write_bytes(repaired)
        except OSError:
            return

    def _sniff_ole_office(self, path: Path, header: bytes) -> str | None:
        """일반 OLE MIME에서 Workbook·WordDocument 등 스트림 이름으로 구분한다."""
        if not header.startswith(self._OLE_MAGIC):
            return None
        try:
            if not olefile.isOleFile(str(path)):
                return None
            with olefile.OleFileIO(str(path)) as compound:
                for name, mime_type in self._OLE_STREAM_MIMES:
                    if compound.exists(name):
                        return mime_type
        except Exception:
            return None
        return None

    def _sniff_excel_markup(self, path: Path, header: bytes) -> str | None:
        """엑셀 HTML·SpreadsheetML은 .xls로 저장돼도 ZIP/OLE 시그니처가 없다."""
        if header.startswith((self._OLE_MAGIC, self._ZIP_MAGIC, self._HWP_SIGNATURE)):
            return None
        text = self._markup_prefix(path).casefold()
        if any(marker in text for marker in self._EXCEL_MARKUP_MARKERS):
            return "application/vnd.ms-excel"
        return None

    def _markup_prefix(self, path: Path) -> str:
        try:
            with path.open("rb") as file_object:
                raw = file_object.read(self._MARKUP_PREFIX_BYTES)
        except OSError:
            return ""
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        elif len(raw) >= 4 and raw[1] == 0 and raw[3] == 0:
            encoding = "utf-16-le"
        else:
            encoding = "utf-8"
        return raw.decode(encoding, errors="ignore")

    def _sniff_ooxml(self, path: Path, header: bytes) -> str | None:
        """매직이 ZIP 문서를 octet-stream으로만 줄 때 OOXML 내부 경로로 구분한다."""
        if not header.startswith(self._ZIP_MAGIC):
            return None
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return None
        if "word/document.xml" in names:
            return (
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document"
            )
        if "xl/workbook.xml" in names:
            return (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        if "ppt/presentation.xml" in names:
            return (
                "application/vnd.openxmlformats-officedocument"
                ".presentationml.presentation"
            )
        if any(
            name.startswith("Contents/section") and name.endswith(".xml")
            for name in names
        ):
            return "application/vnd.hancom.hwpx"
        return None


# 같은 형식의 확장자. 허용 목록은 둘 중 하나만 있어도 통과하고,
# 로그 표기는 파일명이 그 안의 값일 때만 파일명을 따른다.
_EQUIVALENT_EXTENSIONS = (frozenset({"jpg", "jpeg"}),)


def equivalent_extensions(extension: str) -> frozenset[str]:
    """확장자와 정책상 같은 형식인 확장자 묶음을 반환한다."""
    normalized = extension.strip().lower().lstrip(".")
    for group in _EQUIVALENT_EXTENSIONS:
        if normalized in group:
            return group
    if not normalized:
        return frozenset()
    return frozenset({normalized})


def extension_allowed(extension: str, allowed: Iterable[str]) -> bool:
    """허용 목록에 확장자 또는 같은 형식의 다른 표기가 있으면 통과한다."""
    aliases = equivalent_extensions(extension)
    if not aliases:
        return False
    allowed_names = {item.strip().lower().lstrip(".") for item in allowed}
    return bool(aliases & allowed_names)


def logged_extension(file_name: str, detected_extension: str) -> str:
    """내용 판정과 같은 형식이면 파일명 표기를 남기고, 아니면 판정값을 남긴다."""
    detected = detected_extension.strip().lower().lstrip(".")
    suffix = Path(file_name).suffix.lstrip(".").lower()
    if suffix and suffix in equivalent_extensions(detected):
        return suffix
    return detected
