"""PDF 텍스트·위치 동시 추출과 기존 검사 텍스트의 호환성."""

from pathlib import Path
from unittest.mock import Mock

import fitz

from src.core.file import LocalDocumentTextExtractor, LocalPdfTextExtractor


def test_extracts_same_text_and_preserves_page_numbers(tmp_path, monkeypatch):
    path = tmp_path / "document.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((40, 50), "secret one\nsecond line")
        pdf.new_page()
        pdf.new_page().insert_text((40, 50), "secret two")
        pdf.save(path)
    expected = LocalDocumentTextExtractor._read_pdf(path)
    original = fitz.Page.get_textpage
    calls = []

    def get_textpage(page, *args, **kwargs):
        calls.append((page.number, kwargs["flags"]))
        return original(page, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_textpage", get_textpage)
    opened = Mock(wraps=fitz.open)
    monkeypatch.setattr(fitz, "open", opened)

    result = LocalPdfTextExtractor().extract(path)

    assert result.text == expected
    assert calls == [
        (0, fitz.TEXTFLAGS_TEXT),
        (1, fitz.TEXTFLAGS_TEXT),
        (2, fitz.TEXTFLAGS_TEXT),
    ]
    opened.assert_called_once_with(path)
    assert [
        (line.page_number, line.line_number, line.text) for line in result.lines
    ] == [
        (1, 1, "secret one"),
        (1, 2, "second line"),
        (3, 1, "secret two"),
    ]


def test_positions_only_include_visible_page_text(tmp_path: Path):
    path = tmp_path / "cropped.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=400, height=200)
        page.insert_text((20, 50), "visible 17118")
        page.insert_text((220, 50), "outside 14250")
        page.set_cropbox(fitz.Rect(0, 0, 200, 200))
        pdf.save(path)

    result = LocalPdfTextExtractor().extract(path)

    assert result.text == "visible 17118"
    assert [line.text for line in result.lines] == ["visible 17118"]


def test_groups_small_vertical_offsets_and_orders_words_by_position(tmp_path: Path):
    path = tmp_path / "offset-words.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page()
        # Content stream order differs from the visual left-to-right order.
        for x, y, text in [
            (40, 50, "174"),
            (100, 50, "142"),
            (80, 50.1, "-"),
            (40, 80, "133"),
            (100, 80, "135"),
            (80, 80.1, "-"),
        ]:
            page.insert_text((x, y), text, fontsize=10)
        pdf.save(path)

    result = LocalPdfTextExtractor().extract(path)

    assert result.text == LocalDocumentTextExtractor._read_pdf(path)
    assert [
        (line.page_number, line.line_number, line.text) for line in result.lines
    ] == [(1, 1, "174 - 142"), (1, 2, "133 - 135")]


def test_keeps_close_small_font_lines_separate(tmp_path: Path):
    path = tmp_path / "small-lines.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 50), "alpha", fontsize=2)
        page.insert_text((70, 51), "beta", fontsize=2)
        pdf.save(path)

    result = LocalPdfTextExtractor().extract(path)

    assert [line.text for line in result.lines] == ["alpha", "beta"]


def test_position_failure_preserves_all_text_and_discards_partial_lines(
    tmp_path, monkeypatch, caplog
):
    path = tmp_path / "document.pdf"
    with fitz.open() as pdf:
        for text in ["first secret", "second secret", "third secret"]:
            pdf.new_page().insert_text((40, 50), text)
        pdf.save(path)
    original = fitz.Page.get_text

    def get_text(page, option="text", *args, **kwargs):
        if option == "words" and page.number == 1:
            raise RuntimeError("position extraction failed")
        return original(page, option, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_text", get_text)

    result = LocalPdfTextExtractor().extract(path)

    assert result.text == "first secret\nsecond secret\nthird secret"
    assert result.lines == ()
    assert "fail extract PDF text positions" in caplog.text
