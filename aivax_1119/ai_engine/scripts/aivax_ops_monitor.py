#!/usr/bin/env python3
"""AIVAX shell ops monitor — beginner-friendly realtime view for TS.

Commands:
  aivax-ops status   one-shot snapshot
  aivax-ops live     refresh every 1s (Ctrl+C to stop)
  aivax-ops json     machine-readable merged JSON

Sources (read-only, no packet path impact):
  $AIVAX_EXPORT_STATS / mirror_engine log /var/log paths
  $AIVAX_SHM_CONSUMER_STATS (/run/aivax/shm_consumer_stats.json)
  $AIVAX_SHM_META (/tmp/aivax_eve_ring.meta.json)
  NIC sysfs + systemctl is-active

TS tip:
  drop_shm_full↑  → engine drain slow or ring imbalance
  drop_eagain↑    → UDS fallback congestion
  UNBALANCED      → check ring hash / worker assignment
  high http2% + bad extract → H2 reassembly/stitching suspicion
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MIRROR_DIR = Path(__file__).resolve().parents[1] / "mirror"
if str(MIRROR_DIR) not in sys.path:
    sys.path.insert(0, str(MIRROR_DIR))

from ops_classify import ascii_bar, imbalance_ratio  # noqa: E402

EXPORT_CANDIDATES = [
    os.environ.get("AIVAX_EXPORT_STATS", "").strip(),
    "/var/log/aivax/aivax-export-stats.json",
    "/home1/aivax/mirror_engine/log/aivax-export-stats.json",
]
CONSUMER_CANDIDATES = [
    os.environ.get("AIVAX_SHM_CONSUMER_STATS", "").strip(),
    "/run/aivax/shm_consumer_stats.json",
    "/tmp/aivax_shm_consumer_stats.json",
]
SHM_META = os.environ.get("AIVAX_SHM_META", "/tmp/aivax_eve_ring.meta.json")
UNITS = ("aivax-mirror", "aivax-engine-mirror", "aivax-pipeline")
IMBALANCE_THRESHOLD = float(os.environ.get("AIVAX_OPS_IMBALANCE", "0.80"))


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    if not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _find_export_stats() -> Tuple[Optional[Dict[str, Any]], str]:
    for path in EXPORT_CANDIDATES:
        if not path:
            continue
        obj = _read_json(path)
        if obj is not None:
            return obj, path
    return None, EXPORT_CANDIDATES[1]


def _find_consumer_stats() -> Tuple[Optional[Dict[str, Any]], str]:
    for path in CONSUMER_CANDIDATES:
        if not path:
            continue
        obj = _read_json(path)
        if obj is not None:
            return obj, path
    return None, CONSUMER_CANDIDATES[1]


def _unit_active(name: str) -> str:
    try:
        out = subprocess.check_output(
            ["systemctl", "is-active", name],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        if out:
            return out
    except Exception:
        pass
    # Fallback when systemd bus is unavailable (containers/dev sandboxes).
    patterns = {
        "aivax-mirror": ("Suricata-Main", "mirror_engine/bin/suricata"),
        "aivax-engine-mirror": ("shm_ring_consumer.py",),
        "aivax-pipeline": ("pipeline/main.py",),
    }
    needles = patterns.get(name, ())
    try:
        out = subprocess.check_output(["ps", "-eo", "args"], text=True, timeout=2)
    except Exception:
        return "unknown"
    for line in out.splitlines():
        if any(n in line for n in needles):
            return "active"
    return "inactive"


def _nic_iface() -> str:
    for name in (
        os.environ.get("AIVAX_METRICS_IFS", "").split(",")[0].strip(),
        os.environ.get("MIRROR_IF", "").strip(),
        "aivax-mir0",
        "ens6f0",
    ):
        if name and os.path.isdir(f"/sys/class/net/{name}/statistics"):
            return name
    return ""


def _read_counter(path: str) -> Optional[int]:
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _nic_snapshot(prev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    iface = _nic_iface()
    if not iface:
        return {"valid": False, "if": "", "reason": "iface_missing"}
    base = f"/sys/class/net/{iface}/statistics"
    rx_packets = _read_counter(f"{base}/rx_packets") or 0
    rx_bytes = _read_counter(f"{base}/rx_bytes") or 0
    rx_dropped = _read_counter(f"{base}/rx_dropped") or 0
    now = time.time()
    out: Dict[str, Any] = {
        "valid": True,
        "if": iface,
        "rx_packets": rx_packets,
        "rx_bytes": rx_bytes,
        "rx_dropped": rx_dropped,
        "pps": 0.0,
        "mbps": 0.0,
    }
    if prev and prev.get("valid"):
        dt = max(1e-6, now - float(prev.get("ts", now)))
        out["pps"] = max(0, rx_packets - int(prev.get("rx_packets", rx_packets))) / dt
        out["mbps"] = (
            max(0, rx_bytes - int(prev.get("rx_bytes", rx_bytes))) * 8 / dt / 1_000_000.0
        )
    out["ts"] = now
    return out


def _stats_age_s(export: Optional[Dict[str, Any]]) -> Optional[float]:
    if not export:
        return None
    updated = export.get("updated_at_unix")
    if not updated:
        return None
    return max(0.0, time.time() - float(updated))


def collect_snapshot(prev_nic: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    export, export_path = _find_export_stats()
    consumer, consumer_path = _find_consumer_stats()
    consumer = consumer or {}
    meta = _read_json(SHM_META) or {}
    units = {name: _unit_active(name) for name in UNITS}
    nic = _nic_snapshot(prev_nic)

    ring_processed = [int(x) for x in (consumer.get("ring_processed") or [])]
    worker_processed = [int(x) for x in (consumer.get("worker_processed") or [])]
    ring_unbal, ring_share, ring_idx = imbalance_ratio(
        ring_processed, IMBALANCE_THRESHOLD
    )
    worker_unbal, worker_share, worker_idx = imbalance_ratio(
        worker_processed, IMBALANCE_THRESHOLD
    )

    http = {}
    service = {}
    consumer_http = consumer.get("http_version") if isinstance(consumer.get("http_version"), dict) else {}
    export_http = export.get("http_version") if isinstance(export, dict) and isinstance(export.get("http_version"), dict) else {}
    consumer_svc = consumer.get("ai_service") if isinstance(consumer.get("ai_service"), dict) else {}
    export_svc = export.get("ai_service") if isinstance(export, dict) and isinstance(export.get("ai_service"), dict) else {}
    if sum(int(v or 0) for v in consumer_http.values()) > 0:
        http = {k: int(v or 0) for k, v in consumer_http.items()}
    elif export_http:
        http = {k: int(v or 0) for k, v in export_http.items()}
    elif consumer_http:
        http = {k: int(v or 0) for k, v in consumer_http.items()}
    if sum(int(v or 0) for v in consumer_svc.values()) > 0:
        service = {k: int(v or 0) for k, v in consumer_svc.items()}
    elif export_svc:
        service = {k: int(v or 0) for k, v in export_svc.items()}
    elif consumer_svc:
        service = {k: int(v or 0) for k, v in consumer_svc.items()}

    return {
        "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "units": units,
        "nic": nic,
        "export_path": export_path,
        "export_age_s": _stats_age_s(export),
        "export": export or {},
        "consumer_path": consumer_path,
        "consumer": consumer,
        "shm_meta": meta,
        "http_version": http,
        "ai_service": service,
        "balance": {
            "ring_unbalanced": ring_unbal,
            "ring_max_share": round(ring_share, 3),
            "ring_max_index": ring_idx,
            "worker_unbalanced": worker_unbal,
            "worker_max_share": round(worker_share, 3),
            "worker_max_index": worker_idx,
            "threshold": IMBALANCE_THRESHOLD,
        },
    }


def _pct(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return 100.0 * part / float(total)


def _fmt_ratio_lines(title: str, mapping: Dict[str, int]) -> List[str]:
    total = sum(int(v) for v in mapping.values())
    lines = [f"  {title}  (total={total})"]
    if total <= 0:
        lines.append("    (no data yet)")
        return lines
    for key, value in sorted(mapping.items(), key=lambda kv: (-int(kv[1]), kv[0])):
        share = int(value) / float(total)
        lines.append(
            f"    {key:<16} {int(value):>8}  {_pct(int(value), total):5.1f}%  "
            f"{ascii_bar(share)}"
        )
    return lines


def _health_line(units: Dict[str, str]) -> str:
    parts = []
    for name, state in units.items():
        label = name.replace("aivax-", "")
        if state == "active":
            parts.append(_c("32", f"{label}=OK"))
        else:
            parts.append(_c("31", f"{label}={state}"))
    return "  ".join(parts)


def render_text(snap: Dict[str, Any]) -> str:
    export = snap.get("export") or {}
    consumer = snap.get("consumer") or {}
    nic = snap.get("nic") or {}
    balance = snap.get("balance") or {}
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append(f"AIVAX OPS MONITOR  {snap.get('sampled_at')}")
    lines.append("=" * 72)
    lines.append("")
    lines.append("[1] 서비스 건강")
    lines.append("  " + _health_line(snap.get("units") or {}))
    lines.append("")
    lines.append("[2] 미러 포트 유입")
    if nic.get("valid"):
        lines.append(
            f"  if={nic.get('if')}  pps={nic.get('pps', 0):.0f}  "
            f"mbps={nic.get('mbps', 0):.2f}  rx_drop={nic.get('rx_dropped', 0)}"
        )
    else:
        lines.append("  (NIC stats unavailable)")
    lines.append("")
    lines.append("[3] Suricata → SHM (export)")
    if export:
        lines.append(
            f"  emit={export.get('emit_total', 0)}  ok={export.get('export_ok', 0)}  "
            f"drop={export.get('export_drop', 0)}  shm_ok={export.get('export_ok_shm', 0)}"
        )
        lines.append(
            f"  shm_full={export.get('drop_shm_full', 0)}  "
            f"oversized={export.get('shm_oversized_fallback', 0)}  "
            f"eagain={export.get('drop_eagain', 0)}  mode={export.get('export_mode', '-')}"
        )
        emit = int(export.get("emit_total") or 0)
        oversized = int(export.get("shm_oversized_fallback") or 0)
        if oversized and emit:
            pct = 100.0 * oversized / emit
            lines.append(
                _c("31", f"  LOSS: 이벤트 {pct:.1f}% 가 슬롯 초과로 유실 → AIVAX_SHM_SLOT_SIZE 상향")
            )
        age = snap.get("export_age_s")
        if age is not None and age > 10:
            lines.append(_c("33", f"  STALE: 통계가 {age:.0f}초째 갱신 없음 (Suricata 정지 의심)"))
        lines.append(f"  source={snap.get('export_path')}")
    else:
        lines.append(f"  (missing {snap.get('export_path')})")
    lines.append("")
    lines.append("[4] SHM 링 균형")
    rings = consumer.get("ring_processed") or []
    errs = consumer.get("ring_errors") or []
    if rings:
        for i, val in enumerate(rings):
            err = errs[i] if i < len(errs) else 0
            mark = ""
            if balance.get("ring_unbalanced") and i == balance.get("ring_max_index"):
                mark = " " + _c("31", "UNBALANCED")
            lines.append(f"  ring{i}: processed={val} errors={err}{mark}")
        if balance.get("ring_unbalanced"):
            lines.append(
                _c(
                    "31",
                    f"  WARN: one ring holds {100 * float(balance.get('ring_max_share') or 0):.0f}% "
                    f"(threshold {100 * float(balance.get('threshold') or 0):.0f}%)",
                )
            )
    else:
        lines.append("  (no consumer ring stats yet)")
    lines.append("")
    lines.append("[5] 워커 균형")
    workers = consumer.get("worker_processed") or []
    if workers:
        for i, val in enumerate(workers):
            mark = ""
            if balance.get("worker_unbalanced") and i == balance.get("worker_max_index"):
                mark = " " + _c("31", "UNBALANCED")
            lines.append(f"  W{i}: processed={val}{mark}")
    else:
        lines.append("  (no worker stats yet)")
    lines.append("")
    lines.append("[6] HTTP 비율")
    lines.extend(_fmt_ratio_lines("HTTP", snap.get("http_version") or {}))
    lines.append("")
    lines.append("[7] AI 서비스 비율")
    lines.extend(_fmt_ratio_lines("AI service", snap.get("ai_service") or {}))
    lines.append("")
    lines.append("[8] 처리량")
    lines.append(
        f"  processed={consumer.get('processed', 0)}  "
        f"eps_instant={consumer.get('eps_instant', 0)}  "
        f"eps_avg={consumer.get('eps_avg', 0)}  "
        f"workers={consumer.get('workers', 0)}  n_rings={consumer.get('n_rings', 0)}"
    )
    lines.append("")
    lines.append("TS tips: drop_shm_full↑=drain slow/imbalance | drop_eagain↑=UDS congested")
    lines.append("         UNBALANCED=check ring hash | high http2%+bad extract=H2 stitch")
    lines.append("=" * 72)
    return "\n".join(lines)


def cmd_status(_: argparse.Namespace) -> int:
    print(render_text(collect_snapshot()))
    return 0


def cmd_json(_: argparse.Namespace) -> int:
    print(json.dumps(collect_snapshot(), ensure_ascii=False, indent=2))
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    interval = max(0.2, float(args.interval))
    prev_nic: Optional[Dict[str, Any]] = None
    try:
        while True:
            snap = collect_snapshot(prev_nic)
            prev_nic = snap.get("nic") if isinstance(snap.get("nic"), dict) else None
            sys.stdout.write("\033[H\033[J")
            sys.stdout.write(render_text(snap))
            sys.stdout.write("\n(Ctrl+C to quit)\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aivax-ops",
        description="AIVAX shell ops monitor (status/live/json)",
        epilog=(
            "Sources: export stats JSON, shm consumer stats, NIC sysfs, systemctl.\n"
            "NO_COLOR=1 disables ANSI colors."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="one-shot snapshot").set_defaults(func=cmd_status)
    live = sub.add_parser("live", help="refresh every N seconds")
    live.add_argument("--interval", type=float, default=1.0)
    live.set_defaults(func=cmd_live)
    sub.add_parser("json", help="merged JSON snapshot").set_defaults(func=cmd_json)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
