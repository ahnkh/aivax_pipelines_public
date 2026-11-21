"""JSON 파일 기반 AIVAX 실행 설정 adapter."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from src.domain.config_aivax.errors import AivaxConfigDomainError
from src.domain.config_aivax.models import AivaxConfig, AivaxFilters, AivaxMode
from src.domain.config_aivax.ports import AivaxConfigPort


class JsonFileAivaxConfigAdapter(AivaxConfigPort):
    """프로세스 설정을 JSON 파일에서 한 번 읽어 캐시한다."""

    def __init__(
        self,
        mode_file: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self._mode_file = Path(mode_file)
        self._logger = logger or logging.getLogger("khan-python")
        self._cached_config: AivaxConfig | None = None
        self._cache_lock = threading.Lock()

    def get_config(self) -> AivaxConfig:
        if self._cached_config is not None:
            self._logger.debug("AIVAX Mode: %s", self._cached_config.mode)
            return self._cached_config
        with self._cache_lock:
            if self._cached_config is None:
                self._cached_config = self._load_config()
                self._logger.debug("AIVAX Mode: %s", self._cached_config.mode)
        return self._cached_config

    def reset_cache(self) -> None:
        """설정 재로딩과 테스트를 위해 프로세스 캐시를 비운다."""
        with self._cache_lock:
            self._cached_config = None

    def _load_config(self) -> AivaxConfig:
        try:
            with self._mode_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, TypeError) as error:
            self._logger.warning(
                "AIVAX config 파일을 읽을 수 없어 기본 설정을 사용합니다: %s",
                error,
            )
            config = AivaxConfig()
        else:
            try:
                config = self._to_config(payload)
            except AivaxConfigDomainError as error:
                self._logger.warning(
                    "유효하지 않은 AIVAX config이므로 기본 설정을 사용합니다: %s",
                    error,
                )
                config = AivaxConfig()

        return config

    @staticmethod
    def _to_config(payload: Any) -> AivaxConfig:
        defaults = AivaxConfig()
        if not isinstance(payload, dict):
            return defaults
        return AivaxConfig(
            schema_version=payload.get("schema_version", defaults.schema_version),
            mode=JsonFileAivaxConfigAdapter._to_mode(
                payload.get("mode", defaults.mode),
            ),
            updated_at=JsonFileAivaxConfigAdapter._to_datetime(
                payload.get("updated_at", defaults.updated_at),
                defaults.updated_at,
            ),
            source=payload.get("source", defaults.source),
            filters=AivaxFilters.from_payload(payload.get("filters", {})),
        )

    @staticmethod
    def _to_mode(value: Any) -> AivaxMode:
        return AivaxMode(str(value))

    @staticmethod
    def _to_datetime(value: Any, default: datetime) -> Any:
        if isinstance(value, datetime):
            return value
        if value is default:
            return default
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return value
