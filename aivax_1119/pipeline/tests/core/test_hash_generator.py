from pathlib import Path

import pytest

from src.core.hash import HashGenerator


def test_blake2s_is_deterministic_for_different_mapping_orders() -> None:
    assert HashGenerator.blake2s({"b": "2", "a": "1"}) == HashGenerator.blake2s(
        {"a": "1", "b": "2"},
    )


def test_sha256_file_hashes_file_contents(tmp_path: Path) -> None:
    path = tmp_path / "document"
    path.write_bytes(b"content")

    assert HashGenerator.sha256_file(path, chunk_size=2) == (
        "ed7002b439e9ac845f22357d822bac1444730fbdb6016d3ec9432297b9ec9f73"
    )


def test_sha256_file_rejects_non_positive_chunk_size(tmp_path: Path) -> None:
    path = tmp_path / "document"
    path.touch()

    with pytest.raises(ValueError):
        HashGenerator.sha256_file(path, chunk_size=0)
