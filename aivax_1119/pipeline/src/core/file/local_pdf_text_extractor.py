"""PDF를 한 번 해석해 텍스트와 페이지별 줄 위치를 함께 추출한다."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import fitz

_PdfWord = tuple[float, float, float, float, str, int, int, int]


@dataclass(frozen=True, slots=True)
class PdfTextLine:
    page_number: int
    line_number: int
    text: str


@dataclass(frozen=True, slots=True)
class PdfTextExtraction:
    text: str
    lines: tuple[PdfTextLine, ...] = ()


class LocalPdfTextExtractor:
    """정책과 무관한 텍스트·위치 데이터만 반환한다."""

    _LOGGER = logging.getLogger("khan-python")
    _LINE_CENTER_TOLERANCE_RATIO = 0.25

    def extract(self, path: str | Path) -> PdfTextExtraction:
        texts: list[str] = []
        lines: list[PdfTextLine] = []
        layout_failed = False
        with fitz.open(path) as document:
            for page_number, page in enumerate(document, start=1):
                # 기존 get_text("text")와 같은 옵션으로 정책 검사 입력을 유지한다.
                text_page = page.get_textpage(flags=fitz.TEXTFLAGS_TEXT)
                text = page.get_text("text", textpage=text_page).strip()
                if text:
                    texts.append(text)
                if layout_failed:
                    continue
                try:
                    words = page.get_text("words", textpage=text_page)
                    lines.extend(
                        PdfTextLine(page_number, line_number, line)
                        for line_number, line in enumerate(
                            self._group_words_into_lines(words), start=1
                        )
                    )
                except Exception:
                    # 위치 추출 실패가 이미 확보한 검사 텍스트를 버리게 하지 않는다.
                    self._LOGGER.exception(
                        "fail extract PDF text positions: %s page=%s", path, page_number
                    )
                    lines.clear()
                    layout_failed = True
        return PdfTextExtraction(text="\n".join(texts), lines=tuple(lines))

    @classmethod
    def _group_words_into_lines(cls, words: list[_PdfWord]) -> tuple[str, ...]:
        grouped: list[list[_PdfWord]] = []
        # Glyphs on the same visual line can have different bounding-box tops.
        # Use vertical centers and scale the tolerance to the smaller word so
        # closely spaced small-font lines do not merge.
        for word in sorted(words, key=lambda item: (cls._center_y(item), item[0])):
            if grouped:
                # Keep the first word as the anchor to avoid joining separate
                # lines through a chain of small vertical offsets.
                anchor = grouped[-1][0]
                height = min(word[3] - word[1], anchor[3] - anchor[1])
                tolerance = height * cls._LINE_CENTER_TOLERANCE_RATIO
                if abs(cls._center_y(word) - cls._center_y(anchor)) <= tolerance:
                    grouped[-1].append(word)
                    continue
            grouped.append([word])
        return tuple(
            " ".join(word[4] for word in sorted(line, key=lambda item: item[0]))
            for line in grouped
        )

    @staticmethod
    def _center_y(word: _PdfWord) -> float:
        return (word[1] + word[3]) / 2
