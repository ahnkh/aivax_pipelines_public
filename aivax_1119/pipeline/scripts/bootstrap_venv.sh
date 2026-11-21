#!/usr/bin/env bash
# Create a repository-local Pipeline virtual environment.
# The approved dependency source is requirements.txt.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3.13}"
VENV="${ROOT}/.venv"
VENV="${AIVAX_VENV:-${VENV}}"
UV_BIN="${UV_BIN:-uv}"
INSTALL_DEV="${INSTALL_DEV:-1}"

run_uv() {
  env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
    -u ALL_PROXY -u all_proxy -u NO_PROXY -u no_proxy \
    "${UV_BIN}" "$@"
}

if [[ ! -x "${VENV}/bin/python" ]]; then
  if ! command -v "${UV_BIN}" >/dev/null 2>&1; then
    echo "ERROR: ${UV_BIN} was not found. Install uv or set UV_BIN." >&2
    exit 1
  fi

  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "ERROR: ${PYTHON_BIN} was not found. Install Python 3.13 or set PYTHON_BIN." >&2
    exit 1
  fi

  run_uv venv --python "${PYTHON_BIN}" "${VENV}"
fi

if ! "${VENV}/bin/python" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 13))'; then
  echo "ERROR: ${VENV} is not a Python 3.13 environment." >&2
  exit 1
fi

run_uv pip install --python "${VENV}/bin/python" --requirement "${ROOT}/requirements.txt"
if [[ "${INSTALL_DEV}" == "1" ]]; then
  run_uv pip install --python "${VENV}/bin/python" --requirement "${ROOT}/requirements-dev.txt"
fi

"${VENV}/bin/python" - <<'PY'
import aiohttp
import fastapi
import main
import orjson
import requests
import uvicorn
import uvloop

print("Pipeline environment is ready.")
PY

if [[ "${INSTALL_DEV}" == "1" ]]; then
  echo "Run: make dev"
else
  echo "Run: make prod"
fi
