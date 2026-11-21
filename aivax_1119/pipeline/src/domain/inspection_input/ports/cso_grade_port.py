"""파일 경로 목록의 CSO 등급을 조회하는 계약."""

from abc import ABC, abstractmethod

from src.domain.inspection_input.models.cso_grade_result import CsoGradeResult


class CsoGradePort(ABC):
    supports_mime_skip: bool = True

    @abstractmethod
    async def read_grades(self, paths: tuple[str, ...]) -> tuple[CsoGradeResult, ...]:
        """입력 경로 순서대로 조회 결과를 반환한다."""
        raise NotImplementedError
