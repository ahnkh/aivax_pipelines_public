#!/usr/bin/env python3
"""resume_parent_store: Valkey cross-process handoff map."""

from __future__ import annotations

import os
import sys
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "core"))
sys.path.insert(0, os.path.join(ROOT, "adapters"))


class _Skip(Exception):
    pass


def _fresh_store(**kwargs):
    import resume_parent_store as rps

    rps.reset_for_tests()
    ok = rps.configure(**kwargs)
    return rps, ok


def test_memory_backend_roundtrip():
    rps, ok = _fresh_store(force_memory=True)
    assert ok is False
    assert rps.backend_name() == "memory"
    key = f"topic:conversation-turn-{uuid.uuid4()}"
    parent = f"parent-{uuid.uuid4()}"
    rps.put(key, parent)
    assert rps.get(key) == parent


def test_valkey_cross_l1_clear_still_hits_shared():
    """Simulate worker B: empty L1, Valkey still has the binding from worker A."""
    rps, ok = _fresh_store(
        host=os.environ.get("AIVAX_VALKEY_HOST", "127.0.0.1"),
        port=int(os.environ.get("AIVAX_VALKEY_PORT", "6379")),
        key_prefix=f"aivax:test_resume_parent:{uuid.uuid4().hex}:",
        ttl_seconds=120,
    )
    if not ok:
        raise _Skip("Valkey not reachable")
    key = f"topic:conversation-turn-{uuid.uuid4()}"
    parent = "140367930487968_81"
    rps.put(key, parent)
    rps.clear_local_for_tests()
    assert rps.get(key) == parent
    try:
        rps._client.delete(rps._vk_key(key))  # noqa: SLF001
    except Exception:
        pass


def test_chatgpt_engine_delegates_to_store():
    import resume_parent_store as rps
    from adapters import chatgpt_engine as ce

    rps.reset_for_tests()
    rps.configure(force_memory=True)
    key = f"topic:test-{uuid.uuid4()}"
    parent = f"fadaecb9-{uuid.uuid4()}"
    ce._resume_parent_put(key, parent)
    assert ce._resume_parent_get(key) == parent
    rps.clear_local_for_tests()
    assert ce._resume_parent_get(key) == ""


def test_register_stream_handoff_uses_store():
    import resume_parent_store as rps
    from adapters import chatgpt_engine as ce

    rps.reset_for_tests()
    ok = rps.configure(
        host=os.environ.get("AIVAX_VALKEY_HOST", "127.0.0.1"),
        port=int(os.environ.get("AIVAX_VALKEY_PORT", "6379")),
        key_prefix=f"aivax:test_resume_parent:{uuid.uuid4().hex}:",
        ttl_seconds=120,
    )
    if not ok:
        raise _Skip("Valkey not reachable")

    turn = str(uuid.uuid4())
    parent = "140367930487968_81"
    payload = (
        f'data: {{"type":"stream_handoff","stream_handoff":'
        f'{{"handle":"{turn}","conversation_id":"conv-{turn}"}}}}\n\n'
    ).encode()
    keys = ce.register_stream_handoff_parent(payload, parent)
    assert keys
    rps.clear_local_for_tests()
    assert ce._resume_parent_get(f"topic:{turn}") == parent
    assert ce._resume_parent_get(f"topic:conversation-turn-{turn}") == parent
    for k in keys:
        try:
            rps._client.delete(rps._vk_key(k))  # noqa: SLF001
        except Exception:
            pass


if __name__ == "__main__":
    import traceback

    failed = 0
    for name in sorted(globals()):
        if not name.startswith("test_"):
            continue
        try:
            globals()[name]()
            print("PASS", name)
        except _Skip as exc:
            print("SKIP", name, exc)
        except Exception:
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    raise SystemExit(failed)
