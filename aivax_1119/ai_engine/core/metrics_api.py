#!/usr/bin/env python3
"""
Read-only Metrics REST API for external UI.

Cards:
  - port throughput (DPDK telemetry preferred, else NIC sysfs)
  - AI event pipeline (shm_consumer_stats + optional export stats)
  - layered drops + headline drop rate (validated sources only)

See docs/METRICS_API.md for the web developer contract.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

SENSOR_BOOT_ID = os.environ.get("AIVAX_SENSOR_BOOT_ID", str(int(time.time())))
HISTORY_MAX_POINTS = 10_000
MAX_FLOW_CARDINALITY = int(os.environ.get("AIVAX_METRICS_MAX_FLOWS", "200000"))
FLOW_SNAPSHOT_TTL = float(os.environ.get("AIVAX_METRICS_FLOW_TTL", "120"))
DPDK_PORT = int(os.environ.get("AIVAX_METRICS_DPDK_PORT", "0"))
DPDK_TELEMETRY_CANDIDATES = (
    "/run/aivax/dpdk/rte/dpdk_telemetry.v2",
    "/var/run/dpdk/rte/dpdk_telemetry.v2",
)
DPDK_STAT_KEYS = (
    "ipackets",
    "imissed",
    "ierrors",
    "rx_nombuf",
    "ibytes",
    "obytes",
    "opackets",
)
SHM_CONSUMER_CANDIDATES = (
    os.environ.get("AIVAX_SHM_CONSUMER_STATS", "").strip(),
    "/run/aivax/shm_consumer_stats.json",
    "/tmp/aivax_shm_consumer_stats.json",
)
EXPORT_STATS_CANDIDATES = (
    os.environ.get("AIVAX_EXPORT_STATS", "").strip(),
    "/home1/aivax/mirror_engine/log/aivax-export-stats.json",
    "/var/log/aivax/aivax-export-stats.json",
)
GATE_STATS_PATH = os.environ.get("AIVAX_GATE_STATS_PATH", "/run/aivax/service_gate_stats.json")


def _read_sys_counter(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _read_json_file(path: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _first_existing_json(candidates: Tuple[str, ...]) -> Tuple[Optional[Dict[str, Any]], str]:
    for path in candidates:
        if not path:
            continue
        obj = _read_json_file(path)
        if obj is not None:
            return obj, path
    fallback = next((p for p in candidates if p), "")
    return None, fallback


def _nic_stats(ifname: str) -> Dict[str, Any]:
    base = f"/sys/class/net/{ifname}/statistics"
    if not os.path.isdir(base):
        return {"if": ifname, "valid": False, "reason": "iface_missing"}
    rx_packets = _read_sys_counter(f"{base}/rx_packets")
    rx_bytes = _read_sys_counter(f"{base}/rx_bytes")
    rx_dropped = _read_sys_counter(f"{base}/rx_dropped")
    rx_errors = _read_sys_counter(f"{base}/rx_errors")
    return {
        "if": ifname,
        "valid": True,
        "rx_packets": rx_packets,
        "rx_bytes": rx_bytes,
        "rx_dropped": rx_dropped,
        "rx_errors": rx_errors,
        "source": "sysfs",
    }


def find_dpdk_telemetry_socket(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit if Path(explicit).exists() else None
    env = os.environ.get("DPDK_TELEMETRY") or os.environ.get("AIVAX_DPDK_TELEMETRY")
    if env and Path(env).exists():
        return env
    for p in DPDK_TELEMETRY_CANDIDATES:
        if Path(p).exists():
            return p
    for root in ("/run/aivax/dpdk", "/var/run/dpdk"):
        base = Path(root)
        if not base.is_dir():
            continue
        for sock in sorted(base.glob("*/dpdk_telemetry.v2")):
            return str(sock)
    return None


def dpdk_telemetry_query(cmd: str, path: str, timeout: float = 2.0) -> Any:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.recv(65536)  # banner
        payload = cmd if cmd.endswith("\0") else cmd + "\0"
        s.send(payload.encode())
        data = s.recv(65536).decode().rstrip("\0")
    finally:
        s.close()
    return json.loads(data)


def flatten_dpdk_port_dict(raw: Any) -> Dict[str, Any]:
    """Normalize DPDK telemetry nested shapes to a flat counter dict."""
    if isinstance(raw, dict) and any(k in raw for k in DPDK_STAT_KEYS):
        return raw
    if not isinstance(raw, dict):
        return {}
    for v in raw.values():
        if isinstance(v, dict):
            if any(k in v for k in DPDK_STAT_KEYS):
                return v
            for vv in v.values():
                if isinstance(vv, dict) and any(k in vv for k in DPDK_STAT_KEYS):
                    return vv
            if any(k in v for k in DPDK_STAT_KEYS):
                return v
    return {}


def _iget(d: Dict[str, Any], key: str) -> int:
    try:
        return int(d.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def read_dpdk_port_stats(
    sock_path: Optional[str] = None,
    port: int = DPDK_PORT,
) -> Dict[str, Any]:
    """Read cumulative DPDK ethdev stats for one port."""
    path = find_dpdk_telemetry_socket(sock_path)
    if not path:
        return {"valid": False, "reason": "telemetry_socket_missing", "source": "dpdk_telemetry"}
    try:
        raw = dpdk_telemetry_query(f"/ethdev/stats,{port}", path)
        flat = flatten_dpdk_port_dict(raw)
        if not flat:
            return {
                "valid": False,
                "reason": "unexpected_stats_shape",
                "source": "dpdk_telemetry",
                "telemetry": path,
                "port_id": port,
            }
        return {
            "valid": True,
            "source": "dpdk_telemetry",
            "telemetry": path,
            "port_id": port,
            "rx_packets": _iget(flat, "ipackets"),
            "rx_bytes": _iget(flat, "ibytes"),
            "rx_dropped": _iget(flat, "imissed"),
            "rx_errors": _iget(flat, "ierrors"),
            "pmd_imissed": _iget(flat, "imissed"),
            "pmd_rx_nombuf": _iget(flat, "rx_nombuf"),
            "opackets": _iget(flat, "opackets"),
            "obytes": _iget(flat, "obytes"),
        }
    except Exception as exc:  # noqa: BLE001 — surface to UI as invalid sample
        return {
            "valid": False,
            "reason": f"telemetry_error:{type(exc).__name__}",
            "source": "dpdk_telemetry",
            "telemetry": path,
            "port_id": port,
        }


def read_pipeline_snapshot(*, include_export: bool = True) -> Dict[str, Any]:
    """Load AI event pipeline counters from on-disk stats files."""
    consumer, consumer_path = _first_existing_json(SHM_CONSUMER_CANDIDATES)
    gate = _read_json_file(GATE_STATS_PATH)
    export = None
    export_path = ""
    if include_export:
        export, export_path = _first_existing_json(EXPORT_STATS_CANDIDATES)

    age_s = None
    if consumer and isinstance(consumer.get("ts"), (int, float)):
        age_s = max(0.0, time.time() - float(consumer["ts"]))

    # Prefer nested service_gate from consumer; fall back to dedicated file.
    service_gate = None
    if isinstance(consumer, dict) and isinstance(consumer.get("service_gate"), dict):
        service_gate = consumer["service_gate"]
    elif isinstance(gate, dict):
        service_gate = gate

    return {
        "valid": consumer is not None,
        "path": consumer_path,
        "age_s": age_s,
        "consumer": consumer or {},
        "service_gate": service_gate or {},
        "export_path": export_path,
        "export": export or {},
        "export_valid": export is not None,
    }


class MetricsCollector:
    def __init__(self, interfaces: Optional[List[str]] = None) -> None:
        env_ifs = os.environ.get("AIVAX_METRICS_IFS", "")
        if interfaces:
            self.interfaces = interfaces
        elif env_ifs:
            self.interfaces = [x for x in env_ifs.split(",") if x.strip()]
        else:
            self.interfaces = [
                os.environ.get("MIRROR_IF", "ens6f0"),
            ]
        self._lock = threading.RLock()
        self._prev: Dict[str, Any] = {}
        self._history: Deque[Dict[str, Any]] = deque(maxlen=HISTORY_MAX_POINTS)
        self._flow_snaps: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._ai_flow_bytes = 0
        self._ai_flow_packets = 0
        self._total_flow_bytes = 0
        self._total_flow_packets = 0
        self._export_queue_drop = 0
        self._file_aborted = 0
        self._metadata_only = 0
        self._suricata_capture_drop: Optional[int] = None
        self._counter_support = {
            "nic_sysfs": True,
            "dpdk_imissed": False,
            "dpdk_rx_nombuf": False,
            "suricata_capture_drop": False,
            "shm_consumer_stats": False,
        }
        self._prefer_dpdk = os.environ.get("AIVAX_METRICS_PREFER_DPDK", "1") not in (
            "0",
            "false",
            "False",
            "no",
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def note_application_counters(
        self,
        export_queue_drop: int = 0,
        file_aborted: int = 0,
        metadata_only: int = 0,
    ) -> None:
        with self._lock:
            self._export_queue_drop += export_queue_drop
            self._file_aborted += file_aborted
            self._metadata_only += metadata_only

    def observe_flow(
        self,
        flow_id: int,
        bytes_total: int,
        packets_total: int,
        ai_classified: bool,
    ) -> None:
        """Idempotent flow snapshot update keyed by (boot_id, flow_id)."""
        key = (SENSOR_BOOT_ID, int(flow_id))
        with self._lock:
            if len(self._flow_snaps) >= MAX_FLOW_CARDINALITY and key not in self._flow_snaps:
                self._evict_flows_locked()
            prev = self._flow_snaps.get(key)
            if prev is None:
                delta_b = max(0, int(bytes_total))
                delta_p = max(0, int(packets_total))
                self._flow_snaps[key] = {
                    "bytes": bytes_total,
                    "packets": packets_total,
                    "ai": bool(ai_classified),
                    "ts": time.time(),
                }
                self._total_flow_bytes += delta_b
                self._total_flow_packets += delta_p
                if ai_classified:
                    self._ai_flow_bytes += delta_b
                    self._ai_flow_packets += delta_p
                return

            delta_b = max(0, int(bytes_total) - int(prev["bytes"]))
            delta_p = max(0, int(packets_total) - int(prev["packets"]))
            was_ai = bool(prev.get("ai"))
            self._total_flow_bytes += delta_b
            self._total_flow_packets += delta_p
            if ai_classified and not was_ai:
                # Retroactively credit prior non-AI accounting once into AI totals.
                self._ai_flow_bytes += int(prev["bytes"]) + delta_b
                self._ai_flow_packets += int(prev["packets"]) + delta_p
            elif ai_classified or was_ai:
                self._ai_flow_bytes += delta_b
                self._ai_flow_packets += delta_p
            prev["bytes"] = bytes_total
            prev["packets"] = packets_total
            prev["ai"] = bool(ai_classified or was_ai)
            prev["ts"] = time.time()

    def _evict_flows_locked(self) -> None:
        now = time.time()
        expired = [k for k, v in self._flow_snaps.items() if now - v["ts"] > FLOW_SNAPSHOT_TTL]
        for k in expired:
            self._flow_snaps.pop(k, None)
        while len(self._flow_snaps) >= MAX_FLOW_CARDINALITY:
            oldest = min(self._flow_snaps.items(), key=lambda kv: kv[1]["ts"])[0]
            self._flow_snaps.pop(oldest, None)

    def _resolve_port_counters(self) -> Dict[str, Any]:
        """Prefer DPDK telemetry when available; else sum sysfs interfaces."""
        if self._prefer_dpdk:
            dpdk = read_dpdk_port_stats(port=DPDK_PORT)
            if dpdk.get("valid"):
                return {
                    "valid": True,
                    "source": "dpdk_telemetry",
                    "rx_packets": int(dpdk["rx_packets"]),
                    "rx_bytes": int(dpdk["rx_bytes"]),
                    "rx_dropped": int(dpdk["rx_dropped"]),
                    "rx_errors": int(dpdk.get("rx_errors") or 0),
                    "pmd_imissed": int(dpdk["pmd_imissed"]),
                    "pmd_rx_nombuf": int(dpdk["pmd_rx_nombuf"]),
                    "interfaces": [
                        {
                            "if": f"dpdk:{dpdk.get('port_id', 0)}",
                            "valid": True,
                            "source": "dpdk_telemetry",
                            "telemetry": dpdk.get("telemetry"),
                            "rx_packets": dpdk["rx_packets"],
                            "rx_bytes": dpdk["rx_bytes"],
                            "rx_dropped": dpdk["rx_dropped"],
                            "rx_errors": dpdk.get("rx_errors"),
                            "pmd_imissed": dpdk["pmd_imissed"],
                            "pmd_rx_nombuf": dpdk["pmd_rx_nombuf"],
                        }
                    ],
                    "dpdk": dpdk,
                }

        nics = [_nic_stats(i) for i in self.interfaces]
        nic_valid = any(n.get("valid") for n in nics)
        return {
            "valid": nic_valid,
            "source": "nic_sysfs_accepted",
            "rx_packets": sum(int(n.get("rx_packets") or 0) for n in nics if n.get("valid")),
            "rx_bytes": sum(int(n.get("rx_bytes") or 0) for n in nics if n.get("valid")),
            "rx_dropped": sum(int(n.get("rx_dropped") or 0) for n in nics if n.get("valid")),
            "rx_errors": sum(int(n.get("rx_errors") or 0) for n in nics if n.get("valid")),
            "pmd_imissed": None,
            "pmd_rx_nombuf": None,
            "interfaces": nics,
            "dpdk": None,
        }

    def _sample(self) -> Dict[str, Any]:
        now = time.time()
        port_raw = self._resolve_port_counters()
        rx_packets = int(port_raw.get("rx_packets") or 0)
        rx_bytes = int(port_raw.get("rx_bytes") or 0)
        rx_dropped = int(port_raw.get("rx_dropped") or 0)
        nic_valid = bool(port_raw.get("valid"))
        source = str(port_raw.get("source") or "unknown")
        is_dpdk = source == "dpdk_telemetry"

        with self._lock:
            prev = self._prev
            elapsed = max(1e-6, now - float(prev.get("ts", now)))
            # First sample: force zero rates so UI does not spike on boot.
            if "ts" not in prev:
                d_pkts = 0
                d_bytes = 0
                d_drop = 0
            else:
                d_pkts = max(0, rx_packets - int(prev.get("rx_packets", rx_packets)))
                d_bytes = max(0, rx_bytes - int(prev.get("rx_bytes", rx_bytes)))
                d_drop = max(0, rx_dropped - int(prev.get("rx_dropped", rx_dropped)))
            self._prev = {
                "ts": now,
                "rx_packets": rx_packets,
                "rx_bytes": rx_bytes,
                "rx_dropped": rx_dropped,
                "source": source,
            }
            self._evict_flows_locked()

            accepted = d_pkts
            drop = d_drop
            rate = None
            rate_valid = False
            if nic_valid and (accepted + drop) > 0:
                rate = 100.0 * drop / float(accepted + drop)
                rate_valid = True

            ai_ratio = None
            ratio_valid = False
            if self._total_flow_bytes > 0:
                ai_ratio = self._ai_flow_bytes / float(self._total_flow_bytes)
                ratio_valid = True

            self._counter_support["dpdk_imissed"] = is_dpdk and port_raw.get("pmd_imissed") is not None
            self._counter_support["dpdk_rx_nombuf"] = is_dpdk and port_raw.get("pmd_rx_nombuf") is not None
            self._counter_support["nic_sysfs"] = (not is_dpdk) and nic_valid

            if is_dpdk:
                accepted_source = "dpdk_ipackets"
                drop_source = "dpdk_imissed"
                drop_scope = "pmd_rx"
                formula_id = "delta_imissed/(delta_ipackets+delta_imissed)"
            else:
                accepted_source = "nic_sysfs_rx_packets"
                drop_source = "nic_sysfs_rx_dropped"
                drop_scope = "nic_ingress"
                formula_id = "delta_drop/(delta_accepted+delta_drop)"

            sample = {
                "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "sampled_at_unix": now,
                "sensor_boot_id": SENSOR_BOOT_ID,
                "port": {
                    "packets_total": rx_packets,
                    "bytes_total": rx_bytes,
                    "pps": d_pkts / elapsed,
                    "bps": (d_bytes * 8) / elapsed,
                    "mbps": (d_bytes * 8) / elapsed / 1e6,
                    "source": source,
                    "valid": nic_valid,
                    "interfaces": port_raw.get("interfaces") or [],
                    "window_seconds": elapsed,
                    "window_packets": d_pkts,
                    "window_bytes": d_bytes,
                    "window_dropped": d_drop,
                },
                "ai_classified_flow": {
                    "packets_total": self._ai_flow_packets,
                    "bytes_total": self._ai_flow_bytes,
                    "total_classified_flow_packets": self._total_flow_packets,
                    "total_classified_flow_bytes": self._total_flow_bytes,
                    "traffic_ratio": ai_ratio,
                    "ratio_comparable": ratio_valid,
                    "numerator_source": "suricata_ai_classified_flow",
                    "denominator_source": "suricata_total_classified_flow",
                    "ui_label": "AI-classified flow traffic",
                    "mixed_stream_note": "HTTP/2 mixed AI/non-AI streams may include non-AI wire bytes",
                    "valid": True,
                },
                "drops": {
                    "nic_ingress_drop_packets_total": None if is_dpdk else rx_dropped,
                    "nic_ingress_drop_rate": None if is_dpdk else rate,
                    "nic_ingress_drop_rate_valid": False if is_dpdk else rate_valid,
                    "pmd_imissed_total": port_raw.get("pmd_imissed") if is_dpdk else None,
                    "pmd_rx_nombuf_total": port_raw.get("pmd_rx_nombuf") if is_dpdk else None,
                    "pmd_imissed_rate": rate if is_dpdk else None,
                    "pmd_imissed_rate_valid": rate_valid if is_dpdk else False,
                    "window_imissed": d_drop if is_dpdk else None,
                    "suricata_capture_drop_total": self._suricata_capture_drop,
                    "export_queue_drop_total": self._export_queue_drop,
                    "file_aborted_total": self._file_aborted,
                    "metadata_only_total": self._metadata_only,
                    "headline": {
                        "capture_drop_packets_total": d_drop,
                        "capture_drop_source": drop_source,
                        "metric_valid": rate_valid,
                        "drop_rate": rate,
                        "formula_id": formula_id,
                        "drop_scope": drop_scope,
                        "accepted_source": accepted_source,
                        "drop_source": drop_source,
                        "counter_overlap_validated": True,
                    },
                },
                "counter_support": dict(self._counter_support),
                "flow_cardinality": len(self._flow_snaps),
            }
            self._history.append(sample)
            return sample

    def pipeline(self) -> Dict[str, Any]:
        snap = read_pipeline_snapshot(include_export=True)
        with self._lock:
            self._counter_support["shm_consumer_stats"] = bool(snap.get("valid"))
        consumer = snap.get("consumer") or {}
        return {
            "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sensor_boot_id": SENSOR_BOOT_ID,
            "valid": bool(snap.get("valid")),
            "path": snap.get("path"),
            "age_s": snap.get("age_s"),
            "processed": consumer.get("processed"),
            "eps_instant": consumer.get("eps_instant"),
            "eps_avg": consumer.get("eps_avg"),
            "workers": consumer.get("workers"),
            "n_rings": consumer.get("n_rings"),
            "ring_processed": consumer.get("ring_processed"),
            "ring_errors": consumer.get("ring_errors"),
            "worker_processed": consumer.get("worker_processed"),
            "http_version": consumer.get("http_version") or {},
            "ai_service": consumer.get("ai_service") or {},
            "service_gate": snap.get("service_gate") or {},
            "export_valid": bool(snap.get("export_valid")),
            "export_path": snap.get("export_path"),
            "export": snap.get("export") or {},
            "consumer": consumer,
        }

    def current(self) -> Dict[str, Any]:
        sample = self._sample()
        pipe = self.pipeline()
        sample["pipeline"] = pipe
        sample["counter_support"] = dict(self._counter_support)
        return sample

    def history(self, window_sec: int = 3600, step_sec: int = 5) -> Dict[str, Any]:
        window_sec = max(1, min(int(window_sec), 24 * 3600))
        step_sec = max(1, int(step_sec))
        with self._lock:
            points = list(self._history)
        if not points:
            return {"points": [], "window": window_sec, "step": step_sec, "count": 0}
        cutoff = time.time() - window_sec
        filtered = [
            p
            for p in points
            if float(p.get("sampled_at_unix") or 0) >= cutoff or "sampled_at_unix" not in p
        ]
        if not filtered:
            filtered = points
        # step: keep every Nth sample after 1Hz collector (approx step seconds)
        out = filtered[:: max(1, step_sec)]
        if len(out) > HISTORY_MAX_POINTS:
            out = out[-HISTORY_MAX_POINTS:]
        return {"points": out, "window": window_sec, "step": step_sec, "count": len(out)}

    def health(self) -> Dict[str, Any]:
        with self._lock:
            last = self._history[-1] if self._history else None
        pipe = read_pipeline_snapshot(include_export=False)
        dpdk_sock = find_dpdk_telemetry_socket()
        return {
            "ok": True,
            "sensor_boot_id": SENSOR_BOOT_ID,
            "collector_fresh": bool(last),
            "last_sample": (last or {}).get("sampled_at"),
            "counter_support": self._counter_support,
            "dpdk_telemetry": dpdk_sock,
            "pipeline_stats_path": pipe.get("path"),
            "pipeline_stats_valid": bool(pipe.get("valid")),
            "build": {
                "suricata": os.environ.get("AIVAX_SURICATA_VERSION", "8.0.6"),
                "profile": os.environ.get("AIVAX_PROFILE", "production"),
            },
        }

    def start(self, interval: float = 1.0) -> None:
        def _loop():
            while not self._stop.wait(interval):
                try:
                    self._sample()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_loop, name="metrics-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


_collector = MetricsCollector()


def get_collector() -> MetricsCollector:
    return _collector


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/v1/metrics/current":
            self._send(200, _collector.current())
            return
        if u.path == "/v1/metrics/pipeline":
            self._send(200, _collector.pipeline())
            return
        if u.path == "/v1/metrics/history":
            qs = parse_qs(u.query)
            window = qs.get("window", ["1h"])[0]
            step = int(qs.get("step", ["5"])[0])
            if window.endswith("h"):
                window_sec = int(float(window[:-1]) * 3600)
            elif window.endswith("m"):
                window_sec = int(float(window[:-1]) * 60)
            elif window.endswith("s"):
                window_sec = int(float(window[:-1]))
            else:
                window_sec = int(window)
            if window_sec > 24 * 3600 or step < 1:
                self._send(400, {"error": "window/step out of range"})
                return
            hist = _collector.history(window_sec, step)
            if hist.get("count", 0) > HISTORY_MAX_POINTS:
                self._send(400, {"error": "too_many_points"})
                return
            self._send(200, hist)
            return
        if u.path == "/v1/metrics/health":
            self._send(200, _collector.health())
            return
        if u.path in ("/v1/metrics/service-gate", "/v1/metrics/service_gate"):
            try:
                from service_gate_stats import read_stats

                self._send(200, read_stats())
            except Exception as exc:
                # Fall back to on-disk snapshot if import path fails.
                gate = _read_json_file(GATE_STATS_PATH)
                if gate is not None:
                    self._send(200, gate)
                else:
                    self._send(500, {"error": str(exc)})
            return
        self._send(404, {"error": "not_found"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default=os.environ.get("AIVAX_METRICS_BIND", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("AIVAX_METRICS_PORT", "9108")))
    args = ap.parse_args()
    # localhost-only by default for safety without reverse proxy
    if args.bind not in ("127.0.0.1", "::1", "localhost"):
        print(f"[metrics] WARN: binding non-localhost {args.bind}; require auth/TLS proxy")
    _collector.start(1.0)
    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"[metrics] listening on http://{args.bind}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _collector.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
