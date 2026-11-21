"""공통 파일 메타데이터 조회 테스트."""

import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.core.file.file_metadata as file_metadata_module
from src.core.file import FileMetadata, LocalFileMetadataReader
from src.core.file.file_metadata import extension_allowed, logged_extension


def test_reads_media_mime_extension_and_size(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "upload"
    path.write_bytes(b"image")
    monkeypatch.setattr(
        file_metadata_module.filetype,
        "guess",
        lambda _: SimpleNamespace(mime="image/png"),
    )

    assert LocalFileMetadataReader().read(path) == FileMetadata(
        mime_type="image/png",
        extension="png",
        size_bytes=5,
    )


def test_webp_is_an_allowed_extension_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "upload"
    path.write_bytes(b"RIFF")
    monkeypatch.setattr(
        file_metadata_module.filetype,
        "guess",
        lambda _: SimpleNamespace(mime="image/webp"),
    )

    assert LocalFileMetadataReader().read(path).extension == "webp"


def test_uses_libmagic_for_non_media_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "upload"
    path.write_bytes(b"%PDF-")
    monkeypatch.setattr(file_metadata_module.filetype, "guess", lambda _: None)
    monkeypatch.setattr(
        file_metadata_module.magic,
        "from_file",
        lambda *_args, **_kwargs: "application/pdf",
    )

    assert LocalFileMetadataReader().read(path) == FileMetadata(
        mime_type="application/pdf",
        extension="pdf",
        size_bytes=5,
    )


def test_hwp3_octet_stream_is_classified_as_hwp(tmp_path: Path) -> None:
    path = tmp_path / "upload"
    original = b"HWP Document File" + bytes([1, 2, 3, 255, 0]) + b"\x00" * 32
    path.write_bytes(original)

    metadata = LocalFileMetadataReader().read(path)

    assert metadata.mime_type == "application/x-hwp"
    assert metadata.extension == "hwp"
    assert path.read_bytes() == original


def test_hwp5_stays_hwp_when_magic_reports_octet_stream(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "upload"
    path.write_bytes(_hwp5_bytes())
    monkeypatch.setattr(file_metadata_module.filetype, "guess", lambda _: None)
    monkeypatch.setattr(
        file_metadata_module.magic,
        "from_file",
        lambda *_args, **_kwargs: "application/octet-stream",
    )

    assert LocalFileMetadataReader().read(path) == FileMetadata(
        mime_type="application/x-hwp",
        extension="hwp",
        size_bytes=path.stat().st_size,
    )


def test_unknown_octet_stream_is_not_docx(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "upload"
    path.write_bytes(b"\x00\x01not-a-document")
    monkeypatch.setattr(file_metadata_module.filetype, "guess", lambda _: None)
    monkeypatch.setattr(
        file_metadata_module.magic,
        "from_file",
        lambda *_args, **_kwargs: "application/octet-stream",
    )

    assert LocalFileMetadataReader().read(path) == FileMetadata(
        mime_type="application/octet-stream",
        extension="unknown",
        size_bytes=path.stat().st_size,
    )


def test_octet_stream_ooxml_zip_is_docx(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "upload"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    monkeypatch.setattr(file_metadata_module.filetype, "guess", lambda _: None)
    monkeypatch.setattr(
        file_metadata_module.magic,
        "from_file",
        lambda *_args, **_kwargs: "application/octet-stream",
    )

    metadata = LocalFileMetadataReader().read(path)

    assert metadata.extension == "docx"
    assert (
        metadata.mime_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


@pytest.mark.parametrize(
    ("stream_name", "mime_type", "extension"),
    [
        ("Workbook", "application/vnd.ms-excel", "xls"),
        ("Book", "application/vnd.ms-excel", "xls"),
        ("WordDocument", "application/msword", "doc"),
        ("PowerPoint Document", "application/vnd.ms-powerpoint", "ppt"),
    ],
)
@pytest.mark.parametrize(
    "reported_mime",
    [
        "application/octet-stream",
        "application/CDFV2",
        "application/vnd.ms-office",
    ],
)
def test_generic_ole_office_stream_is_classified(
    tmp_path: Path,
    monkeypatch,
    stream_name: str,
    mime_type: str,
    extension: str,
    reported_mime: str,
) -> None:
    path = tmp_path / "upload"
    path.write_bytes(_ole_bytes(stream_name, b"office-stream"))
    _mock_mime(monkeypatch, reported_mime)

    assert LocalFileMetadataReader().read(path) == FileMetadata(
        mime_type=mime_type,
        extension=extension,
        size_bytes=path.stat().st_size,
    )


def test_generic_ole_without_office_stream_stays_unknown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "upload"
    path.write_bytes(_ole_bytes("Contents", b"not-office"))
    _mock_mime(monkeypatch, "application/octet-stream")

    metadata = LocalFileMetadataReader().read(path)

    assert metadata.mime_type == "application/octet-stream"
    assert metadata.extension == "unknown"


@pytest.mark.parametrize(
    ("reported_mime", "payload"),
    [
        (
            "application/octet-stream",
            '<html xmlns:x="urn:schemas-microsoft-com:office:excel">'
            "<body><table><tr><td>1</td></tr></table></body></html>",
        ),
        (
            "text/html",
            '<html xmlns:x="urn:schemas-microsoft-com:office:excel">'
            "<body><table><tr><td>1</td></tr></table></body></html>",
        ),
        (
            "text/xml",
            '<?xml version="1.0"?>'
            '<?mso-application progid="Excel.Sheet"?>'
            '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"/>',
        ),
    ],
)
def test_excel_markup_saved_as_generic_mime_is_xls(
    tmp_path: Path,
    monkeypatch,
    reported_mime: str,
    payload: str,
) -> None:
    path = tmp_path / "upload"
    path.write_bytes(payload.encode("utf-16"))
    _mock_mime(monkeypatch, reported_mime)

    assert LocalFileMetadataReader().read(path) == FileMetadata(
        mime_type="application/vnd.ms-excel",
        extension="xls",
        size_bytes=path.stat().st_size,
    )


def test_real_excel_html_and_spreadsheetml_are_xls(tmp_path: Path) -> None:
    reader = LocalFileMetadataReader()
    html_path = tmp_path / "html"
    html_path.write_bytes(
        (
            '<html xmlns:x="urn:schemas-microsoft-com:office:excel">'
            "<body><table><tr><td>부동산 고유번호</td></tr></table></body></html>"
        ).encode("utf-16")
    )
    xml_path = tmp_path / "xml"
    xml_path.write_text(
        '<?xml version="1.0"?>'
        '<?mso-application progid="Excel.Sheet"?>'
        '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"/>',
        encoding="utf-8",
    )

    assert reader.read(html_path).extension == "xls"
    assert reader.read(xml_path).extension == "xls"


def test_truncated_ole_magic_is_restored_as_xls(tmp_path: Path) -> None:
    path = tmp_path / "upload"
    original = _ole_bytes("Workbook", b"excel-bytes")
    path.write_bytes(original[3:])

    metadata = LocalFileMetadataReader().read(path)

    assert path.read_bytes() == original
    assert metadata.extension == "xls"
    assert metadata.size_bytes == len(original)


def test_ole_prefix_without_compound_file_is_not_rewritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "upload"
    blob = bytes.fromhex("E0A1B11AE1") + b"not-an-ole-document"
    path.write_bytes(blob)
    _mock_mime(monkeypatch, "application/octet-stream")

    metadata = LocalFileMetadataReader().read(path)

    assert path.read_bytes() == blob
    assert metadata.extension == "unknown"


@pytest.mark.parametrize(
    ("extension", "allowed", "accepted"),
    [
        ("jpeg", ("jpg",), True),
        ("jpeg", ("jpeg",), True),
        ("jpeg", ("JPG",), True),
        ("jpg", ("jpeg",), True),
        ("jpeg", ("jpg", "png"), True),
        ("jpeg", ("png",), False),
        ("png", ("jpg",), False),
        ("webp", ("webp",), True),
        ("xls", ("xlsx",), False),
    ],
)
def test_jpg_and_jpeg_share_one_allowlist_entry(
    extension: str,
    allowed: tuple[str, ...],
    accepted: bool,
) -> None:
    assert extension_allowed(extension, allowed) is accepted


@pytest.mark.parametrize(
    ("file_name", "detected", "logged"),
    [
        ("계좌번호 - 신한은행.jpg", "jpeg", "jpg"),
        ("photo.JPG", "jpeg", "jpg"),
        ("photo.jpeg", "jpeg", "jpeg"),
        ("photo.png", "jpeg", "jpeg"),
        ("photo", "jpeg", "jpeg"),
        ("sheet.xls", "xls", "xls"),
        ("fake.jpg", "png", "png"),
    ],
)
def test_logged_extension_keeps_filename_only_for_the_same_format(
    file_name: str,
    detected: str,
    logged: str,
) -> None:
    assert logged_extension(file_name, detected) == logged


def test_truncated_hwp_directory_is_classified_as_hwp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "upload"
    path.write_bytes(_truncated_hwp_directory())
    _mock_mime(monkeypatch, "application/CDFV2")

    metadata = LocalFileMetadataReader().read(path)

    assert metadata.mime_type == "application/x-hwp"
    assert metadata.extension == "hwp"


def test_plain_html_is_not_xls(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "upload"
    path.write_text("<html><body><table><tr><td>1</td></tr></table></body></html>")
    _mock_mime(monkeypatch, "text/html")

    metadata = LocalFileMetadataReader().read(path)

    assert metadata.mime_type == "text/html"
    assert metadata.extension == "unknown"


def _mock_mime(monkeypatch: pytest.MonkeyPatch, mime_type: str) -> None:
    monkeypatch.setattr(file_metadata_module.filetype, "guess", lambda _: None)
    monkeypatch.setattr(
        file_metadata_module.magic,
        "from_file",
        lambda *_args, **_kwargs: mime_type,
    )


def _hwp5_bytes() -> bytes:
    payload = b"HWP Document File" + b"\x00" * (64 - 17)
    return _ole_bytes("FileHeader", payload)


def _ole_bytes(stream_name: str, payload: bytes) -> bytes:
    end_of_chain = 0xFFFFFFFE
    free_sector = 0xFFFFFFFF
    fat_sector = 0xFFFFFFFD
    header = bytearray(512)
    header[0:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<H", header, 0x18, 0x003E)
    struct.pack_into("<H", header, 0x1A, 3)
    struct.pack_into("<H", header, 0x1C, 0xFFFE)
    struct.pack_into("<H", header, 0x1E, 9)
    struct.pack_into("<H", header, 0x20, 6)
    struct.pack_into("<I", header, 0x2C, 1)
    struct.pack_into("<I", header, 0x30, 1)
    struct.pack_into("<I", header, 0x38, 4096)
    struct.pack_into("<I", header, 0x3C, 2)
    struct.pack_into("<I", header, 0x40, 1)
    struct.pack_into("<I", header, 0x44, end_of_chain)
    for index in range(109):
        struct.pack_into("<I", header, 0x4C + 4 * index, free_sector)
    struct.pack_into("<I", header, 0x4C, 0)
    fat = bytearray(512)
    for index in range(128):
        struct.pack_into("<I", fat, index * 4, free_sector)
    struct.pack_into("<I", fat, 0, fat_sector)
    struct.pack_into("<I", fat, 4, end_of_chain)
    struct.pack_into("<I", fat, 8, end_of_chain)
    struct.pack_into("<I", fat, 12, end_of_chain)

    def entry(
        name: str,
        entry_type: int,
        start: int,
        size: int,
        child: int = -1,
    ) -> bytes:
        record = bytearray(128)
        encoded = name.encode("utf-16le")
        record[0 : len(encoded)] = encoded
        struct.pack_into("<H", record, 64, len(encoded) + 2)
        record[66] = entry_type
        record[67] = 1
        struct.pack_into("<i", record, 68, -1)
        struct.pack_into("<i", record, 72, -1)
        struct.pack_into("<i", record, 76, child)
        struct.pack_into("<I", record, 116, start)
        struct.pack_into("<Q", record, 120, size)
        return bytes(record)

    directory = bytearray(512)
    directory[0:128] = entry("Root Entry", 5, 3, len(payload), child=1)
    directory[128:256] = entry(stream_name, 2, 0, len(payload))
    mini_fat = bytearray(512)
    for index in range(128):
        struct.pack_into("<I", mini_fat, index * 4, free_sector)
    struct.pack_into("<I", mini_fat, 0, end_of_chain)
    mini_stream = bytearray(512)
    mini_stream[0 : len(payload)] = payload
    return bytes(header + fat + directory + mini_fat + mini_stream)


def _truncated_hwp_directory() -> bytes:
    """FileHeader 데이터가 파일 밖에 있고 디렉터리 이름만 남은 HWP."""
    end_of_chain = 0xFFFFFFFE
    free_sector = 0xFFFFFFFF
    fat_sector = 0xFFFFFFFD
    header = bytearray(512)
    header[0:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<H", header, 0x18, 0x003E)
    struct.pack_into("<H", header, 0x1A, 3)
    struct.pack_into("<H", header, 0x1C, 0xFFFE)
    struct.pack_into("<H", header, 0x1E, 9)
    struct.pack_into("<H", header, 0x20, 6)
    struct.pack_into("<I", header, 0x2C, 1)
    struct.pack_into("<I", header, 0x30, 1)
    struct.pack_into("<I", header, 0x38, 4096)
    struct.pack_into("<I", header, 0x3C, 2)
    struct.pack_into("<I", header, 0x40, 1)
    struct.pack_into("<I", header, 0x44, end_of_chain)
    for index in range(109):
        struct.pack_into("<I", header, 0x4C + 4 * index, free_sector)
    struct.pack_into("<I", header, 0x4C, 0)
    fat = bytearray(512)
    for index in range(128):
        struct.pack_into("<I", fat, index * 4, free_sector)
    struct.pack_into("<I", fat, 0, fat_sector)
    struct.pack_into("<I", fat, 4, end_of_chain)
    struct.pack_into("<I", fat, 8, end_of_chain)
    struct.pack_into("<I", fat, 12, end_of_chain)

    def entry(
        name: str,
        entry_type: int,
        start: int,
        size: int,
        left: int = -1,
        right: int = -1,
        child: int = -1,
    ) -> bytes:
        record = bytearray(128)
        encoded = name.encode("utf-16le")
        record[0 : len(encoded)] = encoded
        struct.pack_into("<H", record, 64, len(encoded) + 2)
        record[66] = entry_type
        record[67] = 1
        struct.pack_into("<i", record, 68, left)
        struct.pack_into("<i", record, 72, right)
        struct.pack_into("<i", record, 76, child)
        struct.pack_into("<I", record, 116, start)
        struct.pack_into("<Q", record, 120, size)
        return bytes(record)

    directory = bytearray(512)
    directory[0:128] = entry("Root Entry", 5, 3, 11456, child=2)
    directory[128:256] = entry("FileHeader", 2, 117, 256)
    directory[256:384] = entry("DocInfo", 2, 68, 100, left=3, right=1)
    directory[384:512] = entry("BodyText", 1, 0, 0)
    mini_fat = bytearray(512)
    for index in range(128):
        struct.pack_into("<I", mini_fat, index * 4, free_sector)
    return bytes(header + fat + directory + mini_fat + bytearray(64))
