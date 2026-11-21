"""로컬 디렉터리로 파일을 전달하는 adapter."""

from __future__ import annotations

import logging
from datetime import datetime
from functools import partial
from pathlib import Path

from anyio.to_thread import run_sync

from src.core.file import LocalFileOperations
from src.domain.delivery_file.models import FileDeliveryRequest, FileDeliveryResult
from src.domain.delivery_file.ports import FileDeliveryPort


class LocalFileDeliveryAdapter(FileDeliveryPort):
    def __init__(
        self,
        base_directory: str | Path,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._base_directory = Path(base_directory)
        self._file_operations = LocalFileOperations()
        self._logger = logger or logging.getLogger("khan-python")

    async def deliver(self, request: FileDeliveryRequest) -> FileDeliveryResult:
        return await run_sync(partial(self._deliver_sync, request))

    def _deliver_sync(self, request: FileDeliveryRequest) -> FileDeliveryResult:
        source = request.source_path
        now = datetime.now().astimezone()
        destination_directory = self._base_directory / now.strftime("%Y%m%d/%H")
        original_name = Path(request.file_name).name
        name_path = Path(original_name)
        destination = destination_directory / (
            f"{name_path.stem}_{source.name}{name_path.suffix}"
            if original_name
            else source.name
        )
        try:
            if source.stat().st_size == 0:
                return FileDeliveryResult()
            destination_directory.mkdir(parents=True, exist_ok=True)
            if not self._file_operations.move(source, destination):
                return FileDeliveryResult()
            return FileDeliveryResult(
                delivered=True,
                destination=str(destination),
            )
        except OSError:
            self._logger.exception(
                "파일 전달 실패 source=%s destination=%s",
                source,
                destination,
            )
            return FileDeliveryResult()
