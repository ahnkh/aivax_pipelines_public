"""시간 표현을 공통 형식으로 정규화하는 기능."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal


IsoTimeSpec = Literal[
    "auto",
    "hours",
    "minutes",
    "seconds",
    "milliseconds",
    "microseconds",
]


class TimeNormalizer:
    """저장소나 도메인에 종속되지 않는 시간 정규화기."""

    @staticmethod
    def utc_now() -> datetime:
        """현재 UTC 시각을 timezone-aware ``datetime``으로 반환한다."""
        return datetime.now(timezone.utc)

    @classmethod
    def utc_now_iso(cls) -> str:
        """현재 UTC 시각을 ISO-8601 `Z` 문자열로 반환한다."""
        return cls.to_utc_iso(cls.utc_now())

    @staticmethod
    def to_utc_iso(
        value: str | datetime,
        *,
        timespec: IsoTimeSpec = "auto",
    ) -> str:
        """시간대가 있는 값을 UTC ISO-8601 `Z` 문자열로 정규화한다."""
        if isinstance(value, str):
            occurred_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif isinstance(value, datetime):
            occurred_at = value
        else:
            raise TypeError("value must be an ISO 8601 string or datetime")

        if occurred_at.tzinfo is None:
            raise ValueError("value must include a timezone")
        return (
            occurred_at.astimezone(timezone.utc)
            .isoformat(timespec=timespec)
            .replace("+00:00", "Z")
        )

    @classmethod
    def to_utc_second(cls, value: str) -> str:
        """시간대가 있는 ISO 8601 시각을 UTC 초 단위 문자열로 정규화한다."""
        return cls.to_utc_iso(value, timespec="seconds")
