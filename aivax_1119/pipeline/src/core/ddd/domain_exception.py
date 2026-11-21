"""도메인 계층 예외의 공통 기반 객체."""

from __future__ import annotations

from typing import Any, Mapping


class DomainException(Exception):
    """도메인·코드·상세 정보를 보존하는 기계 판독 가능한 도메인 예외."""

    def __init__(
        self,
        domain: str,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.domain = domain
        self.code = code
        self.details = dict(details) if details is not None else None
        self.name = f"{domain}:{code}"
