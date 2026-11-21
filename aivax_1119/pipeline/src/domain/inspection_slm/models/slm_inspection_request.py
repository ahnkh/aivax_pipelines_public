"""SLM 검사에 전달하는 입력 계약."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SlmInspectionRequest:
    prompt: str
