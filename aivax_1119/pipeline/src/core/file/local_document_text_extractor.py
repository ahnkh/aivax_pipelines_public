"""문서와 이미지 형식별 로컬 텍스트 추출 기능."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import zipfile
from io import BytesIO, StringIO
from pathlib import Path
from typing import Protocol
from xml.etree.ElementTree import iterparse

import docx2txt
import fitz
import pandas as pd
import pytesseract
from openpyxl import load_workbook
from PIL import Image
from pptx import Presentation

from src.core.file.file_metadata import FileMetadata

"""
문서별로 고도화가 필요하다면 그 문서 확장자만 지원하는 extractor로 따로 뺄 것.
하지만 분석 로직같은 건 넣으면 안됌. 단순 추출기능만.
"""


class DocumentTextExtractor(Protocol):
    def extract(
        self,
        path: str | Path,
        metadata: FileMetadata,
        timeout_seconds: int,
    ) -> str:
        """파일에서 텍스트를 추출한다."""
        ...

    def convert_to_pdf(
        self,
        path: str | Path,
        directory: str | Path,
        timeout_seconds: int,
        extension: str = "",
    ) -> Path | None:
        """파일을 PDF로 변환한다."""
        ...


class LocalDocumentTextExtractor(DocumentTextExtractor):
    """MIME 타입에 맞는 로컬 도구로 파일의 텍스트를 추출한다."""

    _LOGGER = logging.getLogger("khan-python")
    _IMAGE_MIME_TYPES = {
        "image/png",
        "image/jpeg",
        "image/bmp",
        "image/gif",
        "image/webp",
    }
    _SOFFICE_EXTENSIONS = frozenset(
        {"doc", "docx", "hwp", "hwpx", "ppt", "pptx", "xls", "xlsx"}
    )

    def extract(
        self,
        path: str | Path,
        metadata: FileMetadata,
        timeout_seconds: int,
    ) -> str:
        file_path = Path(path)
        mime_type = metadata.mime_type
        if (
            mime_type
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ):
            return str(docx2txt.process(file_path) or "")
        if mime_type == "application/msword":
            return self._convert_to_text(file_path, timeout_seconds)
        if mime_type == "application/x-hwp":
            return self._read_hwp(file_path, timeout_seconds)
        if mime_type in {"application/zip", "application/vnd.hancom.hwpx"}:
            return self._read_hwpx(file_path)
        if mime_type == "application/pdf":
            return self._read_pdf(file_path)
        if mime_type == "application/vnd.ms-powerpoint":
            return self._read_legacy_presentation(file_path, timeout_seconds)
        if (
            mime_type
            == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ):
            return self._read_presentation(file_path)
        if mime_type == "application/vnd.ms-excel":
            return self._read_legacy_spreadsheet(file_path, timeout_seconds)
        if (
            mime_type
            == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ):
            return self._read_spreadsheet(file_path)
        if mime_type in self._IMAGE_MIME_TYPES:
            return self._read_image(
                file_path, convert_rgb=False, timeout_seconds=timeout_seconds
            )
        if mime_type == "image/avif":
            return self._read_image(
                file_path, convert_rgb=True, timeout_seconds=timeout_seconds
            )
        if mime_type == "text/plain":
            return file_path.read_text(encoding="utf-8", errors="ignore")

        self._LOGGER.error("unsupported file type %s", mime_type)
        return ""

    def convert_to_pdf(
        self,
        path: str | Path,
        directory: str | Path,
        timeout_seconds: int,
        extension: str = "",
    ) -> Path | None:
        file_path = Path(path)
        directory_path = Path(directory)
        source = self._soffice_source(file_path, directory_path, extension)
        try:
            self._run_soffice(source, directory_path, "pdf", timeout_seconds)
        except Exception:
            self._LOGGER.exception("fail convert document to PDF: %s", file_path)
            return None
        pdf_path = directory_path / f"{source.stem}.pdf"
        return pdf_path if pdf_path.exists() else None

    def _soffice_source(self, path: Path, directory: Path, extension: str) -> Path:
        """LibreOffice는 확장자로 가져오기 필터를 고른다.

        업로드 저장명은 확장자가 없다. HWP를 그 경로 그대로 넘기면 Word 필터로
        열려 변환 결과가 깨지므로, 판별된 확장자를 붙인 사본을 변환한다.
        """
        normalized = extension.lower().lstrip(".")
        if normalized not in self._SOFFICE_EXTENSIONS:
            return path
        if path.suffix.lower() == f".{normalized}":
            return path
        directory.mkdir(parents=True, exist_ok=True)
        copy_path = directory / f"{path.name}.{normalized}"
        shutil.copyfile(path, copy_path)
        return copy_path

    def _convert_to_text(self, path: Path, timeout_seconds: int) -> str:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            self._run_soffice(path, directory, "txt", timeout_seconds)
            return (directory / f"{path.stem}.txt").read_text(
                encoding="utf-8",
                errors="ignore",
            )

    @classmethod
    def _read_hwp(cls, path: Path, timeout_seconds: int) -> str:
        try:
            process = subprocess.run(
                ["hwp5txt", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            try:
                return cls._read_hwp_fallback(path)
            except Exception:
                raise RuntimeError(
                    "hwp5txt not found and HWP body read failed"
                ) from exc
        if process.returncode == 0 and process.stdout.strip():
            return process.stdout
        # hwp5txt는 요약 스트림이 없거나 본문 섹터가 잘리면 실패한다.
        # 본문 레코드, 그것도 없으면 파일 앞에 남은 미리보기를 읽는다.
        try:
            return cls._read_hwp_fallback(path)
        except Exception as fallback_exc:
            if process.returncode != 0:
                raise RuntimeError(
                    f"hwp5txt failed: {process.stderr}"
                ) from fallback_exc
            raise RuntimeError("hwp5txt produced empty output") from fallback_exc

    @classmethod
    def _read_hwp_fallback(cls, path: Path) -> str:
        try:
            return cls._read_hwp_body(path)
        except Exception:
            return cls._read_hwp_preview(path)

    @staticmethod
    def _read_hwp_body(path: Path) -> str:
        from hwp5.binmodel import Hwp5File

        document = Hwp5File(str(path))
        try:
            sections = (
                document.viewtext
                if document.header.flags.distributable
                else document.bodytext
            )
            paragraphs: list[str] = []
            for name in sections:
                for model in sections[name].models():
                    if model.get("tagname") != "HWPTAG_PARA_TEXT":
                        continue
                    chunks = model["content"]["chunks"]
                    paragraphs.append(
                        "".join(value for _, value in chunks if isinstance(value, str))
                    )
        finally:
            document.close()
        text = "\n".join(paragraphs).strip()
        if not text:
            raise RuntimeError("HWP body contains no text")
        return text

    @staticmethod
    def _read_hwp_preview(path: Path) -> str:
        import olefile

        with olefile.OleFileIO(str(path)) as compound:
            if not compound.exists("PrvText"):
                raise RuntimeError("HWP preview text is missing")
            raw = compound.openstream("PrvText").read()
        if raw.startswith(b"\xff\xfe"):
            raw = raw[2:]
        if len(raw) % 2:
            raw = raw[:-1]
        text = raw.decode("utf-16-le").replace("\x00", "").strip()
        if not text:
            raise RuntimeError("HWP preview text is empty")
        return text

    @staticmethod
    def _read_hwpx(path: Path) -> str:
        texts: list[str] = []
        with zipfile.ZipFile(path) as archive:
            section_files = sorted(
                name
                for name in archive.namelist()
                if name.startswith("Contents/section") and name.endswith(".xml")
            )
            for section_file in section_files:
                with archive.open(section_file) as file_object:
                    for _, element in iterparse(file_object):
                        if element.text:
                            texts.append(element.text)
                        element.clear()
        return "\n".join(texts)

    @staticmethod
    def _read_pdf(path: Path) -> str:
        texts: list[str] = []
        with fitz.open(path) as document:
            for page in document:
                extracted = page.get_text("text")
                text = extracted.strip() if isinstance(extracted, str) else ""
                if text:
                    texts.append(text)
        return "\n".join(texts)

    def _read_legacy_presentation(self, path: Path, timeout_seconds: int) -> str:
        with tempfile.TemporaryDirectory(prefix="ppt_conv_") as temporary_directory:
            pdf_path = self.convert_to_pdf(path, temporary_directory, timeout_seconds)
            return self._read_pdf(pdf_path) if pdf_path is not None else ""

    @staticmethod
    def _read_presentation(path: Path) -> str:
        texts: list[str] = []
        presentation = Presentation(str(path))
        for slide in presentation.slides:
            for shape in slide.shapes:
                text_frame = getattr(shape, "text_frame", None)
                if text_frame is None:
                    continue
                texts.extend(
                    paragraph.text.strip()
                    for paragraph in text_frame.paragraphs
                    if paragraph.text.strip()
                )
        return "\n".join(texts)

    def _read_legacy_spreadsheet(self, path: Path, timeout_seconds: int) -> str:
        with tempfile.TemporaryDirectory(prefix="xls_conv_") as temporary_directory:
            directory = Path(temporary_directory)
            self._run_soffice(path, directory, "xlsx", timeout_seconds)
            dataframe = pd.read_excel(
                directory / f"{path.stem}.xlsx",
                dtype=str,
            ).fillna("")
            buffer = StringIO()
            dataframe.to_csv(buffer, sep="\t", index=False)
            return buffer.getvalue()

    @staticmethod
    def _read_spreadsheet(path: Path) -> str:
        with path.open("rb") as file_object:
            workbook = load_workbook(
                BytesIO(file_object.read()),
                read_only=True,
                data_only=True,
            )
        try:
            return "\n".join(
                str(cell)
                for worksheet in workbook.worksheets
                for row in worksheet.iter_rows(values_only=True)
                for cell in row
                if cell is not None
            )
        finally:
            workbook.close()

    @staticmethod
    def _read_image(path: Path, *, convert_rgb: bool, timeout_seconds: int) -> str:
        image = Image.open(path)
        if convert_rgb:
            image = image.convert("RGB")
        # 끝나지 않는 tesseract가 파일 검사 슬롯을 점유하면 첨부 없는 프롬프트까지 막힌다.
        return pytesseract.image_to_string(
            image, lang="kor+eng", timeout=max(1, int(timeout_seconds))
        )

    @staticmethod
    def _run_soffice(
        path: Path,
        directory: Path,
        output_format: str,
        timeout_seconds: int | None,
    ) -> None:
        subprocess.run(
            [
                "soffice",
                "--headless",
                "--nologo",
                "--nolockcheck",
                "--nodefault",
                "--nofirststartwizard",
                "--norestore",
                "--convert-to",
                output_format,
                "--outdir",
                str(directory),
                str(path.resolve()),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
