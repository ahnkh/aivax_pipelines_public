"""실제 PDF로 엔진 선택·근거 생성·추출 결과 재사용을 검증한다."""

import asyncio
import re
from pathlib import Path
from unittest.mock import Mock

import fitz
import pytest

from src.adapters.inspection_multimodal import LocalMultimodalInspectionAdapter
from src.domain.inspection_multimodal.models import (
    DocumentSource,
    FileMetadata,
    MultimodalInspectionOptions,
    MultimodalInspectionRequest,
    MultimodalInspectionTarget,
    WatermarkCriteria,
)
from src.domain.policy_multimodal.models import (
    MultimodalPolicyRule,
    MultimodalPolicySnapshot,
)


@pytest.fixture
def pdf_path(tmp_path: Path) -> Path:
    path = tmp_path / "document.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((40, 50), "secret one")
        pdf.new_page()
        pdf.new_page().insert_text((40, 50), "secret two")
        pdf.save(path)
    return path


def _request(path: Path, expression="secret") -> MultimodalInspectionRequest:
    return MultimodalInspectionRequest(
        targets=(
            MultimodalInspectionTarget(DocumentSource.from_path(path), path.name),
        ),
        policy=MultimodalPolicySnapshot(
            rules=(
                MultimodalPolicyRule(
                    id="first", name="Secret", expression=expression, action="block"
                ),
                MultimodalPolicyRule(
                    id="second", name="Other", expression="one", action="accept"
                ),
            )
        ),
        options=MultimodalInspectionOptions(
            bypass=False,
            read_timeout_seconds=30,
            unsupported_file_handling="block",
            oversized_file_handling="block",
            content_chunk_size=100,
            detail_reason_directory=str(path.parent),
            watermark_criteria=WatermarkCriteria(
                drm_enabled=False,
                drm_pattern="",
                drm_pattern_flags=0,
                drm_header_limit=0,
                ocr_enabled=False,
                ocr_patterns=(),
                ocr_pattern_flags=0,
                ocr_hit_threshold=0,
                ocr_pages=(),
            ),
        ),
    )


@pytest.mark.parametrize("engine", ["pdfplumber", "pymupdf"])
def test_both_engines_preserve_policy_and_return_real_pdf_evidence(pdf_path, engine):
    adapter = LocalMultimodalInspectionAdapter(pdf_evidence_engine=engine)

    result = asyncio.run(adapter.inspect(_request(pdf_path)))

    assert result.action == "block"
    assert result.selected_policy.id == "first"
    assert result.files[0].content_chunk == "secret one\nsecret two"
    assert [
        (e.sequence, e.page_number, e.line_number, e.context, e.matched_text)
        for e in result.files[0].evidence
    ] == [
        (1, 1, 1, "secret one", "secret"),
        (2, 3, 1, "secret two", "secret"),
    ]


def test_pymupdf_keeps_evidence_when_hyphens_have_a_small_vertical_offset(tmp_path):
    path = tmp_path / "postal-codes.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page()
        for y, first, second in [(50, "174", "142"), (80, "133", "135")]:
            page.insert_text((40, y), first, fontsize=10)
            page.insert_text((80, y + 0.1), "-", fontsize=9.9)
            page.insert_text((100, y), second, fontsize=10)
        pdf.save(path)
    adapter = LocalMultimodalInspectionAdapter(pdf_evidence_engine="pymupdf")

    result = asyncio.run(adapter.inspect(_request(path, r"\d{3}\s*-\s*\d{3}")))

    assert result.action == "block"
    assert result.selected_policy.id == "first"
    assert [
        (item.page_number, item.line_number, item.matched_text)
        for item in result.files[0].evidence
    ] == [(1, 1, "174 - 142"), (1, 2, "133 - 135")]


def test_pymupdf_reuses_text_positions_without_reopening_pdf(pdf_path, monkeypatch):
    adapter = LocalMultimodalInspectionAdapter(pdf_evidence_engine="pymupdf")
    opened = Mock(wraps=fitz.open)
    monkeypatch.setattr(fitz, "open", opened)
    old_extract = Mock(side_effect=AssertionError("unexpected text re-extraction"))
    old_evidence = Mock(side_effect=AssertionError("unexpected PDF re-analysis"))
    monkeypatch.setattr(adapter._text_extractor, "extract", old_extract)
    monkeypatch.setattr(adapter, "_collect_pdf_evidence", old_evidence)

    result = asyncio.run(adapter.inspect(_request(pdf_path)))

    assert result.action == "block"
    assert len(result.files[0].evidence) == 2
    opened.assert_called_once_with(pdf_path)
    old_extract.assert_not_called()
    old_evidence.assert_not_called()


def test_pymupdf_returns_no_evidence_when_no_policy_matches(pdf_path, monkeypatch):
    adapter = LocalMultimodalInspectionAdapter(pdf_evidence_engine="pymupdf")
    request = _request(pdf_path, "absent")
    # Both rules must be absent to exercise the non-detection path.
    request = MultimodalInspectionRequest(
        targets=request.targets,
        policy=MultimodalPolicySnapshot(rules=(request.policy.rules[0],)),
        options=request.options,
    )
    old_evidence = Mock()
    monkeypatch.setattr(adapter, "_collect_pdf_evidence", old_evidence)

    result = asyncio.run(adapter.inspect(request))

    assert result.action == ""
    assert result.files[0].content_inspected
    assert result.files[0].evidence == ()
    old_evidence.assert_not_called()


@pytest.mark.parametrize("engine", ["pdfplumber", "pymupdf"])
def test_converted_office_pdf_uses_selected_engine(pdf_path, monkeypatch, engine):
    adapter = LocalMultimodalInspectionAdapter(pdf_evidence_engine=engine)
    conversion = Mock(return_value=pdf_path)
    monkeypatch.setattr(adapter._text_extractor, "convert_to_pdf", conversion)
    legacy = Mock(wraps=adapter._collect_pdfplumber_evidence)
    native = Mock(wraps=adapter._pdf_text_extractor.extract)
    monkeypatch.setattr(adapter, "_collect_pdfplumber_evidence", legacy)
    monkeypatch.setattr(adapter._pdf_text_extractor, "extract", native)

    result = adapter.collect_evidence(
        DocumentSource.from_path(pdf_path.with_suffix(".docx")),
        FileMetadata(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
            0,
        ),
        re.compile("secret"),
        str(pdf_path.parent / "evidence"),
        30,
    )

    assert len(result.evidence) == 2
    conversion.assert_called_once()
    assert legacy.call_count == (engine == "pdfplumber")
    assert native.call_count == (engine == "pymupdf")


def test_position_failure_keeps_block_decision_without_legacy_fallback(
    pdf_path, monkeypatch, caplog
):
    adapter = LocalMultimodalInspectionAdapter(pdf_evidence_engine="pymupdf")
    original = fitz.Page.get_text

    def get_text(page, option="text", *args, **kwargs):
        if option == "words":
            raise RuntimeError("positions unavailable")
        return original(page, option, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_text", get_text)
    legacy = Mock(side_effect=AssertionError("unexpected legacy fallback"))
    monkeypatch.setattr(adapter, "_collect_pdfplumber_evidence", legacy)

    result = asyncio.run(adapter.inspect(_request(pdf_path)))

    assert result.action == "block"
    assert result.selected_policy.id == "first"
    assert result.files[0].content_chunk == "secret one\nsecret two"
    assert result.files[0].evidence == ()
    legacy.assert_not_called()
    assert "fail extract PDF text positions" in caplog.text


def test_pdfplumber_remains_default_and_invalid_engine_is_rejected(pdf_path):
    adapter = LocalMultimodalInspectionAdapter()
    result = asyncio.run(adapter.inspect(_request(pdf_path)))
    assert len(result.files[0].evidence) == 2
    assert adapter._pdf_evidence_engine == "pdfplumber"
    with pytest.raises(ValueError, match="pdf_evidence_engine"):
        LocalMultimodalInspectionAdapter(pdf_evidence_engine="other")


def test_pymupdf_keeps_concurrent_file_results_separate(tmp_path):
    paths = []
    for name in ["alpha", "beta"]:
        path = tmp_path / f"{name}.pdf"
        with fitz.open() as pdf:
            pdf.new_page().insert_text((40, 50), f"secret {name}")
            pdf.save(path)
        paths.append(path)
    adapter = LocalMultimodalInspectionAdapter(pdf_evidence_engine="pymupdf")

    async def inspect():
        return await asyncio.gather(*(adapter.inspect(_request(p)) for p in paths))

    results = asyncio.run(inspect())

    assert [r.files[0].evidence[0].context for r in results] == [
        "secret alpha",
        "secret beta",
    ]
