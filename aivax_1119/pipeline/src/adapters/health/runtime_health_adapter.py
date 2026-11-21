"""읽기용 Valkey 연결과 실제 로그 경로·파일 시스템을 진단한다."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from tempfile import TemporaryFile

from valkey.asyncio import Valkey

from src.application.use_cases.check_health import HealthProbe, HealthProbeError
from src.core.valkey.settings import ValkeySettings


class RuntimeHealthAdapter:
    def __init__(
        self,
        settings: ValkeySettings,
        *,
        log_directory: str,
        bulk_directory: str,
        cds_directory: str | None = None,
    ) -> None:
        self._settings = settings
        self._directories = {
            "logDirectory": Path(log_directory),
            "bulkDirectory": Path(bulk_directory),
        }
        if cds_directory is not None:
            self._directories["cdsDirectory"] = Path(cds_directory)

    def probes(self) -> dict[str, HealthProbe]:
        probes: dict[str, HealthProbe] = {
            "valkey": self._valkey,
            "disk": self._disk,
        }
        for name, path in self._directories.items():
            probes[name] = self._directory_probe(path)
        return probes

    @staticmethod
    def _directory_probe(path: Path) -> HealthProbe:
        async def probe() -> None:
            await asyncio.to_thread(RuntimeHealthAdapter._check_directory, path)

        return probe

    async def _valkey(self) -> None:
        settings = self._settings
        client = Valkey(
            host=settings.valkey_host,
            port=settings.valkey_port,
            db=settings.valkey_db,
            password=settings.valkey_password or None,
            socket_connect_timeout=min(
                settings.valkey_socket_connect_timeout_ms / 1000, 1.0
            ),
            socket_timeout=min(settings.valkey_socket_timeout_ms / 1000, 1.0),
            retry_on_timeout=False,
        )
        try:
            if not await client.ping():
                raise HealthProbeError("Valkey PING에 응답하지 않았습니다.")
        except TimeoutError:
            raise
        except Exception as error:
            raise HealthProbeError("Valkey 연결 또는 PING에 실패했습니다.") from error
        finally:
            await client.aclose()

    @staticmethod
    def _check_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        # A real write detects read-only mounts and full disks, even as root.
        # TemporaryFile closes and removes the probe on success or failure.
        with TemporaryFile(dir=path) as probe:
            probe.write(b"aivax-pipeline-health\n")
            probe.flush()

    async def _disk(self) -> float:
        return await asyncio.to_thread(self._check_disk)

    def _check_disk(self) -> float:
        used_percent = 0.0
        for path in self._directories.values():
            existing = path.resolve()
            while not existing.exists() and existing != existing.parent:
                existing = existing.parent
            usage = shutil.disk_usage(existing)
            # Like df/Management, reserved blocks are excluded from available space.
            denominator = usage.used + usage.free
            if denominator <= 0:
                raise HealthProbeError("디스크 용량을 확인할 수 없습니다.")
            used_percent = max(used_percent, round(usage.used / denominator * 100, 2))
        if used_percent >= 90:
            raise HealthProbeError(
                f"디스크 사용률이 임계치 이상입니다: {used_percent}%",
                used_percent=used_percent,
            )
        return used_percent
