"""SLM HTTP adapter에 필요한 실행 설정."""

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class SlmHttpSettings:
    endpoint: str
    request_timeout_seconds: int
    max_connections: int
    max_keepalive_connections: int
    connect_timeout_seconds: int

    @classmethod
    def from_runtime_settings(cls, settings: Any) -> "SlmHttpSettings":
        if isinstance(settings, Mapping):
            values = settings.get("slm_pipeline_filter_module", settings)
            return cls(
                endpoint=str(values.get("slm_url", "")),
                request_timeout_seconds=int(values.get("request_timeout", 10)),
                max_connections=int(values.get("http_max_connections", 200)),
                max_keepalive_connections=int(values.get("http_max_keepalive", 50)),
                connect_timeout_seconds=int(values.get("http_connect_timeout_s", 5)),
            )
        return cls(
            endpoint=settings.slm_url,
            request_timeout_seconds=settings.slm_request_timeout_seconds,
            max_connections=settings.slm_http_max_connections,
            max_keepalive_connections=settings.slm_http_max_keepalive,
            connect_timeout_seconds=settings.slm_http_connect_timeout_seconds,
        )
