"""공통 로컬 파일 연산 테스트."""

from pathlib import Path

from src.core.file import LocalFileOperations


def test_reads_only_requested_header_bytes(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"%PDF-document")

    assert LocalFileOperations.read_header(path, 5) == b"%PDF-"
    assert LocalFileOperations.read_header(path, 0) == b""


def test_copies_file_without_removing_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "copied"
    source.write_bytes(b"contents")

    LocalFileOperations.copy(source, destination)

    assert source.read_bytes() == b"contents"
    assert destination.read_bytes() == b"contents"


def test_moves_file_after_copying_and_verifying_size(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "moved"
    source.write_bytes(b"contents")

    moved = LocalFileOperations.move(source, destination)

    assert moved is True
    assert not source.exists()
    assert destination.read_bytes() == b"contents"


def test_deletes_file_and_can_ignore_missing_path(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"contents")

    LocalFileOperations.delete(path)
    LocalFileOperations.delete(path, missing_ok=True)

    assert not path.exists()
