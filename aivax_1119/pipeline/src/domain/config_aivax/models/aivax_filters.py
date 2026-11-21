"""전역 필터 활성화 설정."""

from dataclasses import dataclass, field

from src.domain.config_aivax.errors import AivaxConfigDomainError


@dataclass(frozen=True, slots=True)
class FilterSettings:
    enabled: bool = True

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise AivaxConfigDomainError(
                "invalid-filters", "enabled는 boolean이어야 합니다."
            )


@dataclass(frozen=True, slots=True)
class AivaxFilters:
    multimodal: FilterSettings = field(default_factory=FilterSettings)
    regex: FilterSettings = field(default_factory=FilterSettings)
    slm: FilterSettings = field(default_factory=FilterSettings)
    secuxper: FilterSettings = field(default_factory=lambda: FilterSettings(False))

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if not isinstance(getattr(self, name), FilterSettings):
                raise AivaxConfigDomainError(
                    "invalid-filters", "필터 설정이 올바르지 않습니다."
                )

    @classmethod
    def from_payload(cls, payload: object) -> "AivaxFilters":
        if (
            not isinstance(payload, dict)
            or payload.keys() - cls.__dataclass_fields__.keys()
        ):
            raise AivaxConfigDomainError(
                "invalid-filters", "알 수 없는 필터 설정입니다."
            )
        values = {}
        for name, value in payload.items():
            if not isinstance(value, dict) or set(value) != {"enabled"}:
                raise AivaxConfigDomainError(
                    "invalid-filters", "필터에는 enabled boolean이 필요합니다."
                )
            values[name] = FilterSettings(value["enabled"])
        return cls(**values)
