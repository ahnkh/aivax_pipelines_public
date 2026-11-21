"""외부 감사 시스템에 기록할 이벤트."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuditEvent:
    succeeded: bool
    category: int
    source: str
    message: str

    def __post_init__(self) -> None:
        if self.category < 0:
            raise ValueError("감사 이벤트 category는 음수일 수 없습니다.")
        if not self.source.strip():
            raise ValueError("감사 이벤트 source가 필요합니다.")
        if not self.message.strip():
            raise ValueError("감사 이벤트 message가 필요합니다.")
