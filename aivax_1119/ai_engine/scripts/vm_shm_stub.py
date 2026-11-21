#!/usr/bin/env python3
"""Hold a small POSIX SHM ring so aivax-engine-mirror can start without Suricata."""
from __future__ import annotations
import json, os, signal, struct, sys, time
from multiprocessing import shared_memory

MAGIC = 0x41585652
VERSION = 2
HDR_SIZE = 256
CTRL_SIZE = 128
NAME = os.environ.get("AIVAX_SHM_NAME", "aivax_eve_ring").lstrip("/")
META = os.environ.get("AIVAX_SHM_META", "/tmp/aivax_eve_ring.meta.json")
N_RINGS = int(os.environ.get("AIVAX_SHM_STUB_RINGS", "2"))
N_SLOTS = int(os.environ.get("AIVAX_SHM_STUB_SLOTS", "64"))  # power of 2
SLOT_SIZE = int(os.environ.get("AIVAX_SHM_STUB_SLOT", "16384"))

def map_size(n_rings, n_slots, slot_size):
    return HDR_SIZE + n_rings * CTRL_SIZE + n_rings * n_slots * slot_size

def main() -> int:
    if N_SLOTS & (N_SLOTS - 1):
        print("n_slots must be power of 2", file=sys.stderr)
        return 2
    size = map_size(N_RINGS, N_SLOTS, SLOT_SIZE)
    # unlink stale
    try:
        stale = shared_memory.SharedMemory(name=NAME)
        stale.close(); stale.unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"warn unlink: {e}", file=sys.stderr)
    shm = shared_memory.SharedMemory(name=NAME, create=True, size=size)
    buf = shm.buf
    created = int(time.time())
    epoch = 1
    name_b = NAME.encode()[:64].ljust(64, b"\0")
    struct.pack_into(
        "<IIIIIIII64sQQ144s",
        buf, 0,
        MAGIC, VERSION, N_RINGS, N_SLOTS, SLOT_SIZE, HDR_SIZE, CTRL_SIZE, 0,
        name_b, created, epoch, b"\0" * 144,
    )
    meta = {
        "magic": f"0x{MAGIC:08x}",
        "version": VERSION,
        "shm_name": NAME,
        "n_rings": N_RINGS,
        "n_slots": N_SLOTS,
        "slot_size": SLOT_SIZE,
        "header_size": HDR_SIZE,
        "ring_ctrl_size": CTRL_SIZE,
        "slot_hdr_size": 16,
        "map_size": size,
        "created_unix": created,
        "producer_epoch": epoch,
        "stub": True,
    }
    with open(META, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    os.chmod(META, 0o640)
    print(f"shm stub ready name={NAME} rings={N_RINGS} slots={N_SLOTS} slot={SLOT_SIZE} map={size} meta={META}", flush=True)

    stop = False
    def _sig(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    while not stop:
        time.sleep(1)
    try:
        os.remove(META)
    except OSError:
        pass
    shm.close()
    try:
        shm.unlink()
    except FileNotFoundError:
        pass
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
