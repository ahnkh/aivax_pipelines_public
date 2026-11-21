"""로컬 파일의 복사, 이동과 삭제를 처리하는 공통 기능."""

from __future__ import annotations

import shutil
from pathlib import Path


class LocalFileOperations:
    @staticmethod
    def read_header(path: str | Path, size: int) -> bytes:
        """파일 시작 위치에서 지정한 크기만큼 읽는다."""
        with Path(path).open("rb") as file_object:
            return file_object.read(max(0, size))

    @staticmethod
    def copy(source: str | Path, destination: str | Path) -> None:
        shutil.copyfile(Path(source), Path(destination))

    @staticmethod
    def delete(path: str | Path, *, missing_ok: bool = False) -> None:
        Path(path).unlink(missing_ok=missing_ok)

    @classmethod
    def move(cls, source: str | Path, destination: str | Path) -> bool:
        """파일을 복사해 크기를 확인한 후 원본을 삭제한다."""
        source_path = Path(source)
        destination_path = Path(destination)
        cls.copy(source_path, destination_path)

        if source_path.stat().st_size != destination_path.stat().st_size:
            cls.delete(destination_path, missing_ok=True)
            return False

        cls.delete(source_path)
        return True
