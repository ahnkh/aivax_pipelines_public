#!/usr/bin/env python3
"""Validate the local development runtime before starting Pipeline."""

from __future__ import annotations

import shutil
import sys
from importlib.util import find_spec
from pathlib import Path


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))


REQUIRED_MODULES = (
    "fastapi",
    "httpx",
    "magic",
    "orjson",
    "pytesseract",
    "uvicorn",
    "valkey",
)
REQUIRED_COMMANDS = ("file", "soffice", "tesseract")


def missing_requirements() -> list[str]:
    missing = [
        f"Python module: {name}" for name in REQUIRED_MODULES if find_spec(name) is None
    ]
    missing.extend(
        f"system command: {name}"
        for name in REQUIRED_COMMANDS
        if shutil.which(name) is None
    )
    return missing


def main() -> int:
    if sys.version_info[:2] != (3, 13):
        print("FAIL: Python 3.13 is required", file=sys.stderr)
        return 1

    missing = missing_requirements()
    if missing:
        print("FAIL: missing development prerequisites", file=sys.stderr)
        print("\n".join(f"- {item}" for item in missing), file=sys.stderr)
        return 1

    import main

    print(
        f"Pipeline development environment is ready (Python {sys.version.split()[0]})."
    )
    print(f"pipeline runtime imports: OK ({main.__file__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
