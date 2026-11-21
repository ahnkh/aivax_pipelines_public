"""SecuXperAI 독립 adapter 실행 설정."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SecuxperaiSettings:
    base_url: str
    filter_path: str
    token_path: str
    refresh_path: str
    team_uuid: str
    client_no: str
    site_code: str
    sync_mode: str
    token_key: str
    token_lock_key: str
    token_lock_ttl_seconds: int
    token_wait_seconds: int
    request_timeout_seconds: int
    connect_timeout_seconds: int
    max_connections: int
    max_keepalive_connections: int

    @property
    def filter_url(self) -> str:
        return self._url(self.filter_path)

    @property
    def token_url(self) -> str:
        return self._url(self.token_path)

    @property
    def refresh_url(self) -> str:
        return self._url(self.refresh_path)

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    @classmethod
    def from_runtime_settings(
        cls,
        settings: Any,
    ) -> "SecuxperaiSettings":
        if isinstance(settings, Mapping):
            values = settings.get("slm_pipeline_filter_module", settings)
            return cls(
                base_url=str(values.get("secuxperai_base_url", "")),
                filter_path=str(
                    values.get("secuxperai_filter_path", "/chat/filter/"),
                ),
                token_path=str(
                    values.get("secuxperai_token_path", "/auth/token"),
                ),
                refresh_path=str(
                    values.get(
                        "secuxperai_refresh_path",
                        "/auth/token/refresh",
                    ),
                ),
                team_uuid=str(values.get("secuxperai_team_uuid", "")),
                client_no=str(values.get("secuxperai_client_no", "")),
                site_code=str(
                    values.get("secuxperai_site_code", "test-wt"),
                ),
                sync_mode=str(
                    values.get("secuxperai_sync_mode", "true"),
                ),
                token_key=str(
                    values.get(
                        "secuxperai_token_key",
                        "aivax:secuxperai:access-token:v1",
                    ),
                ),
                token_lock_key=str(
                    values.get(
                        "secuxperai_token_lock_key",
                        "aivax:secuxperai:access-token-lock:v1",
                    ),
                ),
                token_lock_ttl_seconds=int(
                    values.get("secuxperai_token_lock_ttl_seconds", 10),
                ),
                token_wait_seconds=int(
                    values.get("secuxperai_token_wait_seconds", 2),
                ),
                request_timeout_seconds=int(
                    values.get("secuxperai_request_timeout_seconds", 10),
                ),
                connect_timeout_seconds=int(
                    values.get("secuxperai_connect_timeout_seconds", 5),
                ),
                max_connections=int(
                    values.get("http_max_connections", 200),
                ),
                max_keepalive_connections=int(
                    values.get("http_max_keepalive", 50),
                ),
            )

        return cls(
            base_url=settings.secuxperai_base_url,
            filter_path=settings.secuxperai_filter_path,
            token_path=settings.secuxperai_token_path,
            refresh_path=settings.secuxperai_refresh_path,
            team_uuid=settings.secuxperai_team_uuid,
            client_no=settings.secuxperai_client_no,
            site_code=settings.secuxperai_site_code,
            sync_mode=settings.secuxperai_sync_mode,
            token_key=settings.secuxperai_token_key,
            token_lock_key=settings.secuxperai_token_lock_key,
            token_lock_ttl_seconds=(settings.secuxperai_token_lock_ttl_seconds),
            token_wait_seconds=(settings.secuxperai_token_wait_seconds),
            request_timeout_seconds=(settings.secuxperai_request_timeout_seconds),
            connect_timeout_seconds=(settings.secuxperai_connect_timeout_seconds),
            max_connections=settings.slm_http_max_connections,
            max_keepalive_connections=settings.slm_http_max_keepalive,
        )
