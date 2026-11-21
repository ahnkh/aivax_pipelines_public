"""TypeConverter 단위 테스트."""

from src.core.type_converter import TypeConverter


def test_to_int_converts_integer_text_and_integer() -> None:
    assert TypeConverter.to_int(" 12 ") == 12
    assert TypeConverter.to_int(12) == 12


def test_to_int_returns_default_for_missing_or_invalid_value() -> None:
    assert TypeConverter.to_int(None) == 0
    assert TypeConverter.to_int("") == 0
    assert TypeConverter.to_int("invalid", default=-1) == -1
