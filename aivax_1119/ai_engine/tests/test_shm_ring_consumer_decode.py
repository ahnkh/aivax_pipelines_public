#!/usr/bin/env python3
"""Regression tests for SHM eve JSON decoding."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for path in (_ROOT, _ROOT / "core", _ROOT / "mirror"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mirror.shm_ring_consumer import _decode_eve  # noqa: E402


class TestShmEveDecode(unittest.TestCase):
    def test_replaces_invalid_utf8_inside_request_body(self) -> None:
        raw = (
            b'{"request_id":"rid-1","host":"grok.com",'
            b'"request_body":"binary:\xba\xd0"}\n'
        )

        eve = _decode_eve(raw)

        self.assertEqual(eve["request_id"], "rid-1")
        self.assertEqual(eve["host"], "grok.com")
        self.assertEqual(eve["request_body"], "binary:\ufffd\ufffd")

    def test_preserves_valid_utf8(self) -> None:
        raw = '{"request_id":"rid-2","request_body":"안녕하세요"}\n'.encode()

        eve = _decode_eve(raw)

        self.assertEqual(eve["request_body"], "안녕하세요")


if __name__ == "__main__":
    unittest.main()
