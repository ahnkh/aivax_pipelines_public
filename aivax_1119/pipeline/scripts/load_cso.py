#!/usr/bin/env python3
"""Isolated real Pipeline/Valkey CSO load test, with one JVM per HTTP worker.

Run from the Pipeline root with its Python environment:
  python scripts/load_cso.py --workers 4 --concurrency 4 16 64 --seconds 10
The default fixture is a generated O-grade DOCX, not a customer document.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import psutil
from cso_fixtures import write_cso_docx

ROOT = Path(__file__).resolve().parents[1]


def open_document(path: Path) -> None:
    """일반 DOCX를 생성한 뒤 Fasoo 태깅 API로 O 등급을 부여한다."""
    write_cso_docx(path, 3, text="CSO load test document. " * 3000)


def free_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def jvms(server: subprocess.Popen) -> list[psutil.Process]:
    try:
        children = psutil.Process(server.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return []
    result = []
    for child in children:
        try:
            if "CsoJvmWorker" in child.cmdline():
                result.append(child)
        except psutil.NoSuchProcess:
            continue
    return result


def process_stats(processes: list[psutil.Process]) -> list[dict]:
    stats = []
    for process in processes:
        try:
            cpu = process.cpu_times()
            stats.append(
                {
                    "pid": process.pid,
                    "parent_pid": process.ppid(),
                    "rss_mib": process.memory_info().rss / 2**20,
                    "cpu_seconds": cpu.user + cpu.system,
                }
            )
        except psutil.NoSuchProcess:
            continue
    return stats


async def wait_ready(server: subprocess.Popen, base: str, workers: int) -> None:
    async with aiohttp.ClientSession() as client:
        async with asyncio.timeout(45):
            while True:
                if server.poll() is not None:
                    raise RuntimeError("Pipeline exited; inspect pipeline.log")
                if len(jvms(server)) == workers:
                    try:
                        async with client.get(
                            base + "/v1/health/live",
                            timeout=aiohttp.ClientTimeout(total=1),
                        ) as response:
                            if response.status == 200:
                                return
                    except (aiohttp.ClientError, TimeoutError):
                        pass
                await asyncio.sleep(0.1)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


async def exercise(
    base: str,
    sample: Path,
    concurrency: int,
    *,
    seconds: float = 0,
    count: int = 0,
    keepalive: bool = False,
) -> dict:
    started = time.perf_counter()
    deadline = started + seconds
    latencies: list[float] = []
    failures: Counter = Counter()
    attempts = 0
    successes = 0
    examples: list[str] = []
    client_cpu = psutil.Process().cpu_times()
    cpu_before = client_cpu.user + client_cpu.system
    async with aiohttp.ClientSession(
        base_url=base,
        connector=aiohttp.TCPConnector(limit=concurrency, force_close=not keepalive),
        timeout=aiohttp.ClientTimeout(total=10),
        trust_env=False,
    ) as client:

        async def send():
            nonlocal attempts, successes
            while (attempts < count) if count else (time.perf_counter() < deadline):
                attempts += 1
                body = {
                    "message_id": "cso-load-" + uuid.uuid4().hex,
                    "filter_list": ["input_filter"],
                    "prompt": "CSO load test",
                    "attachments": [{"id": str(sample), "name": sample.name}],
                    "debug": True,
                }
                request_started = time.perf_counter()
                try:
                    async with client.post(
                        "/v1/filter/multiple_filter", json=body
                    ) as response:
                        response.raise_for_status()
                        result = await response.json()
                    cso = result["log_evidence"]["cso"]
                    if (
                        result["final_decision"]["action_code"] != 0
                        or cso["status"] != "open"
                        or len(cso["files"]) != 1
                        or cso["files"][0]["grade"] != 3
                        or cso["files"][0]["status"] != "tagged"
                    ):
                        raise ValueError("Expected tagged O grade and allow action")
                    successes += 1
                except Exception as error:
                    failures[type(error).__name__] += 1
                    if len(examples) < 3:
                        examples.append(str(error))
                finally:
                    latencies.append((time.perf_counter() - request_started) * 1000)

        await asyncio.gather(*(send() for _ in range(concurrency)))
    elapsed = time.perf_counter() - started
    client_cpu = psutil.Process().cpu_times()
    return {
        "load_generator_cpu_percent_one_core": (
            client_cpu.user + client_cpu.system - cpu_before
        )
        / elapsed
        * 100,
        "concurrency": concurrency,
        "elapsed_seconds": elapsed,
        "requests": attempts,
        "successes": successes,
        "errors": dict(failures),
        "error_examples": examples,
        "requests_per_second": attempts / elapsed,
        "successes_per_second": successes / elapsed,
        "latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
            "max": max(latencies),
        },
    }


def stop(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


async def run(args, output: Path) -> dict:
    sample = output / (args.sample.name if args.sample else "open.docx")
    if args.sample:
        shutil.copy2(args.sample.resolve(), sample)
    else:
        open_document(sample)
    if not sample.is_file():
        raise ValueError(f"Sample does not exist: {sample}")
    api_port, valkey_port = free_port(), free_port()
    while valkey_port == api_port:
        valkey_port = free_port()
    base = f"http://127.0.0.1:{api_port}"
    (output / "mode.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "mode": "inline",
                "source": "cso-load-test",
                "filters": {
                    key: {"enabled": False}
                    for key in (
                        "regex",
                        "multimodal",
                        "slm",
                        "secuxper",
                    )
                },
            }
        )
    )
    settings = {
        "ENV_FILE": str(output / "load.env"),
        "RUNTIME_DIR": str(output / "runtime"),
        "HOST": "127.0.0.1",
        "PORT": str(api_port),
        "WORKERS": str(args.workers),
        "MODE_FILE": str(output / "mode.json"),
        "LOG_LEVEL": "warning",
        "CSO_ENABLED": "true",
        "CSO_REQUEST_TIMEOUT_SECONDS": "5",
        "CSO_STARTUP_TIMEOUT_SECONDS": "15",
        "VALKEY_HOST": "127.0.0.1",
        "VALKEY_PORT": str(valkey_port),
        "VALKEY_PASSWORD": "",
        "ANALYSIS_LOG_MERGE_BACKEND": "valkey",
        "ANALYSIS_LOG_MERGE_INPUT_FLUSH_AFTER_SECONDS": "1800",
        "CDS_DELIVERY_ENABLED": "false",
    }
    env = {k: v for k, v in os.environ.items() if not k.startswith("AIVAX_")}
    env.update({"AIVAX_PIPELINE_" + k: v for k, v in settings.items()})
    env.update(
        {
            "AIVAX_TOOLKIT_ENABLED": "false",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )
    (output / "load.env").write_text("")
    (output / "valkey").mkdir()
    server = valkey = None
    observed_pids: set[int] = set()
    peak_rss = 0.0
    sampling = True

    async def sample_processes():
        nonlocal peak_rss
        while sampling:
            stats = process_stats(jvms(server))
            observed_pids.update(item["pid"] for item in stats)
            peak_rss = max(peak_rss, sum(item["rss_mib"] for item in stats))
            await asyncio.sleep(0.1)

    with (
        (output / "valkey.log").open("w") as vk_log,
        (output / "pipeline.log").open("w") as pipeline_log,
    ):
        sampler = None
        try:
            valkey = subprocess.Popen(
                [
                    "valkey-server",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(valkey_port),
                    "--save",
                    "",
                    "--appendonly",
                    "no",
                    "--dir",
                    str(output / "valkey"),
                ],
                stdout=vk_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            from valkey import Valkey

            cache = Valkey(host="127.0.0.1", port=valkey_port)
            async with asyncio.timeout(10):
                while True:
                    try:
                        if cache.ping():
                            break
                    except Exception:
                        if valkey.poll() is not None:
                            raise RuntimeError("Private Valkey exited") from None
                    await asyncio.sleep(0.1)
            boot_started = time.perf_counter()
            server = subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=ROOT,
                env=env,
                stdout=pipeline_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            await wait_ready(server, base, args.workers)
            startup_seconds = time.perf_counter() - boot_started
            initial = process_stats(jvms(server))
            print(json.dumps({"ready": base, "jvms": initial}), flush=True)
            sampler = asyncio.create_task(sample_processes())
            warmup = await exercise(
                base,
                sample,
                16,
                count=args.warmup,
                keepalive=args.keepalive,
            )
            if warmup["errors"]:
                raise RuntimeError(f"Warmup failed: {warmup}")
            rows = []
            for concurrency in args.concurrency:
                before = process_stats(jvms(server))
                python_workers = [psutil.Process(p["parent_pid"]) for p in before]
                workers_before = process_stats(python_workers)
                row = await exercise(
                    base,
                    sample,
                    concurrency,
                    seconds=args.seconds,
                    keepalive=args.keepalive,
                )
                after = process_stats(jvms(server))
                previous_cpu = {p["pid"]: p["cpu_seconds"] for p in before}
                row["jvms"] = [
                    {
                        **p,
                        "cpu_percent_one_core": (
                            p["cpu_seconds"] - previous_cpu.get(p["pid"], 0)
                        )
                        / row["elapsed_seconds"]
                        * 100,
                    }
                    for p in after
                ]
                worker_cpu = {p["pid"]: p["cpu_seconds"] for p in workers_before}
                row["python_workers"] = [
                    {
                        **p,
                        "cpu_percent_one_core": (
                            p["cpu_seconds"] - worker_cpu.get(p["pid"], 0)
                        )
                        / row["elapsed_seconds"]
                        * 100,
                    }
                    for p in process_stats(python_workers)
                ]
                rows.append(row)
                print(json.dumps(row), flush=True)
            return {
                "recorded_utc": datetime.now(UTC).isoformat(),
                "scope": "real Inline API + CSO guard + Valkey INPUT log merge",
                "load_model": "closed-loop, aiohttp, debug=true",
                "keepalive": args.keepalive,
                "workers": args.workers,
                "startup_seconds": startup_seconds,
                "request_timeout_seconds": 5,
                "sample": str(sample),
                "sample_bytes": sample.stat().st_size,
                "generated_sample": not args.sample,
                "cpu_affinity": sorted(os.sched_getaffinity(0)),
                "python": sys.version,
                "warmup": warmup,
                "stages": rows,
                "jvm_peak_total_rss_mib": peak_rss,
                "observed_jvm_pids": sorted(observed_pids),
                "jvm_restarts": max(0, len(observed_pids) - args.workers),
                "valkey_keys": cache.dbsize(),
            }
        finally:
            sampling = False
            if sampler is not None:
                await sampler
            stop(server)
            stop(valkey)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--concurrency", nargs="+", type=int, default=[4, 16, 64])
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument(
        "--keepalive",
        action="store_true",
        help="Reuse connections; default closes each to spread traffic across workers",
    )
    parser.add_argument(
        "--sample", type=Path, help="Copy an O-grade document; original is preserved"
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if min(args.workers, args.seconds, args.warmup, *args.concurrency) <= 0:
        parser.error("workers, concurrency, seconds and warmup must be positive")
    output = (args.out or ROOT / ".runtime/cso-load" / uuid.uuid4().hex[:12]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    try:
        report = asyncio.run(run(args, output))
    except Exception as error:
        print(f"Failed: {error}\nArtifacts: {output}", file=sys.stderr)
        return 1
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(f"Report: {output / 'report.json'}", flush=True)
    return int(any(row["errors"] for row in report["stages"]))


if __name__ == "__main__":
    raise SystemExit(main())
