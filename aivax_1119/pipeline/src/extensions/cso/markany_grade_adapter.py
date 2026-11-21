"""MarkAny N2SF 태그를 기존 CSO 조회 계약으로 변환한다."""

import logging

from anyio.to_thread import run_sync

from src.domain.inspection_input.models.cso_grade_result import (
    CsoGradeResult,
    CsoGradeStatus,
)
from src.domain.inspection_input.ports.cso_grade_port import CsoGradePort
from third_party.markany.n2sf_read import read_tags

_ERROR_DETAILS = (
    ("열기 실패", "Failed to open file"),
    ("zip 파싱 실패", "ZIP parsing failed"),
    ("CFB 파싱 실패", "CFB parsing failed"),
    ("olefile 모듈 없음", "olefile module is not installed"),
    (
        "암호화 PDF — pikepdf 모듈 없음",
        "Encrypted PDF: pikepdf module is not installed",
    ),
    ("암호화 PDF 복호 실패", "Encrypted PDF decryption failed"),
    ("property set 너무 짧음", "Property set is too short"),
    ("property set 헤더 손상", "Corrupted property set header"),
    ("섹션 오프셋 손상", "Corrupted section offset"),
    ("섹션 손상", "Corrupted section"),
    ("사전 손상", "Corrupted property dictionary"),
)


def _english_error_detail(detail: str) -> str:
    for original, translated in _ERROR_DETAILS:
        detail = detail.replace(original, translated)
    return detail


class MarkAnyCsoGradeAdapter(CsoGradePort):
    supports_mime_skip = False

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("khan-python")

    async def read_grades(self, paths: tuple[str, ...]) -> tuple[CsoGradeResult, ...]:
        if not paths:
            return ()
        return await run_sync(self._read_grades, paths)

    def _read_grades(self, paths: tuple[str, ...]) -> tuple[CsoGradeResult, ...]:
        results = []
        for path in paths:
            try:
                result = read_tags(path)
                status = result["status"]
                if status == "UNSUPPORTED":
                    grade = CsoGradeResult(None, CsoGradeStatus.ERROR, unsupported=True)
                elif status == "ERROR":
                    self._logger.warning(
                        "CSO grade lookup failed path=%r detail=%s",
                        path,
                        _english_error_detail(result.get("detail", "")),
                    )
                    grade = CsoGradeResult(None, CsoGradeStatus.ERROR)
                elif status == "OK":
                    level = result["tags"].get("N2SF_Level")
                    if level is None or level == "":
                        grade = CsoGradeResult(None, CsoGradeStatus.UNTAGGED)
                    else:
                        value = {"C": 1, "S": 2, "O": 3}.get(level)
                        if value is None:
                            raise ValueError(f"Invalid N2SF_Level: {level!r}")
                        grade = CsoGradeResult(value, CsoGradeStatus.TAGGED)
                else:
                    raise ValueError(f"Invalid MarkAny read status: {status!r}")
            except Exception:
                self._logger.exception("CSO grade lookup failed path=%r", path)
                grade = CsoGradeResult(None, CsoGradeStatus.ERROR)
            results.append(grade)
        return tuple(results)
