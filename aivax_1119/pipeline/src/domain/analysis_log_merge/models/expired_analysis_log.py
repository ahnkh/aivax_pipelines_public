"""대기 기한이 지나 adapter가 회수한 분석 로그 모델."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.core.ddd.domain_entity import DomainEntity
from src.domain.analysis_log_merge.errors import AnalysisLogMergeDomainError
from src.domain.analysis_log_merge.models.analysis_log_message_key import (
    AnalysisLogMessageKey,
)
from src.domain.analysis_log_merge.models.analysis_log_payload import AnalysisLogPayload


@dataclass(frozen=True, slots=True)
class ExpiredAnalysisLog(DomainEntity):
    """상대 분석 로그 없이 만료되어 단독 처리해야 하는 분석 로그.

    ``payload=None``은 저장소 TTL이 sweep보다 먼저 지나 원본 로그가 이미
    유실된 경우다. 호출자는 이 경우 저장하지 않고 유실 로그만 남긴다.
    """

    message_key: AnalysisLogMessageKey
    payload: AnalysisLogPayload | None
    # INPUT이 만료되기 전에 보관돼 있던 OUTPUT. 있으면 프롬프트 단독으로 적재하지 않는다.
    late_output: AnalysisLogPayload | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.message_key, AnalysisLogMessageKey):
            raise AnalysisLogMergeDomainError.empty_message_key(self.message_key)
        if self.payload is not None and not isinstance(self.payload, Mapping):
            raise AnalysisLogMergeDomainError.invalid_log_document(self.payload)
        if self.late_output is not None and not isinstance(self.late_output, Mapping):
            raise AnalysisLogMergeDomainError.invalid_log_document(self.late_output)

    @property
    def is_lost(self) -> bool:
        """원본 로그가 TTL 만료로 이미 사라졌는지 반환한다."""
        return self.payload is None
