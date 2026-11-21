"""SLM 모델별 외부 추론 adapter."""

from src.adapters.inspection_slm.factory import (
    create_slm_inspection_adapter,
)


__all__ = [
    "create_slm_inspection_adapter",
]
