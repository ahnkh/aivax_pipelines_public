"""파일 전달 요청과 결과."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileDeliveryRequest:
    source_path: Path
    file_name: str


@dataclass(frozen=True, slots=True)
class FileDeliveryResult:
    delivered: bool = False
    destination: str = ""
