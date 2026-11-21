#!/usr/bin/env python3
"""Per-service service-gate counters (Inline UDS + Mirror SHM).

Writes /run/aivax/service_gate_stats.json (override with AIVAX_GATE_STATS_PATH).
Safe across multiple worker processes via fcntl.flock merge.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

GATE_MODES = (
    "inspect_on",
    "inspect_off",
    "block",
    "redirect",
    "expired",
)

_DEFAULT_PATH = "/run/aivax/service_gate_stats.json"

_local_lock = threading.Lock()
_local: Dict[str, Dict[str, int]] = {}  # service -> mode -> count
_local_total: Dict[str, int] = {m: 0 for m in GATE_MODES}
_flush_thread: Optional[threading.Thread] = None
_flush_stop = threading.Event()


def _stats_path() -> str:
    return str(os.environ.get("AIVAX_GATE_STATS_PATH") or _DEFAULT_PATH).strip() or _DEFAULT_PATH


def record_service_gate(service_key: str, mode: str) -> None:
    """Increment in-process counters (flushed periodically to disk)."""
    key = str(service_key or "unknown").strip().lower() or "unknown"
    m = str(mode or "").strip().lower()
    if m not in GATE_MODES:
        m = "inspect_on"
    with _local_lock:
        bucket = _local.setdefault(key, {x: 0 for x in GATE_MODES})
        bucket[m] = int(bucket.get(m) or 0) + 1
        _local_total[m] = int(_local_total.get(m) or 0) + 1


def snapshot_local() -> Dict[str, Any]:
    with _local_lock:
        by_service = {
            svc: {m: int(counts.get(m) or 0) for m in GATE_MODES}
            for svc, counts in _local.items()
        }
        totals = {m: int(_local_total.get(m) or 0) for m in GATE_MODES}
    return {
        "ts": time.time(),
        "by_service": by_service,
        "total": totals,
    }


def _merge_into(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    src_svc = src.get("by_service") if isinstance(src.get("by_service"), dict) else {}
    dst_svc = dst.setdefault("by_service", {})
    if not isinstance(dst_svc, dict):
        dst["by_service"] = {}
        dst_svc = dst["by_service"]
    for svc, counts in src_svc.items():
        if not isinstance(counts, dict):
            continue
        bucket = dst_svc.setdefault(str(svc), {m: 0 for m in GATE_MODES})
        for m in GATE_MODES:
            bucket[m] = int(bucket.get(m) or 0) + int(counts.get(m) or 0)
    src_total = src.get("total") if isinstance(src.get("total"), dict) else {}
    dst_total = dst.setdefault("total", {m: 0 for m in GATE_MODES})
    if not isinstance(dst_total, dict):
        dst["total"] = {m: 0 for m in GATE_MODES}
        dst_total = dst["total"]
    for m in GATE_MODES:
        dst_total[m] = int(dst_total.get(m) or 0) + int(src_total.get(m) or 0)


def flush_to_disk(path: str = "") -> None:
    """Merge this process's delta into the shared stats file, then clear local delta."""
    out_path = str(path or _stats_path()).strip()
    if not out_path:
        return
    with _local_lock:
        if not _local and not any(_local_total.values()):
            return
        delta = {
            "by_service": {
                svc: {m: int(c.get(m) or 0) for m in GATE_MODES} for svc, c in _local.items()
            },
            "total": {m: int(_local_total.get(m) or 0) for m in GATE_MODES},
        }
        _local.clear()
        for m in GATE_MODES:
            _local_total[m] = 0

    try:
        parent = os.path.dirname(os.path.abspath(out_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Exclusive lock file beside the stats json
        lock_path = out_path + ".lock"
        with open(lock_path, "a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                existing: Dict[str, Any] = {
                    "by_service": {},
                    "total": {m: 0 for m in GATE_MODES},
                }
                if os.path.isfile(out_path):
                    try:
                        with open(out_path, encoding="utf-8") as fh:
                            loaded = json.load(fh)
                        if isinstance(loaded, dict):
                            existing = loaded
                    except Exception:
                        logging.debug("service_gate_stats load failed; starting fresh", exc_info=True)
                if not isinstance(existing.get("by_service"), dict):
                    existing["by_service"] = {}
                if not isinstance(existing.get("total"), dict):
                    existing["total"] = {m: 0 for m in GATE_MODES}
                _merge_into(existing, delta)
                existing["ts"] = time.time()
                existing["modes"] = list(GATE_MODES)
                tmp = out_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(existing, fh, ensure_ascii=False, separators=(",", ":"))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, out_path)
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        logging.exception("service_gate_stats flush failed path=%s", out_path)
        # Re-queue delta so a transient flock/disk error does not drop counts.
        with _local_lock:
            for svc, counts in delta.get("by_service", {}).items():
                bucket = _local.setdefault(svc, {m: 0 for m in GATE_MODES})
                for m in GATE_MODES:
                    bucket[m] = int(bucket.get(m) or 0) + int(counts.get(m) or 0)
            for m in GATE_MODES:
                _local_total[m] = int(_local_total.get(m) or 0) + int(
                    (delta.get("total") or {}).get(m) or 0
                )


def read_stats(path: str = "") -> Dict[str, Any]:
    """Read shared stats file (flush local first for same-process freshness)."""
    flush_to_disk(path)
    out_path = str(path or _stats_path()).strip()
    empty = {
        "ts": time.time(),
        "by_service": {},
        "total": {m: 0 for m in GATE_MODES},
        "modes": list(GATE_MODES),
    }
    if not out_path or not os.path.isfile(out_path):
        return empty
    try:
        with open(out_path, encoding="utf-8") as fh:
            obj = json.load(fh)
        if not isinstance(obj, dict):
            return empty
        obj.setdefault("modes", list(GATE_MODES))
        obj.setdefault("by_service", {})
        obj.setdefault("total", {m: 0 for m in GATE_MODES})
        return obj
    except Exception:
        logging.exception("service_gate_stats read failed path=%s", out_path)
        return empty


def start_flush_thread(interval_s: float = 1.0) -> None:
    """Daemon thread: periodic flush (call once from engine main / shm parent)."""
    global _flush_thread
    if _flush_thread is not None and _flush_thread.is_alive():
        return

    def _loop() -> None:
        while not _flush_stop.wait(max(0.2, float(interval_s))):
            try:
                flush_to_disk()
            except Exception:
                logging.exception("service_gate_stats flush thread error")

    _flush_stop.clear()
    _flush_thread = threading.Thread(target=_loop, name="service-gate-stats", daemon=True)
    _flush_thread.start()


def stop_flush_thread() -> None:
    _flush_stop.set()
    flush_to_disk()
