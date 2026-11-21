#!/usr/bin/env python3
"""Unit tests for ops classify helpers used by aivax-ops."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "mirror"))

from ops_classify import (  # noqa: E402
    ascii_bar,
    classify_ai_service,
    classify_http_version,
    imbalance_ratio,
)


class TestOpsClassify(unittest.TestCase):
    def test_http_version(self) -> None:
        self.assertEqual(classify_http_version("1.1"), "1.1")
        self.assertEqual(classify_http_version("2"), "2")
        self.assertEqual(classify_http_version("http/2"), "2")

    def test_ai_service_hosts(self) -> None:
        self.assertEqual(classify_ai_service("chatgpt.com"), "chatgpt")
        self.assertEqual(classify_ai_service("gemini.google.com"), "gemini")
        self.assertEqual(
            classify_ai_service("geminiweb-pa.clients6.google.com", "/v1/processSession"),
            "gemini",
        )
        self.assertEqual(classify_ai_service("ogads-pa.clients6.google.com"), "other")
        self.assertEqual(
            classify_ai_service("accounts.google.com", "/ListAccounts?gpsia=1"),
            "gemini",
        )
        self.assertEqual(classify_ai_service("accounts.google.com", "/signin"), "other")
        self.assertEqual(classify_ai_service("claude.ai"), "claude")
        self.assertEqual(classify_ai_service("www.genspark.ai", "/api/agent/ask_proxy"), "genspark")
        self.assertEqual(classify_ai_service("notgenspark.ai"), "other")
        self.assertEqual(classify_ai_service("unknown.example"), "other")
        self.assertEqual(
            classify_ai_service(
                "www.google.com",
                "/async/folif?q=What+the+hell...&udm=50&sclient=aim-smartcompose",
            ),
            "gemini",
        )
        self.assertEqual(
            classify_ai_service("www.google.com", "/search?q=cats&udm=50"),
            "gemini",
        )
        self.assertEqual(classify_ai_service("www.google.com", "/search?q=cats"), "other")
        self.assertEqual(
            classify_ai_service("www.google.com", "/async/folsrch?q=cats"),
            "other",
        )
        self.assertEqual(classify_ai_service("gemini.google.com", "/app"), "gemini")

    def test_imbalance_and_bar(self) -> None:
        unbalanced, share, idx = imbalance_ratio([99, 1], 0.80)
        self.assertTrue(unbalanced)
        self.assertEqual(idx, 0)
        self.assertGreaterEqual(share, 0.80)
        balanced, _, _ = imbalance_ratio([50, 50], 0.80)
        self.assertFalse(balanced)
        self.assertEqual(ascii_bar(0.5, width=10), "[#####-----]")


if __name__ == "__main__":
    unittest.main()
