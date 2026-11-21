#!/usr/bin/env python3
"""Cross-worker Valkey merge soak: INPUT multiple_filter → OUTPUT output_filter.

Verifies shared merge by checking Valkey key presence after INPUT and absence
after OUTPUT (GETDEL). Concurrent pairs stress different uvicorn workers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib.error
import urllib.request


def _http_json(url: str, body: dict, timeout_s: float) -> tuple[int, dict]:
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read()
            try:
                obj = json.loads(data.decode("utf-8", "replace")) if data else {}
            except Exception:
                obj = {"_raw": data[:200].decode("utf-8", "replace")}
            return int(resp.status), obj if isinstance(obj, dict) else {"_body": obj}
    except urllib.error.HTTPError as e:
        body_b = e.read() if hasattr(e, "read") else b""
        try:
            obj = json.loads(body_b.decode("utf-8", "replace")) if body_b else {}
        except Exception:
            obj = {"_error": str(e)}
        return int(e.code), obj if isinstance(obj, dict) else {"_error": str(e)}
    except Exception as e:
        return 0, {"_error": str(e)}


def _valkey_client():
    from valkey import Valkey

    host = os.environ.get("AIVAX_PIPELINE_VALKEY_HOST", "127.0.0.1")
    port = int(os.environ.get("AIVAX_PIPELINE_VALKEY_PORT", "6379"))
    return Valkey(
        host=host, port=port, db=0, socket_connect_timeout=1.0, socket_timeout=1.0
    )


def one_pair(
    base: str,
    prefix: str,
    timeout_s: float,
    use_slm_skip_path: bool,
) -> dict:
    # Decimal-only id: hex UUIDs in the prompt can trip passport/secret regex → BLOCK.
    # Keep message_id unique; do NOT embed raw hex into prompt text.
    mid = f"{prefix}-{uuid.uuid4().int % 10**16:016d}"
    # Prefer light filter_list so soak measures merge, not SLM RTT
    filter_list = (
        ["input_filter", "secret_filter"]
        if use_slm_skip_path
        else ["input_filter", "secret_filter", "slm_filter"]
    )
    inp = {
        "filter_list": filter_list,
        "prompt": "merge soak prompt safe text only",
        "encoding": False,
        "user_id": "soak-u",
        "email": "soak@example.com",
        "ai_service": 1,
        "client_host": "127.0.0.1",
        "session_id": f"sess-{mid}",
        "attachments": [{"id": "", "size": 0, "name": "", "mime_type": ""}],
        "message_id": mid,
        "debug": False,
    }
    out = {
        "llm_output": "assistant reply for soak pair",
        "user_id": "soak-u",
        "email": "soak@example.com",
        "ai_service": 1,
        "client_host": "127.0.0.1",
        "session_id": f"sess-{mid}",
        "message_id": mid,
        "debug": False,
    }
    st_in, body_in = _http_json(f"{base}/v1/filter/multiple_filter", inp, timeout_s)
    if st_in != 200:
        return {"ok": False, "message_id": mid, "stage": "input", "status": st_in}
    action = ((body_in or {}).get("final_decision") or {}).get("action")
    if action and str(action).lower() not in ("allow", "masking", "mask"):
        return {
            "ok": False,
            "message_id": mid,
            "stage": "input_action",
            "status": st_in,
            "detail": f"unexpected action={action}",
        }

    prefix = os.environ.get(
        "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_KEY_PREFIX",
        "pipeline:analysis-log-merge:v1:",
    )
    key = f"{prefix}{mid}"
    vk = _valkey_client()
    present = False
    for _ in range(50):  # up to ~500ms poll (threadpool/valkey settle)
        if vk.exists(key) == 1:
            present = True
            break
        time.sleep(0.01)
    if not present:
        return {
            "ok": False,
            "message_id": mid,
            "stage": "valkey_after_input",
            "status": st_in,
            "detail": "key missing after INPUT",
        }

    st_out, _ = _http_json(f"{base}/v1/filter/output_filter", out, timeout_s)
    if st_out != 200:
        return {"ok": False, "message_id": mid, "stage": "output", "status": st_out}

    gone = vk.exists(key) == 0
    if not gone:
        return {
            "ok": False,
            "message_id": mid,
            "stage": "valkey_after_output",
            "status": st_out,
            "detail": "key still present after OUTPUT (GETDEL miss)",
        }
    return {"ok": True, "message_id": mid, "status_in": st_in, "status_out": st_out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9099")
    ap.add_argument("--pairs", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--timeout-s", type=float, default=30.0)
    ap.add_argument("--prefix", default="soak")
    ap.add_argument(
        "--include-slm",
        action="store_true",
        help="include slm_filter in filter_list (default: omit for merge-focused soak)",
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    # ping valkey
    try:
        _valkey_client().ping()
    except Exception as exc:
        print(f"FAIL valkey ping: {exc}", file=sys.stderr)
        return 2

    t0 = time.time()
    ok = 0
    fails: list[dict] = []
    use_skip_path = not args.include_slm
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = [
            ex.submit(
                one_pair,
                args.base.rstrip("/"),
                args.prefix,
                args.timeout_s,
                use_skip_path,
            )
            for _ in range(args.pairs)
        ]
        for fut in as_completed(futs):
            r = fut.result()
            if r.get("ok"):
                ok += 1
            else:
                fails.append(r)

    elapsed = time.time() - t0
    report = {
        "ok": ok == args.pairs and len(fails) == 0,
        "pairs": args.pairs,
        "success": ok,
        "fail": len(fails),
        "success_rate": ok / args.pairs if args.pairs else 0.0,
        "elapsed_s": elapsed,
        "concurrency": args.concurrency,
        "base": args.base,
        "include_slm": bool(args.include_slm),
        "failures_sample": fails[:10],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
            f.write("\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
