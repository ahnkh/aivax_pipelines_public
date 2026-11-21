"""POSIX shm ring layout mirroring plugins/aivax-http-logger/aivax_shm_ring.h (v2)."""
from __future__ import annotations

import json
import mmap
import os
import struct
import time
import zlib
from multiprocessing import shared_memory
from typing import Optional

MAGIC = 0x41585652
VERSION = 2
HDR_SIZE = 256
CTRL_SIZE = 128
SLOT_HDR_SIZE = 16  # seq(8)+len(4)+crc(4)
META_DEFAULT = "/tmp/aivax_eve_ring.meta.json"
NAME_DEFAULT = "aivax_eve_ring"

# AivaxShmHeader packed: 8×u32 + name[64] + created + producer_epoch + pad[144]
_HDR_FMT = "<IIIIIIII64sQQ144s"
assert struct.calcsize(_HDR_FMT) == HDR_SIZE


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _attach_shared_memory(name: str) -> shared_memory.SharedMemory:
    """Attach to Suricata/C-owned POSIX shm without resource_tracker ownership.

    Python 3.13+ SharedMemory(track=False) skips register/unregister.
    Older Pythons: open then unregister (ignore KeyError if already gone).
    Manual unregister-after-register races with process exit and spam
    KeyError: '/aivax_eve_ring' in resource_tracker.
    """
    bare = name.lstrip("/")
    try:
        return shared_memory.SharedMemory(name=bare, track=False)
    except TypeError:
        # Pre-3.13: no track= kwarg
        from multiprocessing import resource_tracker

        shm = shared_memory.SharedMemory(name=bare)
        try:
            resource_tracker.unregister(shm._name, "shared_memory")
        except (KeyError, FileNotFoundError):
            pass
        return shm


class ShmRing:
    def __init__(self, shm_name: str = NAME_DEFAULT, meta_path: str = META_DEFAULT):
        self.shm_name = shm_name.lstrip("/")
        self.meta_path = meta_path
        self._shm: Optional[shared_memory.SharedMemory] = None
        self._mm: Optional[mmap.mmap] = None
        self.n_rings = 0
        self.n_slots = 0
        self.slot_size = 0
        self.map_size = 0
        self.producer_epoch = 0

    def open(self, timeout_s: float = 30.0) -> None:
        deadline = time.time() + timeout_s
        last_err: Optional[Exception] = None
        while time.time() < deadline:
            try:
                self._open_once()
                return
            except Exception as exc:  # noqa: BLE001 — retry until producer creates
                last_err = exc
                time.sleep(0.1)
        raise TimeoutError(f"shm ring not ready: {self.shm_name}: {last_err}")

    def _open_once(self) -> None:
        meta = None
        if os.path.exists(self.meta_path):
            with open(self.meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        self._shm = _attach_shared_memory(self.shm_name)
        buf = self._shm.buf
        (
            magic,
            version,
            n_rings,
            n_slots,
            slot_size,
            header_size,
            ctrl_size,
            _flags,
            _name,
            _created,
            producer_epoch,
            _pad,
        ) = struct.unpack_from(_HDR_FMT, buf, 0)
        if magic != MAGIC or version != VERSION:
            self._shm.close()
            self._shm = None
            raise ValueError(f"bad magic/version: {magic:#x}/{version} (need v{VERSION})")
        if header_size != HDR_SIZE or ctrl_size != CTRL_SIZE:
            self._shm.close()
            self._shm = None
            raise ValueError("header/ctrl size mismatch")
        expect = HDR_SIZE + n_rings * CTRL_SIZE + n_rings * n_slots * slot_size
        if meta and int(meta.get("map_size", expect)) != expect:
            pass
        if len(buf) < expect:
            self._shm.close()
            self._shm = None
            raise ValueError(f"map too small: {len(buf)} < {expect}")
        self.n_rings = n_rings
        self.n_slots = n_slots
        self.slot_size = slot_size
        self.map_size = expect
        self.producer_epoch = int(producer_epoch)

    def close(self) -> None:
        if self._shm is not None:
            self._shm.close()
            self._shm = None

    def _ctrl_off(self, ring_id: int) -> int:
        return HDR_SIZE + ring_id * CTRL_SIZE

    def _slot_off(self, ring_id: int, idx: int) -> int:
        return (
            HDR_SIZE
            + self.n_rings * CTRL_SIZE
            + ring_id * self.n_slots * self.slot_size
            + (idx % self.n_slots) * self.slot_size
        )

    def pop(self, ring_id: int) -> Optional[bytes]:
        if self._shm is None:
            return None
        buf = self._shm.buf
        coff = self._ctrl_off(ring_id)
        tail = struct.unpack_from("<Q", buf, coff + 64)[0]
        soff = self._slot_off(ring_id, tail)
        seq = struct.unpack_from("<Q", buf, soff)[0]
        if seq != tail + 1:
            return None  # empty or producer still writing
        (ln,) = struct.unpack_from("<I", buf, soff + 8)
        (crc,) = struct.unpack_from("<I", buf, soff + 12)
        if ln + SLOT_HDR_SIZE > self.slot_size:
            struct.pack_into("<Q", buf, soff, 0)
            struct.pack_into("<Q", buf, coff + 64, tail + 1)
            return None
        data = bytes(buf[soff + SLOT_HDR_SIZE : soff + SLOT_HDR_SIZE + ln])
        if _crc32(data) != crc:
            struct.pack_into("<Q", buf, soff, 0)
            struct.pack_into("<Q", buf, coff + 64, tail + 1)
            return None
        struct.pack_into("<Q", buf, soff, 0)
        struct.pack_into("<Q", buf, coff + 64, tail + 1)
        return data

    def push(self, ring_id: int, data: bytes) -> bool:
        """Test helper — single-threaded CAS substitute (use C producer under load)."""
        if self._shm is None:
            return False
        if len(data) + SLOT_HDR_SIZE > self.slot_size:
            return False
        buf = self._shm.buf
        coff = self._ctrl_off(ring_id)
        for _ in range(64):
            head = struct.unpack_from("<Q", buf, coff)[0]
            tail = struct.unpack_from("<Q", buf, coff + 64)[0]
            if head - tail >= self.n_slots:
                return False
            # Reserve then publish (mirrors C order for single producer tests).
            struct.pack_into("<Q", buf, coff, head + 1)
            soff = self._slot_off(ring_id, head)
            struct.pack_into("<I", buf, soff + 8, len(data))
            struct.pack_into("<I", buf, soff + 12, _crc32(data))
            buf[soff + SLOT_HDR_SIZE : soff + SLOT_HDR_SIZE + len(data)] = data
            struct.pack_into("<Q", buf, soff, head + 1)
            return True
        return False

    def depth(self, ring_id: int) -> int:
        if self._shm is None:
            return 0
        buf = self._shm.buf
        coff = self._ctrl_off(ring_id)
        head = struct.unpack_from("<Q", buf, coff)[0]
        tail = struct.unpack_from("<Q", buf, coff + 64)[0]
        return max(0, int(head - tail))
