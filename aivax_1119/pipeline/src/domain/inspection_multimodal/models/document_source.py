from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DocumentSource:
    """검사 대상 로컬 첨부파일 참조."""

    path: Path

    @classmethod
    def from_path(cls, path: str | Path) -> "DocumentSource":
        return cls(Path(path))
