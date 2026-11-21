"""정규화된 값의 결정적 해시를 생성하는 공통 기능."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


class HashGenerator:
    """저장소나 도메인에 종속되지 않는 결정적 해시 생성기."""

    @staticmethod
    def blake2s(
        value: Mapping[str, Any],
        *,
        digest_size: int = 16,
    ) -> str:
        """정렬된 JSON 표현의 BLAKE2s 16진수 해시를 반환한다."""
        # dict 입력 순서가 달라도 같은 값은 같은 해시가 되도록 key를 정렬한다.
        payload = json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.blake2s(payload, digest_size=digest_size).hexdigest()

    @staticmethod
    def sha256_file(
        path: str | Path,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> str:
        """파일을 청크 단위로 읽어 SHA-256 16진수 해시를 반환한다."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")

        digest = hashlib.sha256()
        with Path(path).open("rb") as file_object:
            for chunk in iter(lambda: file_object.read(chunk_size), b""):
                digest.update(chunk)
        return digest.hexdigest()
