"""Valkey 기반 SecuXperAI 공유 access token 저장소."""

from __future__ import annotations

from functools import partial

from anyio.to_thread import run_sync
from valkey import Valkey


class ValkeySecuxperaiTokenStoreAdapter:
    """SecuXperAI access token과 갱신용 분산 lock을 Valkey에 저장한다."""

    _RELEASE_LOCK_SCRIPT = """
local current_owner = redis.call('GET', KEYS[1])
if current_owner == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

    def __init__(
        self,
        client: Valkey,
        *,
        token_key: str,
        lock_key: str,
    ) -> None:
        if not token_key.strip():
            raise ValueError(
                "SecuXperAI access token key는 비어 있을 수 없습니다.",
            )
        if not lock_key.strip():
            raise ValueError(
                "SecuXperAI access token lock key는 비어 있을 수 없습니다.",
            )
        if token_key == lock_key:
            raise ValueError(
                "SecuXperAI token key와 lock key는 달라야 합니다.",
            )

        self._client = client
        self._token_key = token_key
        self._lock_key = lock_key
        self._release_lock_script = self._client.register_script(
            self._RELEASE_LOCK_SCRIPT,
        )

    async def get(self) -> str | None:
        """현재 공유 access token을 조회한다."""
        return await run_sync(self._get)

    async def save(self, access_token: str) -> None:
        """공유 access token을 만료 시간 없이 저장한다."""
        token = access_token.strip()
        if not token:
            raise ValueError(
                "SecuXperAI access token은 비어 있을 수 없습니다.",
            )
        await run_sync(
            partial(
                self._client.set,
                self._token_key,
                token,
            ),
        )

    async def acquire_lock(
        self,
        owner_id: str,
        ttl_seconds: int,
    ) -> bool:
        """NX/EX 조건으로 토큰 발급·갱신 lock 획득을 시도한다."""
        owner = owner_id.strip()
        if not owner:
            raise ValueError(
                "SecuXperAI token lock owner가 필요합니다.",
            )
        if ttl_seconds <= 0:
            raise ValueError(
                "SecuXperAI token lock TTL은 양수여야 합니다.",
            )

        result = await run_sync(
            partial(
                self._client.set,
                self._lock_key,
                owner,
                nx=True,
                ex=ttl_seconds,
            ),
        )
        return bool(result)

    async def release_lock(self, owner_id: str) -> None:
        """자신이 소유한 lock만 원자적으로 해제한다."""
        owner = owner_id.strip()
        if not owner:
            return

        await run_sync(
            partial(
                self._release_lock_script,
                keys=(self._lock_key,),
                args=(owner,),
            ),
        )

    def _get(self) -> str | None:
        raw_token = self._client.get(self._token_key)
        if raw_token is None:
            return None

        token = (
            raw_token.decode("utf-8")
            if isinstance(raw_token, bytes)
            else str(raw_token)
        ).strip()
        return token or None
