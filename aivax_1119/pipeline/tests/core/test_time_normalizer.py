from src.core.time import TimeNormalizer


def test_to_utc_second_removes_subsecond_precision() -> None:
    assert TimeNormalizer.to_utc_second("2026-09-10T09:00:00.999+09:00") == (
        "2026-09-10T00:00:00Z"
    )


def test_to_utc_iso_preserves_precision_and_normalizes_timezone() -> None:
    assert TimeNormalizer.to_utc_iso("2026-09-10T09:00:00.999+09:00") == (
        "2026-09-10T00:00:00.999000Z"
    )
