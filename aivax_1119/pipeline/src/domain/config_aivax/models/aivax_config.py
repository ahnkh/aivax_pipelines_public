"""AIVAX 프로세스 실행 설정 entity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.core.ddd.domain_entity import DomainEntity
from src.domain.config_aivax.errors import AivaxConfigDomainError
from src.domain.config_aivax.models.aivax_filters import AivaxFilters
from src.domain.config_aivax.models.aivax_mode import AivaxMode


@dataclass(frozen=True, slots=True)
class AivaxConfig(DomainEntity):
    schema_version: int = 2
    mode: AivaxMode = field(default_factory=AivaxMode)
    updated_at: datetime = datetime(1970, 1, 1, tzinfo=timezone.utc)
    source: str = "default"
    filters: AivaxFilters = field(default_factory=AivaxFilters)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.filters, AivaxFilters):
            raise AivaxConfigDomainError(
                "invalid-filters", "필터 설정이 올바르지 않습니다."
            )
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version not in (1, 2)
        ):
            raise AivaxConfigDomainError.invalid_schema_version(self.schema_version)
        if not isinstance(self.mode, AivaxMode):
            raise AivaxConfigDomainError.invalid_mode(self.mode)
        if (
            not isinstance(self.updated_at, datetime)
            or self.updated_at.tzinfo is None
            or self.updated_at.utcoffset() is None
        ):
            raise AivaxConfigDomainError.invalid_updated_at(self.updated_at)
        if not isinstance(self.source, str) or not self.source.strip():
            raise AivaxConfigDomainError.invalid_source(self.source)

    @property
    def is_mirror(self) -> bool:
        return self.mode.is_mirror

    @property
    def is_proxy(self) -> bool:
        return self.mode.is_proxy
