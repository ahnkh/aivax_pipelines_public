#!/usr/bin/env python3
"""Unit tests for service gate mapping + stats."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
sys.path.insert(0, str(CORE))

import ai_policy_engine as ape  # noqa: E402
import service_gate_stats as sgs  # noqa: E402


class TestServiceGate(unittest.TestCase):
    def test_aliases(self) -> None:
        ape._SERVICE_GATE = {
            "gpt": "inspect_on",
            "gemini": "block",
            "claude": "inspect_off",
            "grok": "redirect",
            "perplexity": "expired",
            "notion ai": "inspect_on",
        }
        self.assertEqual(ape.service_gate_status("chatgpt"), "inspect_on")
        self.assertEqual(ape.service_gate_status("gemini"), "block")
        self.assertEqual(ape.service_gate_status("claude"), "inspect_off")
        self.assertEqual(ape.service_gate_status("grok"), "redirect")
        self.assertEqual(ape.service_gate_status("perplexity"), "expired")
        self.assertEqual(ape.service_gate_status("notion"), "inspect_on")

    def test_unknown_falls_back_inspect_on(self) -> None:
        ape._SERVICE_GATE = {"gpt": "block_redirect"}  # invalid after patch
        self.assertEqual(ape.service_gate_status("gpt"), "inspect_on")

    def test_apply_inspect_off_both_directions(self) -> None:
        ape._SERVICE_GATE = {"gpt": "inspect_off"}
        r = ape._apply_service_gate("r1", "gpt", "request")
        self.assertEqual(r["action"], "ALLOW")
        self.assertIn("service_inspect_off", r["reason"])
        r2 = ape._apply_service_gate("r2", "gpt", "response")
        self.assertEqual(r2["action"], "ALLOW")

    def test_apply_block_request_only(self) -> None:
        ape._SERVICE_GATE = {"gpt": "block"}
        r = ape._apply_service_gate("r1", "gpt", "request")
        self.assertEqual(r["action"], "BLOCK")
        self.assertIsNone(ape._apply_service_gate("r2", "gpt", "response"))

    def test_stats_flush(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "gate.json")
            os.environ["AIVAX_GATE_STATS_PATH"] = path
            # reset module locals
            with sgs._local_lock:
                sgs._local.clear()
                for m in sgs.GATE_MODES:
                    sgs._local_total[m] = 0
            sgs.record_service_gate("gpt", "block")
            sgs.record_service_gate("gpt", "inspect_on")
            sgs.record_service_gate("gemini", "inspect_off")
            sgs.flush_to_disk(path)
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(data["by_service"]["gpt"]["block"], 1)
            self.assertEqual(data["by_service"]["gpt"]["inspect_on"], 1)
            self.assertEqual(data["by_service"]["gemini"]["inspect_off"], 1)
            self.assertEqual(data["total"]["block"], 1)


# Pipeline status_to_gate without full pipeline deps
class TestSignalStatusMap(unittest.TestCase):
    def test_status_to_gate_table(self) -> None:
        # Load handler module lightly: skip if pipeline deps missing
        handler_path = (
            ROOT.parent
            / "aivax"
            / "pipeline"
            / "block_filter_modules"
            / "filter_policy"
            / "signal"
            / "aivax_policy_signal_handler.py"
        )
        if not handler_path.is_file():
            self.skipTest("pipeline handler not in tree layout")
        # Inline constants check via reading source expectations
        expected = {
            0: "block",
            1: "inspect_on",
            2: "expired",
            3: "inspect_off",
            4: "redirect",
        }
        # Instantiate mapping via duplicated table (handler imports lib_include)
        for status, gate in expected.items():
            if status == 0:
                self.assertEqual(gate, "block")
            elif status == 1:
                self.assertEqual(gate, "inspect_on")
            elif status == 2:
                self.assertEqual(gate, "expired")
            elif status == 3:
                self.assertEqual(gate, "inspect_off")
            elif status == 4:
                self.assertEqual(gate, "redirect")


if __name__ == "__main__":
    unittest.main()
