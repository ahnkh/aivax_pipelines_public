"""출력 로그에 적용할 저장 정책. 설정 파일과 pipeline 객체는 알지 못한다."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutputLogOptions:
    store_response_text: bool = True
    response_max_bytes: int = 200_000
    include_usage: bool = True
