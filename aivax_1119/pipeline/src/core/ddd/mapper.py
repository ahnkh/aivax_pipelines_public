"""도메인 객체와 영속 모델의 변환 계약."""

from __future__ import annotations

from typing import Protocol, TypeVar


DomainEntity = TypeVar("DomainEntity", covariant=True)
DbModel = TypeVar("DbModel")

# Python 3.13에서는 SaveEntity의 기본값을 DomainEntity로 둔다.
try:
    SaveEntity = TypeVar(
        "SaveEntity",
        contravariant=True,
        default=DomainEntity,
    )
except TypeError:  # pragma: no cover - 개발 도구의 구형 Python 호환
    SaveEntity = TypeVar("SaveEntity", contravariant=True)


class Mapper(Protocol[DomainEntity, DbModel, SaveEntity]):
    """도메인 객체와 저장소 모델을 상호 변환하는 인터페이스."""

    def toPersistence(self, entity: SaveEntity) -> DbModel:
        """저장할 도메인 객체를 저장소 모델로 변환한다."""
        ...

    def toDomain(self, record: DbModel) -> DomainEntity:
        """저장소 모델을 도메인 객체로 변환한다."""
        ...
