#!/usr/bin/env python3
"""Account cache must not lose identity under concurrent writers.

Run from the ai_engine tree:

  cd /home/AIVAX/ai_engine   # or /home1/aivax/ai_engine
  PYTHONPATH=.:core python3 tests/test_account_lookup_concurrency.py -v

Stress (same file, extra threads/procs):

  AIVAX_ACCOUNT_STRESS_THREADS=32 AIVAX_ACCOUNT_STRESS_PROCS=8 \\
    PYTHONPATH=.:core python3 tests/test_account_lookup_concurrency.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from multiprocessing import Process, Queue
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "core"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import account_lookup  # noqa: E402


def _stress_threads() -> int:
    try:
        return max(8, int(os.environ.get("AIVAX_ACCOUNT_STRESS_THREADS", "16")))
    except ValueError:
        return 16


def _stress_procs() -> int:
    try:
        return max(2, int(os.environ.get("AIVAX_ACCOUNT_STRESS_PROCS", "4")))
    except ValueError:
        return 4


def _mp_writer(path: str, delay: float, uid: str, email: str, q: Queue) -> None:
    os.environ["AIVAX_ACCOUNT_CACHE_PATH"] = path
    account_lookup.configure(maxsize=128, ttl_seconds=60, shards=4)
    try:
        time.sleep(delay)
        account_lookup.set_account("gemini", "psid-mp", uid, email)
        q.put(("ok", account_lookup.get("gemini", "psid-mp")))
    except Exception as exc:  # pragma: no cover - surfaced to parent
        q.put(("err", str(exc)))
    finally:
        if account_lookup._shared is not None:
            account_lookup._shared.close()
            account_lookup._shared = None


class TestAccountLookupConcurrency(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["AIVAX_ACCOUNT_CACHE_PATH"] = os.path.join(
            self._tmp.name, "accounts.sqlite"
        )
        account_lookup.configure(maxsize=128, ttl_seconds=60, shards=4)

    def tearDown(self) -> None:
        if account_lookup._shared is not None:
            account_lookup._shared.close()
            account_lookup._shared = None
        os.environ.pop("AIVAX_ACCOUNT_CACHE_PATH", None)
        self._tmp.cleanup()

    def test_empty_write_does_not_wipe_email(self) -> None:
        account_lookup.set_account("claude", "org:1", "u1", "a@example.com")
        account_lookup.set_account("claude", "org:1", "u1", "")
        self.assertEqual(
            account_lookup.get("claude", "org:1"),
            ("u1", "a@example.com"),
        )

    def test_first_email_wins_on_conflict(self) -> None:
        account_lookup.set_account("claude", "org:1", "u1", "a@example.com")
        account_lookup.set_account("claude", "org:1", "u1", "b@example.com")
        self.assertEqual(
            account_lookup.get("claude", "org:1"),
            ("u1", "a@example.com"),
        )

    def test_replace_overwrites_conflicting_email(self) -> None:
        account_lookup.set_account("claude", "org:1", "u1", "support@anthropic.com")
        account_lookup.set_account(
            "claude", "org:1", "u1", "alice@corp.com", replace=True
        )
        self.assertEqual(
            account_lookup.get("claude", "org:1"),
            ("u1", "alice@corp.com"),
        )

    def test_wait_for_sees_other_thread(self) -> None:
        def _late_write() -> None:
            time.sleep(0.05)
            account_lookup.set_account(
                "gemini", "psid-1", "gaia-1", "user@example.com"
            )

        threading.Thread(target=_late_write, daemon=True).start()
        info = account_lookup.wait_for("gemini", ["psid-1"], timeout_ms=400)
        self.assertEqual(info, ("gaia-1", "user@example.com"))

    def test_prefer_identity_helper(self) -> None:
        self.assertEqual(
            account_lookup.prefer_identity("u1", "a@x.com", "", ""),
            ("u1", "a@x.com"),
        )
        self.assertEqual(
            account_lookup.prefer_identity("", "", "u2", "b@x.com"),
            ("u2", "b@x.com"),
        )

    def test_thread_storm_empty_write_keeps_email(self) -> None:
        n = _stress_threads()
        barrier = threading.Barrier(n)
        errors: list[str] = []

        def _worker(i: int) -> None:
            try:
                barrier.wait(timeout=5)
                if i % 4 == 0:
                    account_lookup.set_account(
                        "claude", "org:storm", "u-storm", "keep@example.com"
                    )
                else:
                    account_lookup.set_account("claude", "org:storm", "u-storm", "")
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertFalse(errors, errors)
        self.assertEqual(
            account_lookup.get("claude", "org:storm"),
            ("u-storm", "keep@example.com"),
        )

    def test_many_waiters_see_one_writer(self) -> None:
        """Chat POST wait_for vs profile HTML set_account — the usual launch bug."""
        n = _stress_threads()
        barrier = threading.Barrier(n + 1)
        hits: list[tuple[str, str] | None] = [None] * n

        def _waiter(i: int) -> None:
            barrier.wait(timeout=5)
            hits[i] = account_lookup.wait_for(
                "chatgpt", ["did:device-1"], timeout_ms=600
            )

        def _writer() -> None:
            barrier.wait(timeout=5)
            time.sleep(0.03)
            account_lookup.set_account(
                "chatgpt", "did:device-1", "user-1", "ship@example.com"
            )

        threads = [threading.Thread(target=_waiter, args=(i,)) for i in range(n)]
        threads.append(threading.Thread(target=_writer))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        missing = [i for i, h in enumerate(hits) if h != ("user-1", "ship@example.com")]
        self.assertEqual(missing, [], f"waiters missed identity: {missing[:8]}")

    def test_multiprocess_sqlite_wait_for(self) -> None:
        """SHM workers do not share RAM — profile on A must be visible to wait on B."""
        path = os.environ["AIVAX_ACCOUNT_CACHE_PATH"]
        q: Queue = Queue()
        proc = Process(
            target=_mp_writer,
            args=(path, 0.08, "gaia-mp", "mp@example.com", q),
        )
        proc.start()
        info = account_lookup.wait_for("gemini", ["psid-mp"], timeout_ms=800)
        proc.join(timeout=5)
        self.assertFalse(proc.is_alive())
        self.assertEqual(q.get(timeout=2)[0], "ok")
        self.assertEqual(info, ("gaia-mp", "mp@example.com"))

    def test_multiprocess_first_email_wins(self) -> None:
        path = os.environ["AIVAX_ACCOUNT_CACHE_PATH"]
        n = _stress_procs()
        q: Queue = Queue()
        procs = [
            Process(
                target=_mp_writer,
                args=(path, 0.01 * i, f"u{i}", f"user{i}@example.com", q),
            )
            for i in range(n)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=10)
        self.assertTrue(all(not p.is_alive() for p in procs))
        statuses = [q.get(timeout=2) for _ in procs]
        self.assertTrue(all(s[0] == "ok" for s in statuses), statuses)
        got = account_lookup.get("gemini", "psid-mp")
        self.assertIsNotNone(got)
        uid, email = got
        self.assertTrue(email.endswith("@example.com"))
        # Conflict policy: first non-empty email is sticky; later procs must not flip it
        # to a different address after it is visible.
        again = account_lookup.get("gemini", "psid-mp")
        self.assertEqual(again, got)
        self.assertEqual(uid.startswith("u") or uid.startswith("gaia"), True)


if __name__ == "__main__":
    unittest.main()
