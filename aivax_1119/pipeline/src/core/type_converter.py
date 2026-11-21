"""외부 입력의 기본형 변환을 안전하게 처리하는 공통 기능."""

from __future__ import annotations


class TypeConverter:
    """변환할 수 없는 값을 호출자가 지정한 기본값으로 대체한다."""

    @staticmethod
    def to_int(value: object, default: int = 0) -> int:
        if value is None:
            return default

        text = str(value).strip()
        if not text:
            return default

        try:
            return int(text)
        except ValueError:
            return default
