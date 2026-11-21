#!/usr/bin/env bash
# Reuse a complete wheelhouse; CI still validates installation offline every time.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${PYTHON_BIN:?}" "${UV_BIN:?}" "${AIVAX_VENV:?}" "${WHEEL_CACHE_STATE:?}"

fingerprint="$("$PYTHON_BIN" - "$ROOT" "$UV_BIN" <<'PY'
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
import sysconfig

root = Path(sys.argv[1])
runtime = {
    "python": sys.version,
    "platform": sysconfig.get_platform(),
    "abi": sysconfig.get_config_var("SOABI"),
    "libc": platform.libc_ver(),
    "uv": subprocess.check_output([sys.argv[2], "--version"], text=True).strip(),
}
digest = hashlib.sha256(json.dumps(runtime, sort_keys=True).encode())
for filename in (
    "requirements.txt", "requirements-dev.txt", "Makefile",
    "scripts/bootstrap_venv.sh", "scripts/ensure_ci_wheels.sh",
):
    digest.update(filename.encode())
    digest.update((root / filename).read_bytes())
print(digest.hexdigest())
PY
)"

if [[ -f "$WHEEL_CACHE_STATE/fingerprint" && -f "$WHEEL_CACHE_STATE/SHA256SUMS" ]] \
    && [[ "$(cat "$WHEEL_CACHE_STATE/fingerprint")" == "$fingerprint" ]] \
    && (cd "$ROOT" && sha256sum --check --status "$WHEEL_CACHE_STATE/SHA256SUMS"); then
    echo 'Reusing cached Pipeline wheels; skipping online setup and pip wheel.'
    exit 0
fi

echo 'Building Pipeline wheels: cache missing, changed or incomplete.'
rm -rf -- "$ROOT/offline-wheel" "$WHEEL_CACHE_STATE"
make -C "$ROOT" "PYTHON=$PYTHON_BIN" "UV=$UV_BIN" "VENV=$AIVAX_VENV" setup
make -C "$ROOT" "VENV=$AIVAX_VENV" wheel
mkdir -p "$WHEEL_CACHE_STATE"
(
    cd "$ROOT"
    find offline-wheel -maxdepth 1 -type f -name '*.whl' -print0 \
        | sort -z | xargs -0 -r sha256sum
) > "$WHEEL_CACHE_STATE/SHA256SUMS"
test -s "$WHEEL_CACHE_STATE/SHA256SUMS"
printf '%s\n' "$fingerprint" > "$WHEEL_CACHE_STATE/fingerprint"
