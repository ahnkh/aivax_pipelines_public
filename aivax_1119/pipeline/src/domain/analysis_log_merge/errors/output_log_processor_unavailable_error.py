"""출력 로그 처리기가 준비되지 않은 경우의 예외."""

from src.core.ddd.domain_exception import DomainException


class OutputLogProcessorUnavailableError(DomainException):
    def __init__(self) -> None:
        super().__init__(
            "analysis-log-merge",
            "output-processor-unavailable",
            "invalid pipeline, not exist pipeline, id = output_filter",
        )
