from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WatermarkCriteria:
    drm_enabled: bool
    drm_pattern: str
    drm_pattern_flags: int
    drm_header_limit: int
    ocr_enabled: bool
    ocr_patterns: tuple[str, ...]
    ocr_pattern_flags: int
    ocr_hit_threshold: int
    ocr_pages: tuple[int, ...]
