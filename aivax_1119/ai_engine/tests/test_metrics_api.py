#!/usr/bin/env python3
"""Unit tests for Mirror metrics API (DPDK + pipeline)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "core"))

import metrics_api as m  # noqa: E402


class TestFlattenDpdk(unittest.TestCase):
    def test_nested_ethdev_stats(self) -> None:
        raw = {
            "/ethdev/stats": {
                "ipackets": 10,
                "ibytes": 100,
                "imissed": 2,
                "ierrors": 0,
                "rx_nombuf": 1,
            }
        }
        flat = m.flatten_dpdk_port_dict(raw)
        self.assertEqual(flat["ipackets"], 10)
        self.assertEqual(flat["imissed"], 2)


class TestReadDpdkPortStats(unittest.TestCase):
    def test_missing_socket(self) -> None:
        with mock.patch.object(m, "find_dpdk_telemetry_socket", return_value=None):
            out = m.read_dpdk_port_stats()
        self.assertFalse(out["valid"])
        self.assertEqual(out["reason"], "telemetry_socket_missing")

    def test_happy_path(self) -> None:
        payload = {
            "/ethdev/stats": {
                "ipackets": 1000,
                "ibytes": 8000,
                "imissed": 5,
                "ierrors": 0,
                "rx_nombuf": 0,
                "opackets": 0,
                "obytes": 0,
            }
        }
        with mock.patch.object(m, "find_dpdk_telemetry_socket", return_value="/tmp/fake.sock"):
            with mock.patch.object(m, "dpdk_telemetry_query", return_value=payload):
                out = m.read_dpdk_port_stats(port=0)
        self.assertTrue(out["valid"])
        self.assertEqual(out["rx_packets"], 1000)
        self.assertEqual(out["pmd_imissed"], 5)
        self.assertEqual(out["source"], "dpdk_telemetry")


class TestPipelineSnapshot(unittest.TestCase):
    def test_reads_consumer_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "shm_consumer_stats.json"
            path.write_text(
                json.dumps(
                    {
                        "ts": 1000.0,
                        "processed": 42,
                        "eps_avg": 1.5,
                        "ai_service": {"gemini": 3},
                        "http_version": {"1.1": 3},
                        "service_gate": {"total": {"inspect_on": 3}},
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(m, "SHM_CONSUMER_CANDIDATES", (str(path),)):
                with mock.patch.object(m, "EXPORT_STATS_CANDIDATES", ("",)):
                    with mock.patch.object(m, "GATE_STATS_PATH", str(Path(td) / "missing.json")):
                        snap = m.read_pipeline_snapshot()
            self.assertTrue(snap["valid"])
            self.assertEqual(snap["consumer"]["processed"], 42)
            self.assertEqual(snap["service_gate"]["total"]["inspect_on"], 3)


class TestCollectorDpdkPrefer(unittest.TestCase):
    def test_current_fills_port_and_pipeline(self) -> None:
        collector = m.MetricsCollector(interfaces=["missing0"])
        dpdk = {
            "valid": True,
            "source": "dpdk_telemetry",
            "telemetry": "/run/aivax/dpdk/rte/dpdk_telemetry.v2",
            "port_id": 0,
            "rx_packets": 10_000,
            "rx_bytes": 1_000_000,
            "rx_dropped": 20,
            "rx_errors": 0,
            "pmd_imissed": 20,
            "pmd_rx_nombuf": 0,
            "opackets": 0,
            "obytes": 0,
        }
        pipe = {
            "sampled_at": "2026-01-01T00:00:00Z",
            "sensor_boot_id": "x",
            "valid": True,
            "path": "/run/aivax/shm_consumer_stats.json",
            "age_s": 0.1,
            "processed": 7,
            "eps_instant": 1.0,
            "eps_avg": 0.5,
            "workers": 16,
            "n_rings": 16,
            "ring_processed": [1],
            "ring_errors": [0],
            "worker_processed": [1],
            "http_version": {"1.1": 7},
            "ai_service": {"gemini": 7},
            "service_gate": {"total": {"inspect_on": 7}},
            "export_valid": False,
            "export_path": "",
            "export": {},
            "consumer": {"processed": 7},
        }
        with mock.patch.object(collector, "_prefer_dpdk", True):
            with mock.patch.object(m, "read_dpdk_port_stats", return_value=dpdk):
                # Prime previous counters so second sample has rates.
                collector._prev = {
                    "ts": 1000.0,
                    "rx_packets": 9000,
                    "rx_bytes": 900_000,
                    "rx_dropped": 20,
                    "source": "dpdk_telemetry",
                }
                with mock.patch.object(collector, "pipeline", return_value=pipe):
                    with mock.patch.object(m.time, "time", return_value=1001.0):
                        cur = collector.current()
        self.assertTrue(cur["port"]["valid"])
        self.assertEqual(cur["port"]["source"], "dpdk_telemetry")
        self.assertAlmostEqual(cur["port"]["pps"], 1000.0, places=0)
        self.assertEqual(cur["drops"]["pmd_imissed_total"], 20)
        self.assertEqual(cur["drops"]["headline"]["drop_source"], "dpdk_imissed")
        self.assertTrue(cur["counter_support"]["dpdk_imissed"])
        self.assertEqual(cur["pipeline"]["processed"], 7)
        self.assertEqual(cur["pipeline"]["ai_service"]["gemini"], 7)

    def test_sysfs_fallback_when_dpdk_invalid(self) -> None:
        collector = m.MetricsCollector(interfaces=["lo"])
        with mock.patch.object(collector, "_prefer_dpdk", True):
            with mock.patch.object(
                m,
                "read_dpdk_port_stats",
                return_value={"valid": False, "reason": "telemetry_socket_missing"},
            ):
                with mock.patch.object(
                    collector,
                    "pipeline",
                    return_value={"valid": False, "processed": None, "consumer": {}},
                ):
                    cur = collector.current()
        # lo usually exists; if not, valid may be false — source must not claim dpdk success
        self.assertNotEqual(cur["port"]["source"], "dpdk_telemetry")
        self.assertIsNone(cur["drops"]["pmd_imissed_total"])


if __name__ == "__main__":
    unittest.main()
