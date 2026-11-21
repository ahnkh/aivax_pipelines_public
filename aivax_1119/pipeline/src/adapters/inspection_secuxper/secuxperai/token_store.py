"""SecuXperAI access token 저장소 내부 계약."""

from typing import Protocol


class SecuxperaiTokenStore(Protocol):
    async def get(self) -> str | None:
        """현재 공유 access token을 조회한다."""
        ...

    async def save(self, access_token: str) -> None:
        """공유 access token을 저장한다."""
        ...

    async def acquire_lock(
        self,
        owner_id: str,
        ttl_seconds: int,
    ) -> bool:
        """token 발급·갱신용 분산 lock 획득을 시도한다."""
        ...

    async def release_lock(self, owner_id: str) -> None:
        """현재 소유자의 분산 lock을 해제한다."""
        ...
