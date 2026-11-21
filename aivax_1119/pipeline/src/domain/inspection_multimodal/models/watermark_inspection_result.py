from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WatermarkInspectionResult:
    """DRM 헤더 또는 OCR 워터마크 검사 결과."""

    detected: bool = False
    kind: str = ""
    offset: int = 0
    length: int = 0
    matched_text: str = ""
    page_number: int = 0
    match_count: int = 0
    match_threshold: int = 0
