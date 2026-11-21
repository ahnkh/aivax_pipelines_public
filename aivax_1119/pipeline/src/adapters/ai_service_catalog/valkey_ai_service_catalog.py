"""Valkey 서비스 카탈로그를 읽고 마지막 정상 snapshot을 보관한다."""

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from valkey import Valkey


class ValkeyAiServiceCatalog:
    def __init__(
        self,
        client: Valkey,
        key: str,
        *,
        backup_path: Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._key = key
        self._backup_path = backup_path
        self._logger = logger or logging.getLogger("khan-python")
        self._names: dict[str, str] | None = None
        self._lock = threading.Lock()

    def name_for(self, service_id: int | None) -> str:
        if service_id is None:
            return ""
        return (self._names or {}).get(str(service_id), "")

    def refresh(self) -> int:
        with self._lock:
            try:
                raw = self._client.get(self._key)
                if raw is None:
                    raise ValueError(f"AI service catalog key is missing: {self._key}")
                names = self._parse(raw)
            except Exception:
                self._logger.warning(
                    "AI service catalog load failed; keeping last snapshot",
                    exc_info=True,
                )
                if self._backup_path is not None:
                    try:
                        self._names = self._parse(self._backup_path.read_bytes())
                    except FileNotFoundError:
                        pass
                    except Exception:
                        self._logger.exception("AI service catalog backup is invalid")
                return len(self._names or {})

            if self._backup_path is not None:
                self._save_backup(raw)
            self._names = names
            return len(names)

    @staticmethod
    def _parse(raw: bytes | str) -> dict[str, str]:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
            raise ValueError("AI service catalog requires version and services")
        services = payload.get("services")
        if not isinstance(services, dict) or not all(
            isinstance(key, str)
            and bool(key.strip())
            and key == key.strip()
            and isinstance(name, str)
            and bool(name.strip())
            for key, name in services.items()
        ):
            raise ValueError("AI service catalog services must map IDs to names")
        return {key: name.strip() for key, name in services.items()}

    def _save_backup(self, raw: bytes | str) -> None:
        path = self._backup_path
        assert path is not None
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
                temporary_path = Path(file.name)
                file.write(raw.encode("utf-8") if isinstance(raw, str) else raw)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
        except OSError:
            self._logger.exception("AI service catalog backup save failed")
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
