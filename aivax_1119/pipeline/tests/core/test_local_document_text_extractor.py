"""MIME 타입별 공통 문서 텍스트 추출 테스트."""

import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

import src.core.file.local_document_text_extractor as extractor_module
from src.core.file import (
    FileMetadata,
    LocalDocumentTextExtractor,
    LocalFileMetadataReader,
)


def test_extracts_plain_text_file(tmp_path: Path) -> None:
    path = tmp_path / "document.txt"
    path.write_text("회의 내용", encoding="utf-8")

    text = LocalDocumentTextExtractor().extract(
        path,
        FileMetadata("text/plain", "txt", path.stat().st_size),
        30,
    )

    assert text == "회의 내용"


def test_routes_docx_to_docx_reader(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "document.docx"
    path.touch()
    monkeypatch.setattr(
        extractor_module.docx2txt,
        "process",
        lambda _: "docx contents",
    )

    text = LocalDocumentTextExtractor().extract(
        path,
        FileMetadata(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
            0,
        ),
        30,
    )

    assert text == "docx contents"


def test_routes_pdf_to_pdf_reader(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "document.pdf"
    path.touch()
    extractor = LocalDocumentTextExtractor()
    monkeypatch.setattr(extractor, "_read_pdf", lambda _: "pdf contents")

    text = extractor.extract(
        path,
        FileMetadata("application/pdf", "pdf", 0),
        30,
    )

    assert text == "pdf contents"


def test_extracts_webp_text_without_a_filename_extension(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "upload"
    with Image.new("RGB", (24, 16), color="white") as image:
        image.save(path, format="WEBP")
    ocr = Mock(return_value="contains secret")
    monkeypatch.setattr(extractor_module.pytesseract, "image_to_string", ocr)

    text = LocalDocumentTextExtractor().extract(
        path,
        LocalFileMetadataReader().read(path),
        30,
    )

    assert text == "contains secret"
    ocr.assert_called_once()
    assert ocr.call_args.kwargs == {"lang": "kor+eng", "timeout": 30}
    with ocr.call_args.args[0] as decoded_image:
        decoded_image.load()
        assert decoded_image.format == "WEBP"
        assert decoded_image.size == (24, 16)


def test_image_ocr_stops_at_read_timeout(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "upload.png"
    with Image.new("RGB", (24, 16), color="white") as image:
        image.save(path, format="PNG")
    stuck = tmp_path / "tesseract"
    stuck.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    stuck.chmod(0o755)
    monkeypatch.setattr(
        extractor_module.pytesseract.pytesseract, "tesseract_cmd", str(stuck)
    )

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timeout"):
        LocalDocumentTextExtractor().extract(
            path,
            LocalFileMetadataReader().read(path),
            1,
        )
    assert time.monotonic() - started < 10


def test_legacy_spreadsheet_conversion_uses_read_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "sheet.xls"
    path.touch()
    extractor = LocalDocumentTextExtractor()
    run_soffice = Mock(side_effect=RuntimeError("converted"))
    monkeypatch.setattr(extractor, "_run_soffice", run_soffice)

    with pytest.raises(RuntimeError, match="converted"):
        extractor.extract(path, FileMetadata("application/vnd.ms-excel", "xls", 0), 45)

    assert run_soffice.call_args.args[3] == 45


def test_octet_stream_is_not_opened_as_docx(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "upload"
    original = b"HWP Document File\x01\x02"
    path.write_bytes(original)
    opened = Mock(side_effect=AssertionError("docx reader"))
    monkeypatch.setattr(extractor_module.docx2txt, "process", opened)

    text = LocalDocumentTextExtractor().extract(
        path,
        FileMetadata("application/octet-stream", "unknown", path.stat().st_size),
        30,
    )

    assert text == ""
    opened.assert_not_called()
    assert path.read_bytes() == original


def test_hwp_conversion_uses_a_suffixed_copy(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "upload"
    original = b"HWP Document File\x01\x02"
    path.write_bytes(original)
    extractor = LocalDocumentTextExtractor()

    def convert(
        source: Path, directory: Path, output_format: str, timeout: int
    ) -> None:
        assert source.name == "upload.hwp"
        assert source.read_bytes() == original
        assert output_format == "pdf"
        assert timeout == 30
        (directory / "upload.pdf").write_bytes(b"%PDF-")

    monkeypatch.setattr(extractor, "_run_soffice", convert)

    pdf_path = extractor.convert_to_pdf(path, tmp_path / "out", 30, extension="hwp")

    assert pdf_path is not None and pdf_path.read_bytes() == b"%PDF-"
    assert path.read_bytes() == original
    assert not path.with_suffix(".hwp").exists()


def test_returns_empty_text_for_unsupported_mime_type(tmp_path: Path) -> None:
    path = tmp_path / "document.bin"
    path.touch()

    text = LocalDocumentTextExtractor().extract(
        path,
        FileMetadata("application/x-unknown", "unknown", 0),
        30,
    )

    assert text == ""


def test_hwp_falls_back_to_body_reader_when_hwp5txt_fails(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "document"
    path.write_bytes(b"not used")
    failed = Mock(returncode=1, stdout="", stderr="KeyError")
    monkeypatch.setattr(extractor_module.subprocess, "run", Mock(return_value=failed))
    body_reader = Mock(return_value="계좌번호 본문")
    monkeypatch.setattr(LocalDocumentTextExtractor, "_read_hwp_body", body_reader)

    text = LocalDocumentTextExtractor().extract(
        path, FileMetadata("application/x-hwp", "hwp", 8), 30
    )

    assert text == "계좌번호 본문"
    body_reader.assert_called_once_with(path)


def test_hwp_uses_body_reader_when_hwp5txt_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "document"
    path.write_bytes(b"not used")

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("hwp5txt")

    monkeypatch.setattr(extractor_module.subprocess, "run", missing)
    monkeypatch.setattr(
        LocalDocumentTextExtractor, "_read_hwp_body", Mock(return_value="본문")
    )

    text = LocalDocumentTextExtractor().extract(
        path, FileMetadata("application/x-hwp", "hwp", 8), 30
    )

    assert text == "본문"


def test_hwp_prefers_hwp5txt_output_when_it_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "document"
    path.write_bytes(b"not used")
    ok = Mock(returncode=0, stdout="cli text\n", stderr="")
    monkeypatch.setattr(extractor_module.subprocess, "run", Mock(return_value=ok))
    body_reader = Mock(return_value="should not be used")
    monkeypatch.setattr(LocalDocumentTextExtractor, "_read_hwp_body", body_reader)

    text = LocalDocumentTextExtractor().extract(
        path, FileMetadata("application/x-hwp", "hwp", 8), 30
    )

    assert text == "cli text\n"
    body_reader.assert_not_called()


def test_hwp_uses_preview_when_body_and_hwp5txt_fail(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "document"
    path.write_bytes(b"not used")
    failed = Mock(returncode=1, stdout="", stderr="truncated")
    monkeypatch.setattr(extractor_module.subprocess, "run", Mock(return_value=failed))
    monkeypatch.setattr(
        LocalDocumentTextExtractor,
        "_read_hwp_body",
        Mock(side_effect=RuntimeError("no body")),
    )
    preview = Mock(return_value="미리보기")
    monkeypatch.setattr(LocalDocumentTextExtractor, "_read_hwp_preview", preview)

    text = LocalDocumentTextExtractor().extract(
        path, FileMetadata("application/x-hwp", "hwp", 8), 30
    )

    assert text == "미리보기"
    preview.assert_called_once_with(path)


def test_hwp_preview_decodes_utf16(tmp_path: Path) -> None:
    path = tmp_path / "document"
    path.write_bytes(_preview_ole("미리보기".encode("utf-16-le")))

    assert LocalDocumentTextExtractor._read_hwp_preview(path) == "미리보기"


def _preview_ole(payload: bytes) -> bytes:
    import struct

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
    record = bytearray(128)
    encoded = "PrvText".encode("utf-16le")
    record[0 : len(encoded)] = encoded
    struct.pack_into("<H", record, 64, len(encoded) + 2)
    record[66] = 2
    record[67] = 1
    struct.pack_into("<i", record, 68, -1)
    struct.pack_into("<i", record, 72, -1)
    struct.pack_into("<i", record, 76, -1)
    struct.pack_into("<I", record, 116, 0)
    struct.pack_into("<Q", record, 120, len(payload))
    root = bytearray(128)
    root_name = "Root Entry".encode("utf-16le")
    root[0 : len(root_name)] = root_name
    struct.pack_into("<H", root, 64, len(root_name) + 2)
    root[66] = 5
    root[67] = 1
    struct.pack_into("<i", root, 68, -1)
    struct.pack_into("<i", root, 72, -1)
    struct.pack_into("<i", root, 76, 1)
    struct.pack_into("<I", root, 116, 3)
    struct.pack_into("<Q", root, 120, len(payload))
    directory = bytearray(512)
    directory[0:128] = root
    directory[128:256] = record
    mini_fat = bytearray(512)
    for index in range(128):
        struct.pack_into("<I", mini_fat, index * 4, free_sector)
    struct.pack_into("<I", mini_fat, 0, end_of_chain)
    mini_stream = bytearray(512)
    mini_stream[0 : len(payload)] = payload
    return bytes(header + fat + directory + mini_fat + mini_stream)
