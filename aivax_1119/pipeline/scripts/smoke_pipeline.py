#!/usr/bin/env python3
"""Check that a prepared Pipeline instance exposes its API contract.

Run ``make smoke`` after starting ``make dev`` in another terminal.  Override
the target for a remote or alternate instance with
``AIVAX_PIPELINE_SMOKE_URL=http://127.0.0.1:19099 make smoke``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

from src.bootstrap.env_loader import SETTINGS


def _load_json(url: str) -> dict:
    try:
        with urlopen(url, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            return json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        raise RuntimeError(f"request failed: {url}: {error}") from error


def main() -> int:
    configured_url = os.environ.get("AIVAX_PIPELINE_SMOKE_URL", "").rstrip("/")
    scheme = "https" if SETTINGS.ssl_cert_file else "http"
    base_url = configured_url or f"{scheme}://{SETTINGS.host}:{SETTINGS.port}"

    try:
        status = _load_json(f"{base_url}/")
        if status != {"status": True}:
            raise RuntimeError(f"unexpected status response: {status!r}")

        openapi = _load_json(f"{base_url}/openapi.json")
        paths = openapi.get("paths", {})
        required_paths = {"/v1/filter/multiple_filter", "/v1/filter/output_filter"}
        missing_paths = sorted(required_paths - set(paths))
        if missing_paths:
            raise RuntimeError(
                f"missing Pipeline API paths: {', '.join(missing_paths)}"
            )
    except RuntimeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "url": base_url, "paths": sorted(required_paths)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
