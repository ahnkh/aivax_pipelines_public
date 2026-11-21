#!/usr/bin/env python3
"""Multi-process shm ring consumer for aivax eve export.

Suricata plugin (C) publishes into POSIX shm rings; this process pool pops and
runs the same PolicyEngine path as UDS eve mode — but each OS process has its
own GIL, so CPU-bound extract/decompress scales across cores.

Architecture (per OS worker):
  drain loop  — only pops SHM → asyncio.Queue  (never awaits policy HTTP)
  policy pool — N tasks take from the queue → evaluate / policy API

So a slow multi-file policy call cannot stall ring drain (mirror-friendly).

Usage:
  # Plugin must create the ring first (Suricata with AIVAX_EXPORT_MODE=auto|shm)
  python3 shm_ring_consumer.py --workers 8 --server-url ''

  # Or create a test ring and feed it (see scripts/shm_ring_c_bench.py)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import multiprocessing as mp
import os
import signal
import sys
import time
from typing import Any, Dict, Optional, Tuple

MIRROR_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(MIRROR_DIR)
CORE_DIR = os.path.join(BASE_DIR, "core")
for _p in (BASE_DIR, CORE_DIR, MIRROR_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shm_ring import META_DEFAULT, NAME_DEFAULT, ShmRing  # noqa: E402
from ops_classify import (  # noqa: E402
    HTTP_KEYS,
    SERVICE_KEYS,
    classify_ai_service_index,
    classify_http_version_index,
    counts_to_dict,
)


def _decode_eve(raw: bytes) -> Any:
    """Decode plugin JSON consistently with the UDS consumer.

    Request bodies can contain arbitrary WebSocket bytes.  Replacing malformed
    UTF-8 keeps one bad body from dropping the entire metadata event.
    """
    return json.loads(raw.rstrip(b"\n").decode("utf-8", errors="replace"))


def _build_engine(args: argparse.Namespace):
    import ai_policy_engine as ape

    engine = ape.PolicyEngine(
        keywords=ape.read_keywords(getattr(args, "block_keywords", "") or ""),
        payload_preview_bytes=args.payload_preview_bytes,
        log_payload=args.log_payload,
        rewrite_from="",
        rewrite_to="",
        adapters_dir=args.adapters_dir,
        adapters_hot_reload=not args.adapters_no_reload,
        chatgpt_force_request_text="",
        chatgpt_block_sysinstruct=True,
        pipeline_server_url=args.server_url or "",
        pipeline_server_port=args.port,
        pipeline_input_api_path=args.PipelineInputAPIPath,
        pipeline_output_api_path=args.PipelineOutputAPIPath,
        policy_api_timeout_ms=args.policy_api_timeout_ms,
        policy_api_max_inflight=args.policy_api_max_inflight,
        policy_api_pool_limit=args.policy_api_pool_limit,
        policy_api_acquire_timeout_ms=args.policy_api_acquire_timeout_ms,
        policy_api_failure_policy=args.policy_api_failure_policy,
    )
    return ape, engine


def _worker_main(
    worker_id: int,
    ring_ids: list[int],
    shm_name: str,
    meta_path: str,
    args_ns: Dict[str, Any],
    stop_event: Any,
    stats: Any,
    ring_stats: Any,
    ring_errors: Any,
    worker_stats: Any,
    http_stats: Any,
    service_stats: Any,
) -> None:
    # Rebuild Namespace in child
    args = argparse.Namespace(**args_ns)
    cpu_list = [
        int(item)
        for item in str(args.cpu_list or "").split(",")
        if item.strip().isdigit()
    ]
    if cpu_list and hasattr(os, "sched_setaffinity"):
        cpu = cpu_list[worker_id % len(cpu_list)]
        if cpu in os.sched_getaffinity(0):
            os.sched_setaffinity(0, {cpu})
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format=f"%(asctime)s W{worker_id} %(levelname)s %(message)s",
    )
    # Keep file collection semantics identical to the UDS engine. Without
    # this, shm workers silently fall back to the 10MiB default and skip
    # valid 30MiB uploads even when deployment policy allows them.
    import ai_policy_engine as ape

    ape.load_file_collection_policy(args.dyn_policy_file)
    try:
        from service_gate_stats import start_flush_thread

        start_flush_thread(1.0)
    except Exception:
        logging.exception("service_gate_stats flush thread start failed (shm worker)")
    # UDS main() configures account_lookup; SHM workers must do the same or
    # set_account/get silently no-op (_cache is None) → Anonymous on Input Filter.
    ape.configure_account_lookup(
        maxsize=int(getattr(args, "account_cache_maxsize", 10_000) or 10_000),
        ttl_seconds=int(getattr(args, "account_cache_ttl", 3600) or 3600),
        shards=int(os.environ.get("AIVAX_ACCOUNT_SHARDS", "64")),
    )
    # ChatGPT stream_handoff → parent request_id must be visible across SHM
    # worker processes (HTTP register on W_a, WS final answer on W_b).
    try:
        from resume_parent_store import configure_from_env, backend_name

        configure_from_env()
        logging.info("resume_parent_store ready backend=%s", backend_name())
    except Exception:
        logging.exception(
            "resume_parent_store init failed; ChatGPT multi-worker handoff "
            "remap may break (parent=-)"
        )
    ape, engine = _build_engine(args)

    async def _run() -> None:
        await engine.start()
        ring = ShmRing(shm_name=shm_name, meta_path=meta_path)
        ring.open(timeout_s=60.0)

        # Policy workers: concurrent evaluate/policy HTTP.
        max_inflight = max(1, int(os.environ.get("AIVAX_SHM_MAX_INFLIGHT", "64")))
        # Queue absorbs popped payloads in RAM so SHM slots free immediately.
        # Depth >> inflight: drain keeps going while policy is slow.
        queue_depth = max(
            max_inflight * 4,
            int(os.environ.get("AIVAX_SHM_QUEUE_DEPTH", "0") or 0),
            256,
        )
        work_q: asyncio.Queue[Optional[Tuple[bytes, int]]] = asyncio.Queue(maxsize=queue_depth)

        logging.info(
            "shm consumer up rings=%s n_rings=%d slot=%d policy_workers=%d queue_depth=%d cpus=%s",
            ring_ids,
            ring.n_rings,
            ring.slot_size,
            max_inflight,
            queue_depth,
            sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else "-",
        )

        async def _policy_worker(wid: int) -> None:
            while True:
                item = await work_q.get()
                try:
                    if item is None:
                        return
                    raw, ring_id = item
                    eve: Any = None
                    try:
                        eve = _decode_eve(raw)
                        if isinstance(eve, dict) and eve:
                            http_idx = classify_http_version_index(
                                str(eve.get("http_version") or "")
                            )
                            svc_idx = classify_ai_service_index(
                                str(eve.get("host") or ""),
                                str(eve.get("uri") or ""),
                            )
                            await ape._process_eve_event(eve, engine)
                            with stats.get_lock():
                                stats.value += 1
                            with ring_stats.get_lock():
                                ring_stats[ring_id] += 1
                            with worker_stats.get_lock():
                                worker_stats[worker_id] += 1
                            with http_stats.get_lock():
                                http_stats[http_idx] += 1
                            with service_stats.get_lock():
                                service_stats[svc_idx] += 1
                    except Exception:
                        with ring_errors.get_lock():
                            ring_errors[ring_id] += 1
                        service = "-"
                        path = "-"
                        request_id = "-"
                        try:
                            if isinstance(eve, dict):
                                service = str(eve.get("service_id") or eve.get("host") or "-")
                                path = str(eve.get("uri") or "-")
                                request_id = str(eve.get("request_id") or "-")
                        except Exception:
                            pass
                        logging.exception(
                            "process error ring=%d pw=%d request_id=%s service=%s path=%s raw_bytes=%d",
                            ring_id,
                            wid,
                            request_id,
                            service,
                            path[:160],
                            len(raw),
                        )
                finally:
                    work_q.task_done()

        workers = [asyncio.create_task(_policy_worker(i)) for i in range(max_inflight)]

        idle = 0
        while not stop_event.is_set():
            got = False
            for rid in ring_ids:
                # Drain as many slots as the queue will take without blocking
                # the event loop forever — use put_nowait when possible.
                while True:
                    raw = ring.pop(rid)
                    if not raw:
                        break
                    got = True
                    idle = 0
                    try:
                        work_q.put_nowait((raw, rid))
                    except asyncio.QueueFull:
                        # Rare: policy backlog deeper than queue_depth.
                        # Await put so we still don't drop; SHM depth covers the wait.
                        await work_q.put((raw, rid))
                        break
            if not got:
                idle += 1
                if idle < 32:
                    await asyncio.sleep(0)
                    continue
                await asyncio.sleep(0.0002 if idle < 200 else 0.001)

        # Drain remaining work, then stop policy workers.
        await work_q.join()
        for _ in workers:
            await work_q.put(None)
        await asyncio.gather(*workers, return_exceptions=True)
        await engine.close()
        ring.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


def main() -> int:
    import ai_policy_engine as ape

    p = argparse.ArgumentParser(description="aivax shm ring multi-process consumer")
    p.add_argument("--shm-name", default=os.environ.get("AIVAX_SHM_NAME", NAME_DEFAULT))
    p.add_argument("--shm-meta", default=os.environ.get("AIVAX_SHM_META", META_DEFAULT))
    p.add_argument("--workers", type=int, default=int(os.environ.get("AIVAX_SHM_WORKERS", "8")))
    p.add_argument(
        "--cpu-list",
        default=os.environ.get("AIVAX_SHM_CPU_LIST", ""),
        help="comma-separated CPUs; pins one worker per CPU",
    )
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    p.add_argument("--payload-preview-bytes", type=int, default=512)
    p.add_argument("--log-payload", action="store_true")
    p.add_argument("--server-url", "--server_url", dest="server_url", default="")
    p.add_argument("--port", type=int, default=ape.DEFAULT_POLICY_SERVER_PORT)
    p.add_argument("--PipelineInputAPIPath", default=ape.DEFAULT_PIPELINE_INPUT_API_PATH)
    p.add_argument("--PipelineOutputAPIPath", default=ape.DEFAULT_PIPELINE_OUTPUT_API_PATH)
    p.add_argument("--policy-api-timeout-ms", type=int, default=ape.DEFAULT_POLICY_API_TIMEOUT_MS)
    p.add_argument("--policy-api-max-inflight", type=int, default=ape.DEFAULT_POLICY_API_MAX_INFLIGHT)
    p.add_argument("--policy-api-pool-limit", type=int, default=ape.DEFAULT_POLICY_API_POOL_LIMIT)
    p.add_argument(
        "--policy-api-acquire-timeout-ms",
        type=int,
        default=ape.DEFAULT_POLICY_API_ACQUIRE_TIMEOUT_MS,
    )
    p.add_argument(
        "--policy-api-failure-policy",
        default=ape.DEFAULT_POLICY_API_FAILURE_POLICY,
        choices=["open", "close"],
    )
    p.add_argument("--adapters-dir", default=ape.ADAPTERS_DIR)
    p.add_argument("--adapters-no-reload", action="store_true")
    p.add_argument(
        "--dyn-policy-file",
        default=ape._DEFAULT_DYN_POLICY_FILE,
        help="file_control_maxsize/file_allow_extension policy snapshot",
    )
    p.add_argument("--block-keywords", default="")
    p.add_argument(
        "--stats-path",
        default=os.environ.get(
            "AIVAX_SHM_CONSUMER_STATS",
            "/run/aivax/shm_consumer_stats.json",
        ),
    )
    p.add_argument("--duration", type=float, default=0.0, help=">0 이면 초 후 종료(벤치용)")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Wait for meta (producer creates ring)
    deadline = time.time() + 60
    n_rings = 0
    while time.time() < deadline:
        if os.path.exists(args.shm_meta):
            try:
                with open(args.shm_meta, encoding="utf-8") as f:
                    n_rings = int(json.load(f).get("n_rings", 0))
                if n_rings > 0:
                    break
            except Exception:
                pass
        time.sleep(0.1)
    if n_rings <= 0:
        # last resort: open shm directly
        probe = ShmRing(shm_name=args.shm_name, meta_path=args.shm_meta)
        probe.open(timeout_s=5.0)
        n_rings = probe.n_rings
        probe.close()
    if n_rings <= 0:
        logging.error("could not discover shm ring")
        return 2

    workers = max(1, min(args.workers, n_rings))
    # Assign rings round-robin to workers
    assignments: list[list[int]] = [[] for _ in range(workers)]
    for rid in range(n_rings):
        assignments[rid % workers].append(rid)

    stop_event = mp.Event()
    stats = mp.Value("Q", 0)
    ring_stats = mp.Array("Q", n_rings, lock=True)
    ring_errors = mp.Array("Q", n_rings, lock=True)
    worker_stats = mp.Array("Q", workers, lock=True)
    http_stats = mp.Array("Q", len(HTTP_KEYS), lock=True)
    service_stats = mp.Array("Q", len(SERVICE_KEYS), lock=True)
    args_ns = vars(args)
    try:
        import account_lookup as al

        al.prepare_shared_store(
            ttl_seconds=int(getattr(args, "account_cache_ttl", 3600) or 3600)
        )
    except Exception:
        logging.exception("account cache prepare failed; workers will retry")
    procs: list[mp.Process] = []
    for i in range(workers):
        if not assignments[i]:
            continue
        proc = mp.Process(
            target=_worker_main,
            args=(
                i,
                assignments[i],
                args.shm_name,
                args.shm_meta,
                args_ns,
                stop_event,
                stats,
                ring_stats,
                ring_errors,
                worker_stats,
                http_stats,
                service_stats,
            ),
            daemon=True,
        )
        proc.start()
        procs.append(proc)

    logging.info(
        "shm consumer supervisor workers=%d rings=%d assignments=%s",
        len(procs), n_rings, assignments[: len(procs)],
    )

    def _stop(*_a: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    t0 = time.time()
    last = 0
    try:
        while not stop_event.is_set():
            time.sleep(0.5)
            cur = stats.value
            dt = time.time() - t0
            eps = (cur - last) / 0.5
            last = cur
            if args.stats_path:
                stats_dir = os.path.dirname(os.path.abspath(args.stats_path))
                try:
                    os.makedirs(stats_dir, exist_ok=True)
                    tmp = args.stats_path + ".tmp"
                    gate_stats: Dict[str, Any] = {}
                    try:
                        from service_gate_stats import flush_to_disk, read_stats

                        flush_to_disk()
                        gate_stats = read_stats()
                    except Exception:
                        logging.debug("service_gate_stats snapshot failed", exc_info=True)
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(
                            {
                                "ts": time.time(),
                                "processed": cur,
                                "eps_instant": round(eps, 1),
                                "eps_avg": round(cur / dt, 1) if dt > 0 else 0,
                                "workers": len(procs),
                                "n_rings": n_rings,
                                "ring_processed": list(ring_stats[:]),
                                "ring_errors": list(ring_errors[:]),
                                "worker_processed": list(worker_stats[:]),
                                "http_version": counts_to_dict(HTTP_KEYS, list(http_stats[:])),
                                "ai_service": counts_to_dict(
                                    SERVICE_KEYS, list(service_stats[:])
                                ),
                                "service_gate": gate_stats,
                            },
                            f,
                        )
                    os.replace(tmp, args.stats_path)
                except OSError:
                    logging.warning("shm consumer stats write failed path=%s", args.stats_path)
            if args.duration > 0 and dt >= args.duration:
                break
    finally:
        stop_event.set()
        for proc in procs:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
        logging.info("shm consumer stopped processed=%d", stats.value)
    return 0


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    raise SystemExit(main())
