"""상주 JVM의 응답을 CSO 등급 계약으로 변환한다."""

import json

from src.domain.inspection_input.models.cso_grade_result import (
    CsoGradeResult,
    CsoGradeStatus,
)
from src.domain.inspection_input.ports.cso_grade_port import CsoGradePort
from src.extensions.cso.jvm_process import CsoJvmProcess


class FasooCsoGradeAdapter(CsoGradePort):
    def __init__(self, runtime: CsoJvmProcess) -> None:
        self._runtime = runtime

    async def read_grades(self, paths: tuple[str, ...]) -> tuple[CsoGradeResult, ...]:
        if not paths:
            return ()
        stdout = await self._runtime.request(paths)
        return self._parse_results(stdout, len(paths))

    @staticmethod
    def _parse_results(
        stdout: bytes,
        expected_count: int,
    ) -> tuple[CsoGradeResult, ...]:
        documents = json.loads(stdout)
        if not isinstance(documents, list) or len(documents) != expected_count:
            raise ValueError("CSO 조회 결과 수가 입력 파일 수와 다릅니다.")

        results = []
        for document in documents:
            if not isinstance(document, dict) or "grade" not in document:
                raise ValueError("잘못된 CSO 조회 결과 형식입니다.")
            status = CsoGradeStatus(document["status"])
            if status is CsoGradeStatus.SKIPPED:
                raise ValueError("CSO 조회 응답은 생략 상태를 반환할 수 없습니다.")
            grade = document["grade"]
            if status is CsoGradeStatus.TAGGED:
                if type(grade) is not int or grade not in (1, 2, 3):
                    raise ValueError("잘못된 CSO 등급입니다.")
            elif grade is not None:
                raise ValueError("등급이 없는 상태의 grade는 null이어야 합니다.")
            results.append(CsoGradeResult(grade=grade, status=status))
        return tuple(results)
