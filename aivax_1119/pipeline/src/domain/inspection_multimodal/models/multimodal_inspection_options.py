"""멀티모달 검사 실행 옵션."""

from dataclasses import dataclass

from src.domain.inspection_multimodal.models.watermark_criteria import WatermarkCriteria


@dataclass(frozen=True, slots=True)
class MultimodalInspectionOptions:
    bypass: bool
    read_timeout_seconds: int
    unsupported_file_handling: str
    oversized_file_handling: str
    content_chunk_size: int
    detail_reason_directory: str
    watermark_criteria: WatermarkCriteria
