#!/usr/bin/env python3
"""Attachment Lifecycle Manager — single-node owner of tmpfs create/lease/unlink.

No-Persistent-Disk: payload lives only on dedicated tmpfs. This process alone
may unlink. Writer / Publisher / Policy API only report state transitions.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("attachment_lifecycle")

DEFAULT_TMPFS = os.environ.get("AIVAX_ATTACH_TMPFS", "/run/aivax/attachments")
DEFAULT_WAL = os.environ.get("AIVAX_WAL_DIR", "/var/lib/aivax/wal")
DEFAULT_LOCK = os.environ.get("AIVAX_LIFECYCLE_LOCK", "/run/aivax/lifecycle.lock")
ADMISSION_PCT = float(os.environ.get("AIVAX_ATTACH_ADMISSION_PCT", "80"))
ABORT_PCT = float(os.environ.get("AIVAX_ATTACH_ABORT_PCT", "90"))


class State(str, Enum):
    CREATING = "CREATING"
    READY = "READY"
    INSPECTING = "INSPECTING"
    TERMINAL = "TERMINAL"
    UNLINKED = "UNLINKED"


@dataclass
class Attachment:
    attachment_id: str
    correlation_id: str
    state: State
    created_at: float
    updated_at: float
    size_so_far: int = 0
    expected_inspection: bool = True
    lease_generation: int = 0
    local_path: str = ""
    part_path: str = ""
    body_reason: str = ""
    recovery_observation: str = ""
    hash_algorithm: str = "sha256"
    hash_value: str = ""
    hash_scope: str = ""
    hashed_bytes: int = 0
    filename_present: bool = False
    filename_extension: str = ""
    filename_length: int = 0


class AttachmentLifecycleManager:
    """Single-instance manager with pidfile lock and lifecycle WAL."""

    CREATING_TTL = 120.0
    READY_TTL = 300.0
    INSPECTING_LEASE = 120.0
    TERMINAL_GRACE = 30.0

    def __init__(
        self,
        tmpfs_dir: str = DEFAULT_TMPFS,
        wal_dir: str = DEFAULT_WAL,
        lock_path: str = DEFAULT_LOCK,
    ) -> None:
        self.tmpfs_dir = Path(tmpfs_dir)
        self.wal_dir = Path(wal_dir)
        self.lock_path = Path(lock_path)
        self._lock_fd: Optional[int] = None
        self._mu = threading.RLock()
        self._items: Dict[str, Attachment] = {}
        self._reserved_bytes = 0
        self._stop = threading.Event()

    # ── single-instance lock ─────────────────────────────────────────────
    def acquire_leadership(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError(f"Lifecycle Manager already running: {exc}") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._lock_fd = fd

    def release_leadership(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    # ── tmpfs capacity ───────────────────────────────────────────────────
    def _tmpfs_stats(self) -> Tuple[int, int]:
        usage = shutil.disk_usage(str(self.tmpfs_dir))
        return usage.used, usage.total

    def admission_usage(self, extra_reserve: int = 0) -> float:
        used, total = self._tmpfs_stats()
        if total <= 0:
            return 100.0
        with self._mu:
            reserved = self._reserved_bytes + max(0, int(extra_reserve))
        return 100.0 * (used + reserved) / float(total)

    def can_admit(self, reserve_bytes: int) -> bool:
        return self.admission_usage(reserve_bytes) < ADMISSION_PCT

    # ── WAL (metadata only; READY/TERMINAL may group-commit) ─────────────
    def _wal_path(self) -> Path:
        self.wal_dir.mkdir(parents=True, exist_ok=True)
        return self.wal_dir / "lifecycle.wal"

    def _wal_append(self, att: Attachment, durable: bool = False) -> None:
        rec = {
            "ts": time.time(),
            "attachment_id": att.attachment_id,
            "correlation_id": att.correlation_id,
            "state": att.state.value,
            "created_at": att.created_at,
            "updated_at": att.updated_at,
            "size_so_far": att.size_so_far,
            "expected_inspection": att.expected_inspection,
            "lease_generation": att.lease_generation,
            "body_reason": att.body_reason,
            "recovery_observation": att.recovery_observation,
            "filename_present": att.filename_present,
            "filename_extension": att.filename_extension,
            "filename_length": att.filename_length,
            "hash_algorithm": att.hash_algorithm,
            "hash_value": att.hash_value,
            "hash_scope": att.hash_scope,
            "hashed_bytes": att.hashed_bytes,
            # intentionally omit local_path and payload
        }
        path = self._wal_path()
        line = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(str(path), flags, 0o640)
        try:
            os.write(fd, line)
            if durable:
                os.fsync(fd)
        finally:
            os.close(fd)

    # ── create / publish ─────────────────────────────────────────────────
    def begin_create(
        self,
        correlation_id: str,
        reserve_bytes: int = 0,
        filename: str = "",
    ) -> Attachment:
        if not self.can_admit(reserve_bytes):
            raise RuntimeError("tmpfs_admission_denied")
        if self.admission_usage(reserve_bytes) >= ABORT_PCT:
            raise RuntimeError("tmpfs_abort_threshold")

        aid = uuid.uuid4().hex
        part = self.tmpfs_dir / f"{aid}.part"
        now = time.time()
        ext = ""
        if filename:
            _, ext = os.path.splitext(filename)
            ext = ext.lstrip(".").lower()
        att = Attachment(
            attachment_id=aid,
            correlation_id=str(correlation_id or ""),
            state=State.CREATING,
            created_at=now,
            updated_at=now,
            part_path=str(part),
            filename_present=bool(filename),
            filename_extension=ext,
            filename_length=len(filename or ""),
        )
        with self._mu:
            self._items[aid] = att
            self._reserved_bytes += max(0, int(reserve_bytes))
        self._wal_append(att, durable=False)
        return att

    def write_chunk(self, attachment_id: str, data: bytes) -> None:
        with self._mu:
            att = self._items.get(attachment_id)
            if not att or att.state != State.CREATING:
                raise RuntimeError("invalid_state_for_write")
            part = att.part_path
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
        fd = os.open(part, flags, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        with self._mu:
            att = self._items[attachment_id]
            att.size_so_far += len(data)
            att.updated_at = time.time()

    def publish_ready(
        self,
        attachment_id: str,
        hash_value: str = "",
        hash_scope: str = "full_file",
        hashed_bytes: int = 0,
    ) -> Attachment:
        with self._mu:
            att = self._items[attachment_id]
            if att.state != State.CREATING:
                raise RuntimeError("invalid_state_for_ready")
            final = self.tmpfs_dir / att.attachment_id
            os.rename(att.part_path, str(final))  # atomic publish
            att.local_path = str(final)
            att.part_path = ""
            att.state = State.READY
            att.updated_at = time.time()
            if hash_value:
                att.hash_value = hash_value
                att.hash_scope = hash_scope
                att.hashed_bytes = hashed_bytes
            snap = att
        self._wal_append(snap, durable=True)
        return snap

    def issue_lease(self, attachment_id: str) -> Tuple[str, int, str]:
        with self._mu:
            att = self._items[attachment_id]
            if att.state != State.READY:
                raise RuntimeError("invalid_state_for_lease")
            att.lease_generation += 1
            att.state = State.INSPECTING
            att.updated_at = time.time()
            path = att.local_path
            gen = att.lease_generation
            snap = att
        self._wal_append(snap, durable=False)
        return path, gen, snap.attachment_id

    def complete(
        self,
        attachment_id: str,
        lease_generation: int,
        body_reason: str = "",
        success: bool = True,
    ) -> None:
        with self._mu:
            att = self._items.get(attachment_id)
            if not att:
                return
            if att.state in (State.TERMINAL, State.UNLINKED):
                return  # idempotent
            if att.state == State.INSPECTING and lease_generation and lease_generation != att.lease_generation:
                logger.warning("stale lease ack ignored aid=%s", attachment_id)
                return
            att.state = State.TERMINAL
            att.body_reason = body_reason or ("ok" if success else "inspect_failed")
            att.updated_at = time.time()
            snap = att
        self._wal_append(snap, durable=True)

    def unlink_terminal(self, attachment_id: str) -> None:
        with self._mu:
            att = self._items.get(attachment_id)
            if not att or att.state != State.TERMINAL:
                return
            path = att.local_path
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError as exc:
                logger.exception("unlink failed: %s", exc)
                return
        with self._mu:
            att = self._items[attachment_id]
            att.state = State.UNLINKED
            att.updated_at = time.time()
            att.local_path = ""
            snap = att
        self._wal_append(snap, durable=False)

    def policy_attachment(self, attachment_id: str) -> Dict[str, Any]:
        """Build attachments[] entry. id remains local_path for legacy contract."""
        with self._mu:
            att = self._items[attachment_id]
            return {
                "id": att.local_path,  # legacy path contract
                "storage_type": "ephemeral_tmpfs_path",
                "ephemeral": True,
                "expires_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(att.updated_at + self.INSPECTING_LEASE),
                ),
                "size": att.size_so_far,
                "name": att.attachment_id,
                "mime_type": "",
                "hash": {
                    "algorithm": att.hash_algorithm,
                    "value": att.hash_value,
                    "scope": att.hash_scope or "unknown",
                    "bytes": att.hashed_bytes,
                },
                "attachment_id": att.attachment_id,
                "delivery_class": "ephemeral_file_inspection",
            }

    # ── reconciliation on startup ────────────────────────────────────────
    def reconcile(self) -> Dict[str, int]:
        counts = {
            "missing_attachment": 0,
            "untracked_orphan": 0,
            "creating_part_present": 0,
            "cleanup_incomplete": 0,
            "process_crash": 0,
        }
        self.tmpfs_dir.mkdir(parents=True, exist_ok=True)
        wal_states: Dict[str, Dict[str, Any]] = {}
        wal = self._wal_path()
        if wal.exists():
            with open(wal, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    wal_states[rec["attachment_id"]] = rec

        on_disk = set()
        for name in os.listdir(self.tmpfs_dir):
            on_disk.add(name)

        for aid, rec in wal_states.items():
            state = rec.get("state")
            final = aid
            part = f"{aid}.part"
            now = time.time()
            if state in ("READY", "INSPECTING") and final not in on_disk:
                counts["missing_attachment"] += 1
                counts["process_crash"] += 1
                att = Attachment(
                    attachment_id=aid,
                    correlation_id=rec.get("correlation_id", ""),
                    state=State.TERMINAL,
                    created_at=float(rec.get("created_at", now)),
                    updated_at=now,
                    body_reason="process_crash",
                    recovery_observation="missing_attachment",
                    size_so_far=int(rec.get("size_so_far", 0)),
                )
                with self._mu:
                    self._items[aid] = att
                self._wal_append(att, durable=True)
            elif state == "CREATING" and part in on_disk:
                counts["creating_part_present"] += 1
                counts["process_crash"] += 1
                try:
                    os.unlink(str(self.tmpfs_dir / part))
                except OSError:
                    pass
                att = Attachment(
                    attachment_id=aid,
                    correlation_id=rec.get("correlation_id", ""),
                    state=State.TERMINAL,
                    created_at=float(rec.get("created_at", now)),
                    updated_at=now,
                    body_reason="process_crash",
                    recovery_observation="creating_part_present",
                )
                with self._mu:
                    self._items[aid] = att
                self._wal_append(att, durable=True)
            elif state == "TERMINAL" and final in on_disk:
                counts["cleanup_incomplete"] += 1
                try:
                    os.unlink(str(self.tmpfs_dir / final))
                except OSError:
                    pass

        for name in on_disk:
            aid = name[:-5] if name.endswith(".part") else name
            if aid not in wal_states:
                counts["untracked_orphan"] += 1
                try:
                    os.unlink(str(self.tmpfs_dir / name))
                except OSError:
                    pass
        return counts

    def sweep_once(self) -> None:
        now = time.time()
        with self._mu:
            items = list(self._items.values())
        for att in items:
            age = now - att.updated_at
            if att.state == State.CREATING and age > self.CREATING_TTL:
                att.body_reason = "creating_ttl"
                att.state = State.TERMINAL
                self._wal_append(att, durable=True)
                if att.part_path and os.path.exists(att.part_path):
                    try:
                        os.unlink(att.part_path)
                    except OSError:
                        pass
                att.state = State.UNLINKED
                self._wal_append(att, durable=False)
            elif att.state == State.READY and age > self.READY_TTL:
                self.complete(att.attachment_id, 0, body_reason="ready_ttl", success=False)
                self.unlink_terminal(att.attachment_id)
            elif att.state == State.INSPECTING and age > self.INSPECTING_LEASE:
                self.complete(att.attachment_id, att.lease_generation, body_reason="lease_timeout", success=False)
                self.unlink_terminal(att.attachment_id)
            elif att.state == State.TERMINAL and age > self.TERMINAL_GRACE:
                self.unlink_terminal(att.attachment_id)


# Module-level singleton used by file_upload_manager adapters
_manager: Optional[AttachmentLifecycleManager] = None


def get_manager() -> AttachmentLifecycleManager:
    global _manager
    if _manager is None:
        _manager = AttachmentLifecycleManager()
    return _manager


def configure(tmpfs_dir: str = DEFAULT_TMPFS, wal_dir: str = DEFAULT_WAL) -> AttachmentLifecycleManager:
    global _manager
    _manager = AttachmentLifecycleManager(tmpfs_dir=tmpfs_dir, wal_dir=wal_dir)
    return _manager


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="AIVAX Attachment Lifecycle Manager")
    p.add_argument("--tmpfs", default=DEFAULT_TMPFS)
    p.add_argument("--wal", default=DEFAULT_WAL)
    p.add_argument("--lock", default=DEFAULT_LOCK)
    p.add_argument("--reconcile-only", action="store_true")
    args = p.parse_args(argv)

    mgr = AttachmentLifecycleManager(args.tmpfs, args.wal, args.lock)
    mgr.acquire_leadership()
    try:
        counts = mgr.reconcile()
        logger.info("reconcile: %s", counts)
        if args.reconcile_only:
            return 0

        def _stop(*_a):
            mgr._stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        while not mgr._stop.is_set():
            mgr.sweep_once()
            mgr._stop.wait(5.0)
    finally:
        mgr.release_leadership()
    return 0


if __name__ == "__main__":
    sys.exit(main())
