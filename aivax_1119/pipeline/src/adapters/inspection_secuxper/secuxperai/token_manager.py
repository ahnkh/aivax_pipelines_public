"""SecuXperAI 공유 access token의 발급·갱신 서비스."""

from __future__ import annotations

import asyncio
import logging
import os
from time import monotonic
from uuid import uuid4

from src.adapters.inspection_secuxper.secuxperai.http_client import (
    SecuxperaiHttpClient,
)
from src.adapters.inspection_secuxper.secuxperai.settings import (
    SecuxperaiSettings,
)
from src.adapters.inspection_secuxper.secuxperai.token_store import (
    SecuxperaiTokenStore,
)


class SecuxperaiTokenManager:
    """Valkey를 통해 여러 프로세스가 SecuXperAI token을 공유하게 한다."""

    _LOCK_RETRY_INTERVAL_SECONDS = 0.1

    def __init__(
        self,
        settings: SecuxperaiSettings,
        token_store: SecuxperaiTokenStore,
        http_client: SecuxperaiHttpClient,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._token_store = token_store
        self._http_client = http_client
        self._logger = logger or logging.getLogger("khan-python")
        self._memory_token: str | None = None

    async def get_access_token(self) -> str | None:
        """공유 token을 조회하고 없으면 신규 발급한다."""
        access_token = await self._shared_token()
        if access_token is not None:
            return access_token

        return await self._replace_token(
            expected_token=None,
            try_refresh=False,
        )

    async def recover_after_auth_failure(
        self,
        failed_token: str,
        *,
        try_refresh: bool,
    ) -> str | None:
        """인증에 실패한 token을 공유 저장소 확인 후 교체한다."""
        current_token = await self._shared_token()
        if current_token is not None and current_token != failed_token:
            return current_token

        return await self._replace_token(
            expected_token=failed_token,
            try_refresh=try_refresh,
        )

    async def _replace_token(
        self,
        *,
        expected_token: str | None,
        try_refresh: bool,
    ) -> str | None:
        owner_id = self._owner_id()

        try:
            acquired = await self._token_store.acquire_lock(
                owner_id,
                self._settings.token_lock_ttl_seconds,
            )
        except Exception:
            self._logger.exception(
                "SecuXperAI token lock 획득 실패; 로컬에서 token 복구를 시도합니다",
            )
            return await self._replace_without_lock(
                expected_token=expected_token,
                try_refresh=try_refresh,
            )

        if not acquired:
            return await self._wait_for_replacement(
                expected_token,
                try_refresh=try_refresh,
            )

        try:
            # lock 대기 중 다른 프로세스가 token을 교체했을 수 있으므로
            # 실제 발급 또는 갱신 전에 반드시 다시 확인한다.
            current_token = await self._shared_token()
            if self._was_replaced(current_token, expected_token):
                return current_token

            return await self._replace_without_lock(
                expected_token=expected_token,
                try_refresh=try_refresh,
            )
        finally:
            try:
                await self._token_store.release_lock(owner_id)
            except Exception:
                self._logger.exception(
                    "SecuXperAI token lock 해제 실패",
                )

    async def _replace_without_lock(
        self,
        *,
        expected_token: str | None,
        try_refresh: bool,
    ) -> str | None:
        if try_refresh and expected_token:
            try:
                access_token = await self._http_client.refresh_token(
                    expected_token,
                )
            except Exception as error:
                self._logger.warning(
                    "SecuXperAI token refresh 실패; 신규 발급을 시도합니다 error=%s",
                    error,
                )
            else:
                await self._save_token(access_token)
                return access_token

        try:
            access_token = await self._http_client.issue_token()
        except Exception as error:
            self._logger.error(
                "SecuXperAI token 신규 발급 실패 error=%s",
                error,
            )
            return None

        await self._save_token(access_token)
        return access_token

    async def _wait_for_replacement(
        self,
        expected_token: str | None,
        *,
        try_refresh: bool,
    ) -> str | None:
        deadline = monotonic() + self._settings.token_wait_seconds

        while monotonic() < deadline:
            await asyncio.sleep(self._LOCK_RETRY_INTERVAL_SECONDS)
            current_token = await self._shared_token()
            if self._was_replaced(current_token, expected_token):
                return current_token

        # 여러 access token이 동시에 유효하므로 lock 보유자의 처리가
        # 길어져도 현재 요청은 별도 token을 발급해 계속 진행할 수 있다.
        self._logger.warning(
            "SecuXperAI token 갱신 대기 시간 초과; 로컬에서 token 복구를 시도합니다",
        )
        return await self._replace_without_lock(
            expected_token=expected_token,
            try_refresh=try_refresh,
        )

    async def _shared_token(self) -> str | None:
        try:
            access_token = await self._token_store.get()
        except Exception:
            # Valkey 장애 중에만 마지막으로 확인한 메모리 token을 사용한다.
            self._logger.exception(
                "SecuXperAI access token Valkey 조회 실패",
            )
            return self._memory_token

        # 조회가 성공했다면 Valkey 값을 source of truth로 사용한다.
        # Management가 key를 삭제한 경우 메모리 token도 폐기한다.
        self._memory_token = access_token
        return access_token

    async def _save_token(self, access_token: str) -> None:
        self._memory_token = access_token
        try:
            await self._token_store.save(access_token)
        except Exception:
            # 여러 token이 동시에 유효하므로 Valkey 장애 중에는 발급한
            # token을 현재 프로세스 메모리에서 계속 사용할 수 있다.
            self._logger.exception(
                "SecuXperAI access token Valkey 저장 실패",
            )

    @staticmethod
    def _was_replaced(
        current_token: str | None,
        expected_token: str | None,
    ) -> bool:
        if expected_token is None:
            return current_token is not None
        return current_token is not None and current_token != expected_token

    @staticmethod
    def _owner_id() -> str:
        return f"pipeline:{os.getpid()}:{uuid4().hex}"
