#!/usr/bin/env python3
"""Run isolated four-worker Pipeline scenarios and audit final NDJSON logs.

python scripts/load_pipeline.py --workers 4 --seconds 10 --concurrency 4 16 64
Uses real regex, DOCX inspection, CSO and Valkey. No model/OCR service is called.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import psutil
from load_cso import (
    ROOT,
    free_port,
    jvms,
    open_document,
    percentile,
    process_stats,
    stop,
)
from valkey import Valkey

PROFILES = ("merge", "regex_allow", "regex_block", "docx_cso", "mirror_docx")
PROMPT = ("Pipeline benchmark normal business text. " * 110)[:4096]
BLOCK_TOKEN = "LOAD_BLOCK_ABCD1234"
PREFIX = "pipeline:analysis-log-merge:v1:"
ENTRY = """import os
from pathlib import Path
from main import create_app as original_create_app
from src.bootstrap.env_loader import SETTINGS

def create_app():
    app = original_create_app()
    # Keep the real allowlist validation, with an isolated test backup root.
    service = app.state.pipeline_runtime_modules.attachment_notification_service
    if service is not None:
        service._shared_backup_roots = (Path(SETTINGS.file_block_backup_dir).resolve(),)
    async def tagged(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        async def tagged_send(message):
            if message["type"] == "http.response.start":
                worker = str(os.getpid()).encode()
                message["headers"].append((b"x-load-worker-pid", worker))
            await send(message)
        return await app(scope, receive, tagged_send)
    return tagged
"""


def has_file(profile: str) -> bool:
    return profile in {"docx_cso", "mirror_docx"}


def policies(cache: Valkey) -> None:
    def rule(index, expression):
        return {
            "id": f"load-rule-{index}",
            "name": f"load rule {index}",
            "rule": base64.b64encode(expression.encode()).decode(),
            "action": "block",
            "scope": "default",
            "subject_id": "",
            "category": "load-test",
            "targets": "contents",
            "regex_flag": 0,
        }

    regex = [rule(i, f"SENSITIVE_{i:04d}_[A-Z0-9]{{12}}") for i in range(99)]
    regex.append(rule(99, "LOAD_BLOCK_[A-Z0-9]{8}"))
    cache.set(
        "pipeline:policy:regex:v1",
        json.dumps(
            {
                "version": "load-regex-100",
                "rules": regex,
                "group_members": {},
            }
        ),
    )
    cache.set(
        "pipeline:policy:multimodal:v1",
        json.dumps(
            {
                "version": "load-file-20",
                "group_members": {},
                "settings": [],
                "rules": [
                    rule(i, f"FILE_SECRET_{i:02d}_[A-Z]{{8}}") for i in range(20)
                ],
            }
        ),
    )


@asynccontextmanager
async def runtime(output: Path, workers: int, profile: str):
    output.mkdir(parents=True)
    api_port, vk_port = free_port(), free_port()
    while api_port == vk_port:
        vk_port = free_port()
    (output / "mode.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source": "pipeline-load-test",
                "mode": "mirror" if profile == "mirror_docx" else "inline",
                "filters": {
                    "regex": {"enabled": profile != "merge"},
                    "multimodal": {"enabled": has_file(profile)},
                    "slm": {"enabled": False},
                    "secuxper": {"enabled": False},
                },
            }
        )
    )
    (output / "load.env").write_text("")
    (output / "pipeline_load_entry.py").write_text(ENTRY)
    (output / "incoming").mkdir()
    (output / "valkey").mkdir()
    if has_file(profile):
        open_document(output / "template.docx")
    settings = {
        "ENV_FILE": str(output / "load.env"),
        "RUNTIME_DIR": str(output / "runtime"),
        "HOST": "127.0.0.1",
        "PORT": str(api_port),
        "WORKERS": str(workers),
        "MODE_FILE": str(output / "mode.json"),
        "LOG_LEVEL": "warning",
        "CSO_ENABLED": str(has_file(profile)).lower(),
        "VALKEY_HOST": "127.0.0.1",
        "VALKEY_PORT": str(vk_port),
        "VALKEY_PASSWORD": "",
        "ANALYSIS_LOG_MERGE_BACKEND": "valkey",
        "CDS_DELIVERY_ENABLED": "false",
        "FILE_CONTENT_CHUNK_SIZE": "64",
        "WATERMARK_USE_OCR_BASE_WATERMARK": "false",
        "WATERMARK_USE_REGEX_BASE_DRM": "false",
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
            "PYTHONPATH": str(output) + os.pathsep + str(ROOT),
        }
    )
    server = valkey = None
    cache = None
    with (
        (output / "pipeline.log").open("w") as log,
        (output / "valkey.log").open("w") as vk_log,
    ):
        try:
            valkey = subprocess.Popen(
                [
                    "valkey-server",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(vk_port),
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
            cache = Valkey(
                host="127.0.0.1",
                port=vk_port,
                socket_timeout=1,
                socket_connect_timeout=1,
            )
            async with asyncio.timeout(10):
                while True:
                    try:
                        if cache.ping():
                            break
                    except Exception:
                        if valkey.poll() is not None:
                            raise RuntimeError("Private Valkey exited") from None
                    await asyncio.sleep(0.1)
            policies(cache)
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "pipeline_load_entry:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                    "--workers",
                    str(workers),
                    "--loop",
                    "uvloop",
                    "--http",
                    "httptools",
                    "--log-level",
                    "warning",
                    "--no-access-log",
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            base = f"http://127.0.0.1:{api_port}"
            seen = set()
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
                timeout=aiohttp.ClientTimeout(total=1),
            ) as client:
                async with asyncio.timeout(45):
                    while len(seen) < workers:
                        if server.poll() is not None:
                            raise RuntimeError(
                                f"Pipeline exited; inspect {output}/pipeline.log"
                            )
                        try:
                            async with client.get(base + "/v1/health/live") as response:
                                if response.status == 200:
                                    seen.add(int(response.headers["x-load-worker-pid"]))
                        except (aiohttp.ClientError, TimeoutError):
                            pass
                        await asyncio.sleep(0.02)
            yield base, server, cache, sorted(seen)
        finally:
            stop(server)
            if cache is not None:
                cache.close()
            stop(valkey)


def summary(samples: list[float]) -> dict:
    if not samples:
        return {}
    return {
        "p50": percentile(samples, 0.5),
        "p95": percentile(samples, 0.95),
        "p99": percentile(samples, 0.99),
        "max": max(samples),
    }


class Logs:
    def __init__(self, output: Path, profile: str):
        self.directory = output / "runtime/bulk"
        self.profile = profile
        self.offsets: dict[Path, int] = {}
        self.counts: Counter = Counter()
        self.invalid: dict[str, str] = {}

    def read(self):
        for path in self.directory.glob("*.json"):
            with path.open("rb") as source:
                source.seek(self.offsets.get(path, 0))
                chunk = source.read()
            end = chunk.rfind(b"\n")
            if end < 0:
                continue
            self.offsets[path] = self.offsets.get(path, 0) + end + 1
            for line in chunk[:end].splitlines():
                document = json.loads(line)
                mid = document["message_id"]
                self.counts[mid] += 1
                try:
                    self.validate(document)
                except (AssertionError, KeyError, IndexError, TypeError) as error:
                    self.invalid[mid] = str(error) or type(error).__name__

    def validate(self, document):
        mid = document["message_id"]
        assert document["prompt"].startswith("Pipeline benchmark"), "prompt missing"
        if self.profile == "regex_block":
            assert document["mode"] == "block", "block mode missing"
            assert document["output_text"], "block output missing"
        else:
            assert document["mode"] != "block", "unexpected block"
            assert document["output_text"] == f"pipeline reply {mid}", (
                "output merge failed"
            )
        if self.profile != "merge":
            assert document["filter_detect"], "inspection log missing"
            assert all(
                row.get("started_at")
                and row.get("completed_at")
                and not row.get("error")
                for row in document["filter_detect"]
            ), "filter skipped or failed"
        if self.profile == "docx_cso":
            assert document["cso"]["files"][0]["grade"] == 3, "O grade missing"
            assert document["cso"]["status"] == "open", "unexpected CSO status"
        else:
            assert "cso" not in document, "unexpected CSO in non-CSO/Mirror log"
        if has_file(self.profile):
            summaries = [
                item
                for row in document["filter_detect"]
                for item in row.get("file_summary", [])
            ]
            assert len(summaries) == 1, "file summary missing"
            info = summaries[0]["file_info"]
            assert info.get("hash"), "content inspection missing"
            assert info.get("chunk", "").startswith("CSO load test document"), (
                "DOCX extraction failed"
            )
            assert Path(summaries[0]["file_backup_path"]).is_file(), "archive missing"

    async def audit(self, expected: list[str]) -> dict:
        began = time.perf_counter()
        while True:
            self.read()
            missing = [mid for mid in expected if not self.counts[mid]]
            if not missing or time.perf_counter() - began > 30:
                break
            await asyncio.sleep(0.05)
        return {
            "expected": len(expected),
            "missing": len(missing),
            "duplicates": sum(max(0, self.counts[mid] - 1) for mid in expected),
            "invalid": sum(mid in self.invalid for mid in expected),
            "invalid_examples": [
                self.invalid[mid] for mid in expected if mid in self.invalid
            ][:3],
            "drain_and_audit_seconds": time.perf_counter() - began,
        }


async def exercise(base, output, profile, concurrency, seconds=0, count=0):
    started = time.perf_counter()
    cpu = psutil.Process().cpu_times()
    cpu_before = cpu.user + cpu.system
    input_ms, output_ms, cycle_ms = [], [], []
    expected, failures = [], Counter()
    input_workers, output_workers = Counter(), Counter()
    crosses = http_requests = success = 0
    errors = []
    async with aiohttp.ClientSession(
        base_url=base,
        connector=aiohttp.TCPConnector(limit=concurrency, force_close=True),
        timeout=aiohttp.ClientTimeout(total=30),
        trust_env=False,
    ) as client:

        async def post(route, body, samples):
            nonlocal http_requests
            http_requests += 1
            began = time.perf_counter()
            try:
                async with client.post(route, json=body) as response:
                    response.raise_for_status()
                    return await response.json(), int(
                        response.headers["x-load-worker-pid"]
                    )
            finally:
                samples.append((time.perf_counter() - began) * 1000)

        async def send():
            nonlocal success, crosses
            while (
                (len(expected) < count)
                if count
                else (time.perf_counter() - started < seconds)
            ):
                mid = "pipeline-load-" + uuid.uuid4().hex
                expected.append(mid)
                filters = ["input_filter"]
                if profile != "merge":
                    filters.append("secret_filter")
                attachments = []
                if has_file(profile):
                    source = output / "incoming" / f"{mid}.docx"
                    os.link(output / "template.docx", source)
                    attachments.append({"id": str(source), "name": "document.docx"})
                    filters.append("file_block_filter")
                body = {
                    "message_id": mid,
                    "prompt": PROMPT
                    + (BLOCK_TOKEN if profile == "regex_block" else ""),
                    "filter_list": filters,
                    "attachments": attachments,
                    "email": "pipeline-load@example.invalid",
                    "user_id": "load-user",
                    "ai_service": 1,
                    "client_host": "127.0.0.1",
                }
                cycle_started = time.perf_counter()
                try:
                    result, input_pid = await post(
                        "/v1/filter/multiple_filter",
                        body,
                        input_ms,
                    )
                    input_workers[input_pid] += 1
                    expected_code = 1 if profile == "regex_block" else 0
                    if result["final_decision"]["action_code"] != expected_code:
                        raise ValueError(f"Unexpected input decision: {result}")
                    if profile != "regex_block":
                        _, output_pid = await post(
                            "/v1/filter/output_filter",
                            {
                                "message_id": mid,
                                "llm_output": f"pipeline reply {mid}",
                                "email": body["email"],
                                "user_id": body["user_id"],
                                "ai_service": 1,
                                "client_host": "127.0.0.1",
                            },
                            output_ms,
                        )
                        output_workers[output_pid] += 1
                        crosses += input_pid != output_pid
                    success += 1
                except Exception as error:
                    failures[type(error).__name__] += 1
                    if len(errors) < 3:
                        errors.append(str(error))
                finally:
                    cycle_ms.append((time.perf_counter() - cycle_started) * 1000)

        await asyncio.gather(*(send() for _ in range(concurrency)))
    elapsed = time.perf_counter() - started
    cpu = psutil.Process().cpu_times()
    return {
        "concurrency": concurrency,
        "submission_seconds": elapsed,
        "sessions": len(expected),
        "successes": success,
        "http_requests": http_requests,
        "sessions_per_second": success / elapsed,
        "http_requests_per_second": http_requests / elapsed,
        "errors": dict(failures),
        "error_examples": errors,
        "input_latency_ms": summary(input_ms),
        "output_latency_ms": summary(output_ms),
        "cycle_latency_ms": summary(cycle_ms),
        "input_worker_requests": dict(input_workers),
        "output_worker_requests": dict(output_workers),
        "cross_worker_pairs": crosses,
        "load_generator_cpu_percent_one_core": (cpu.user + cpu.system - cpu_before)
        / elapsed
        * 100,
    }, expected


async def profile_run(args, output, profile):
    async with runtime(output, args.workers, profile) as (base, server, cache, workers):
        print(json.dumps({"profile_ready": profile, "workers": workers}), flush=True)
        collector = Logs(output, profile)
        warm, ids = await exercise(base, output, profile, 16, count=args.warmup)
        warm["log_audit"] = await collector.audit(ids)
        if warm["errors"] or any(
            warm["log_audit"][key] for key in ("missing", "duplicates", "invalid")
        ):
            raise RuntimeError(f"Warmup failed: {warm}")
        sampling = True
        peak_python = peak_java = 0.0
        seen_jvms = set()
        python_processes = [psutil.Process(pid) for pid in workers]

        async def sample():
            nonlocal peak_python, peak_java
            while sampling:
                java = process_stats(jvms(server))
                seen_jvms.update(p["pid"] for p in java)
                peak_java = max(peak_java, sum(p["rss_mib"] for p in java))
                peak_python = max(
                    peak_python,
                    sum(p["rss_mib"] for p in process_stats(python_processes)),
                )
                await asyncio.sleep(0.2)

        sampler = asyncio.create_task(sample())
        rows = []
        try:
            for concurrency in args.concurrency:
                before = {
                    p["pid"]: p["cpu_seconds"] for p in process_stats(python_processes)
                }
                row, ids = await exercise(
                    base, output, profile, concurrency, seconds=args.seconds
                )
                row["log_audit"] = await collector.audit(ids)
                row["verified_logs_per_second"] = (
                    len(ids) - row["log_audit"]["missing"]
                ) / (
                    row["submission_seconds"]
                    + row["log_audit"]["drain_and_audit_seconds"]
                )
                row["pending_merge_keys"] = sum(
                    key.decode() not in {PREFIX + "index", PREFIX + "outpend_index"}
                    for key in cache.scan_iter(match=PREFIX + "*")
                )
                row["python_workers"] = [
                    {
                        **p,
                        "cpu_percent_one_core": (p["cpu_seconds"] - before[p["pid"]])
                        / (
                            row["submission_seconds"]
                            + row["log_audit"]["drain_and_audit_seconds"]
                        )
                        * 100,
                    }
                    for p in process_stats(python_processes)
                ]
                rows.append(row)
                print(json.dumps({"profile": profile, **row}), flush=True)
            await asyncio.sleep(1.1)
            expected_events = args.warmup + sum(row["sessions"] for row in rows)
            event_targets = {
                "management:file-asset:v1": expected_events if has_file(profile) else 0,
                "management:policy-notification:v1": (
                    expected_events if profile == "regex_block" else 0
                ),
                "management:user-sync:v1": expected_events,
            }
            for _ in range(50):
                stream_lengths = {name: cache.xlen(name) for name in event_targets}
                if stream_lengths == event_targets:
                    break
                await asyncio.sleep(0.1)
            error_lines = sum(
                "ERROR" in line
                for line in (output / "pipeline.log").read_text().splitlines()
            )
            return {
                "profile": profile,
                "workers": workers,
                "warmup": warm,
                "stages": rows,
                "python_peak_rss_mib": peak_python,
                "jvm_peak_rss_mib": peak_java,
                "observed_jvm_pids": sorted(seen_jvms),
                "jvm_restarts": max(
                    0, len(seen_jvms) - (args.workers if has_file(profile) else 0)
                ),
                "event_stream_lengths": stream_lengths,
                "event_stream_expected": event_targets,
                "event_stream_audit_ok": stream_lengths == event_targets,
                "application_error_lines": error_lines,
            }
        finally:
            sampling = False
            await sampler


async def run(args, output):
    report = {
        "recorded_utc": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "workers": args.workers,
        "seconds_per_stage": args.seconds,
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "prompt_bytes": len(PROMPT.encode()),
        "regex_rules": 100,
        "file_rules": 20,
        "load_model": "closed-loop aiohttp, TCP close, debug=false",
        "excluded": ["SLM", "SecuXper", "OCR", "Fluent Bit forwarding", "OpenSearch"],
        "profiles": [],
    }
    for profile in args.profiles:
        result = await profile_run(args, output / profile, profile)
        report["profiles"].append(result)
        (output / "report.json").write_text(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[4, 16, 64])
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument(
        "--profiles", nargs="+", choices=PROFILES, default=list(PROFILES)
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if min(args.workers, args.seconds, args.warmup, *args.concurrency) <= 0:
        parser.error("workers, seconds, warmup and concurrency must be positive")
    output = (
        args.out or ROOT / ".runtime/pipeline-load" / uuid.uuid4().hex[:12]
    ).resolve()
    output.mkdir(parents=True, exist_ok=False)
    try:
        report = asyncio.run(run(args, output))
    except Exception as error:
        print(f"Failed: {error}\nArtifacts: {output}", file=sys.stderr)
        return 1
    print(f"Report: {output / 'report.json'}", flush=True)
    return int(
        any(
            row["errors"]
            or not profile["event_stream_audit_ok"]
            or profile["application_error_lines"]
            or row["pending_merge_keys"]
            or any(
                row["log_audit"][key] for key in ("missing", "duplicates", "invalid")
            )
            for profile in report["profiles"]
            for row in profile["stages"]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
