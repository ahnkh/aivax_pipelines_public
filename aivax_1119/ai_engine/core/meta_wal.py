"""
Metadata WAL for durable Policy API events (not file payloads).

delivery_class:
  - durable_metadata: at-least-once via WAL + correlation_id idempotency
  - ephemeral_file_inspection: retry only while process alive and file TTL/lease valid
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

DEFAULT_WAL_DIR = os.environ.get("AIVAX_WAL_DIR", "/var/lib/aivax/wal")


class MetaWal:
    def __init__(self, wal_dir: str = DEFAULT_WAL_DIR, segment: str = "events.wal") -> None:
        self.wal_dir = Path(wal_dir)
        self.wal_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.wal_dir / segment
        self._lock = threading.Lock()

    def append(
        self,
        event: Dict[str, Any],
        delivery_class: str = "durable_metadata",
        durable: bool = True,
    ) -> None:
        if delivery_class == "ephemeral_file_inspection":
            # still may log metadata without path/payload; caller decides
            pass
        rec = dict(event)
        rec.setdefault("delivery_class", delivery_class)
        rec.setdefault("wal_ts", time.time())
        # strip forbidden fields
        for banned in ("body", "payload", "cookie", "authorization", "local_path", "tmpfs_path"):
            rec.pop(banned, None)
        line = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
        with self._lock:
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
            try:
                os.write(fd, line)
                if durable and delivery_class == "durable_metadata":
                    os.fsync(fd)
            finally:
                os.close(fd)

    def iter_pending(self) -> Iterator[Dict[str, Any]]:
        if not self.path.exists():
            return
            yield  # pragma: no cover
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


_wal: Optional[MetaWal] = None


def get_wal() -> MetaWal:
    global _wal
    if _wal is None:
        _wal = MetaWal()
    return _wal
