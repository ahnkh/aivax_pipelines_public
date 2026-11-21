"""실제 참조 판독기와 교체 가능한 어댑터 계약을 검증한다."""

import asyncio
import builtins
import logging
import struct
import threading
from unittest.mock import Mock
from zipfile import ZipFile

import pikepdf
import pytest

from src.bootstrap.env_loader import ConfigurationError, EnvironmentLoader
from src.bootstrap.pipeline_bootstrap import PipelineBootstrap
from src.domain.inspection_input.models.cso_grade_result import (
    CsoGradeResult,
    CsoGradeStatus,
)
from src.extensions.cso import markany_grade_adapter
from src.extensions.cso.markany_grade_adapter import MarkAnyCsoGradeAdapter


def write_docx(path, level):
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        if level is not None:
            archive.writestr(
                "docProps/custom.xml",
                f'<property name="N2SF_Level"><vt:lpwstr>{level}'
                "</vt:lpwstr></property>",
            )
    return path


@pytest.mark.parametrize(
    ("level", "grade", "status"),
    [
        ("C", 1, CsoGradeStatus.TAGGED),
        ("S", 2, CsoGradeStatus.TAGGED),
        ("O", 3, CsoGradeStatus.TAGGED),
        (None, None, CsoGradeStatus.UNTAGGED),
        ("", None, CsoGradeStatus.UNTAGGED),
        ("o", None, CsoGradeStatus.ERROR),
        (" O ", None, CsoGradeStatus.ERROR),
        ("X", None, CsoGradeStatus.ERROR),
    ],
)
def test_reads_real_zip_tags_with_exact_grade_values(tmp_path, level, grade, status):
    path = write_docx(tmp_path / "document.docx", level)
    results = asyncio.run(MarkAnyCsoGradeAdapter().read_grades((str(path),)))
    assert results == (CsoGradeResult(grade, status),)


@pytest.mark.parametrize(
    ("part", "xml"),
    [
        ("Contents/content.hpf", '<opf:meta name="N2SF_Level">O</opf:meta>'),
        (
            "meta.xml",
            '<office:document-meta><meta:user-defined meta:name="N2SF_Level">'
            "O</meta:user-defined></office:document-meta>",
        ),
    ],
)
def test_reads_hwpx_and_odf_without_an_extension(tmp_path, part, xml):
    path = tmp_path / "document"
    with ZipFile(path, "w") as archive:
        archive.writestr(part, xml)
    assert asyncio.run(MarkAnyCsoGradeAdapter().read_grades((str(path),))) == (
        CsoGradeResult(3, CsoGradeStatus.TAGGED),
    )


def test_keeps_order_and_continues_after_parse_and_file_errors(tmp_path):
    good = write_docx(tmp_path / "good.docx", "O")
    corrupt = write_docx(tmp_path / "corrupt.docx", "C")
    data = bytearray(corrupt.read_bytes())
    offset = data.find(b"PK\x03\x04", 4)
    name_length, extra_length = struct.unpack_from("<HH", data, offset + 26)
    data[offset + 30 + name_length + extra_length] ^= 1
    corrupt.write_bytes(data)
    unsupported = tmp_path / "plain.txt"
    unsupported.write_text("plain text")
    paths = (corrupt, tmp_path / "missing.docx", unsupported, good)
    results = asyncio.run(
        MarkAnyCsoGradeAdapter().read_grades(tuple(str(p) for p in paths))
    )
    assert results == (
        CsoGradeResult(None, CsoGradeStatus.ERROR),
        CsoGradeResult(None, CsoGradeStatus.ERROR),
        CsoGradeResult(None, CsoGradeStatus.ERROR, unsupported=True),
        CsoGradeResult(3, CsoGradeStatus.TAGGED),
    )


def test_error_state_wins_over_partial_tags_and_runs_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()

    def read(path):
        assert threading.get_ident() != loop_thread
        return {"status": "ERROR", "tags": {"N2SF_Level": "O"}}

    monkeypatch.setattr(markany_grade_adapter, "read_tags", read)
    assert asyncio.run(MarkAnyCsoGradeAdapter().read_grades(("document",))) == (
        CsoGradeResult(None, CsoGradeStatus.ERROR),
    )


@pytest.mark.parametrize("encrypted", [False, True])
def test_reads_metadata_from_real_plain_and_encrypted_pdfs(tmp_path, encrypted):
    path = tmp_path / "document.pdf"
    xml = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:n2sf="http://markany.com/n2sf/1.0/">'
        b"<n2sf:Level>O</n2sf:Level></rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    with pikepdf.new() as pdf:
        pdf.Root.Metadata = pdf.make_stream(xml)
        pdf.Root.Metadata.Type = pikepdf.Name.Metadata
        pdf.Root.Metadata.Subtype = pikepdf.Name.XML
        options = {}
        if encrypted:
            options["encryption"] = pikepdf.Encryption(owner="owner", user="", R=6)
        pdf.save(path, compress_streams=False, **options)
    assert asyncio.run(MarkAnyCsoGradeAdapter().read_grades((str(path),))) == (
        CsoGradeResult(3, CsoGradeStatus.TAGGED),
    )


def test_hwpml_and_password_pdf_are_not_unsupported(tmp_path):
    hml = tmp_path / "document.hwpx"
    hml.write_text("<HWPML></TAIL><N2SF><N2SF_Level>S</N2SF_Level></N2SF></HWPML>")
    password_pdf = tmp_path / "password.pdf"
    with pikepdf.new() as pdf:
        pdf.save(password_pdf, encryption=pikepdf.Encryption(user="secret", owner="x"))
    assert asyncio.run(
        MarkAnyCsoGradeAdapter().read_grades((str(hml), str(password_pdf)))
    ) == (
        CsoGradeResult(2, CsoGradeStatus.TAGGED),
        CsoGradeResult(None, CsoGradeStatus.UNTAGGED),
    )


@pytest.mark.parametrize("module", ("olefile", "pikepdf"))
def test_missing_optional_dependency_is_an_error(tmp_path, monkeypatch, module, caplog):
    path = tmp_path / "document"
    path.write_bytes(
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        if module == "olefile"
        else b"%PDF-1.7\n/Encrypt"
    )
    real_import = builtins.__import__

    def without_dependency(name, *args, **kwargs):
        if name == module:
            raise ImportError(f"missing {module}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_dependency)
    reader = MarkAnyCsoGradeAdapter(logger=logging.getLogger(__name__))
    assert asyncio.run(reader.read_grades((str(path),))) == (
        CsoGradeResult(None, CsoGradeStatus.ERROR),
    )
    assert "CSO grade lookup failed" in caplog.text
    assert f"{module} module is not installed" in caplog.text


def test_missing_file_warning_is_in_english(tmp_path, caplog):
    path = tmp_path / "missing.docx"
    reader = MarkAnyCsoGradeAdapter(logger=logging.getLogger(__name__))
    assert asyncio.run(reader.read_grades((str(path),))) == (
        CsoGradeResult(None, CsoGradeStatus.ERROR),
    )
    message = caplog.records[-1].getMessage()
    assert message.startswith(f"CSO grade lookup failed path={str(path)!r}")
    assert "detail=Failed to open file:" in message
    assert "열기 실패" not in message


def test_invalid_grade_error_is_in_english_and_retains_traceback(tmp_path, caplog):
    path = write_docx(tmp_path / "invalid.docx", "X")
    reader = MarkAnyCsoGradeAdapter(logger=logging.getLogger(__name__))
    assert asyncio.run(reader.read_grades((str(path),))) == (
        CsoGradeResult(None, CsoGradeStatus.ERROR),
    )
    record = caplog.records[-1]
    assert record.getMessage() == f"CSO grade lookup failed path={str(path)!r}"
    assert record.exc_info is not None
    assert "ValueError: Invalid N2SF_Level: 'X'" in caplog.text


@pytest.mark.parametrize("value", ["1", "other", "faso"])
def test_invalid_provider_is_rejected(value):
    with pytest.raises(ConfigurationError, match="AIVAX_PIPELINE_CSO_PROVIDER"):
        EnvironmentLoader(environ={}).build_settings(
            {"AIVAX_PIPELINE_CSO_PROVIDER": value}
        )


def test_markany_bootstrap_does_not_load_java_adapter_or_runtime(monkeypatch):
    real_import = builtins.__import__

    def without_java(name, *args, **kwargs):
        if name in (
            "src.extensions.cso.jvm_process",
            "src.extensions.cso.fasoo_grade_adapter",
        ):
            raise AssertionError("MarkAny must not load a Java implementation")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_java)
    loader = EnvironmentLoader(environ={})
    assert loader.build_settings({}).cso_provider == "fasoo"
    settings = loader.build_settings(
        {"AIVAX_PIPELINE_CSO_ENABLED": "true", "AIVAX_PIPELINE_CSO_PROVIDER": "markany"}
    )
    bootstrap = PipelineBootstrap(settings, None, Mock())
    guard = bootstrap._create_input_guard(Mock())
    assert isinstance(guard._cso_grade_reader, MarkAnyCsoGradeAdapter)
    assert guard._cso_grade_reader.supports_mime_skip is False
    assert bootstrap._input_guard_runtime is None
