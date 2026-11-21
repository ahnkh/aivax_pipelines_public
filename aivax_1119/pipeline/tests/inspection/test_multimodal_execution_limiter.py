"""멀티모달 파일/OCR 실행 개수 제한 계약."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from src.adapters.inspection_multimodal import LocalMultimodalInspectionAdapter
from src.application.services.inspection_multimodal import MultimodalInspectionService
from src.domain.inspection_multimodal.models import (
    DocumentSource,
    MultimodalInspectionOptions,
    MultimodalInspectionTarget,
    WatermarkCriteria,
)

_TARGETS = (
    MultimodalInspectionTarget(
        source=DocumentSource.from_path("/run/aivax/attachments/image"),
        file_name="image.png",
    ),
)


class _InspectionProbe:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0

    async def inspect(self, request):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return request


class _PolicyProvider:
    @staticmethod
    def get_policy():
        return object()


def _options() -> MultimodalInspectionOptions:
    return MultimodalInspectionOptions(
        bypass=False,
        read_timeout_seconds=30,
        unsupported_file_handling="block",
        oversized_file_handling="block",
        content_chunk_size=0,
        detail_reason_directory="/tmp",
        watermark_criteria=WatermarkCriteria(
            drm_enabled=False,
            drm_pattern="",
            drm_pattern_flags=0,
            drm_header_limit=0,
            ocr_enabled=False,
            ocr_patterns=(),
            ocr_pattern_flags=0,
            ocr_hit_threshold=0,
            ocr_pages=(),
        ),
    )


def test_file_inspection_respects_max_inflight() -> None:
    async def run() -> int:
        probe = _InspectionProbe()
        service = MultimodalInspectionService(
            probe,
            _PolicyProvider(),
            _options(),
            max_inflight=2,
        )
        await asyncio.gather(*(service.inspect(_TARGETS) for _ in range(6)))
        return probe.maximum

    assert asyncio.run(run()) == 2


class _BlockedInspection:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def inspect(self, request):
        self.calls += 1
        await self.release.wait()
        return request


def test_request_without_attachments_does_not_wait_for_file_slots() -> None:
    async def run() -> int:
        blocked = _BlockedInspection()
        service = MultimodalInspectionService(
            blocked,
            _PolicyProvider(),
            _options(),
            max_inflight=1,
        )
        holder = asyncio.create_task(service.inspect(_TARGETS))
        await asyncio.sleep(0)
        result = await asyncio.wait_for(service.inspect(()), timeout=1)
        assert result.files == ()
        blocked.release.set()
        await holder
        return blocked.calls

    assert asyncio.run(run()) == 1


def test_file_slot_wait_timeout_skips_instead_of_blocking_the_prompt() -> None:
    async def run() -> bool:
        blocked = _BlockedInspection()
        service = MultimodalInspectionService(
            blocked,
            _PolicyProvider(),
            _options(),
            max_inflight=1,
            acquire_timeout_seconds=0.05,
        )
        holder = asyncio.create_task(service.inspect(_TARGETS))
        await asyncio.sleep(0)
        result = await service.inspect(_TARGETS)
        blocked.release.set()
        await holder
        return result.unavailable

    assert asyncio.run(run()) is True


def test_ocr_respects_max_inflight() -> None:
    adapter = LocalMultimodalInspectionAdapter(ocr_max_inflight=2)
    lock = threading.Lock()
    active = 0
    maximum = 0

    def execute() -> None:
        nonlocal active, maximum
        adapter._run_ocr(None)

    def fake_ocr(_page) -> str:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return ""

    adapter._ocr_page = fake_ocr

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda _: execute(), range(6)))

    assert maximum == 2
