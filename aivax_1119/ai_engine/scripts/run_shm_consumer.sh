#!/usr/bin/env bash
set -euo pipefail
ROOT="${AIVAX_ENGINE_ROOT:-/home1/aivax/ai_engine}"
PY="${AIVAX_PYTHON:-/home1/aivax/aivax-venv/bin/python3}"
# Defaults match deploy/mirror/aivax-engine-mirror.service (1G). Override via ai_engine.env.
W="${AIVAX_SHM_WORKERS:-8}"
CPL="${AIVAX_SHM_CPU_LIST:-18,19,20,21,22,23,36,37}"
META="${AIVAX_SHM_META:-/tmp/aivax_eve_ring.meta.json}"
NAME="${AIVAX_SHM_NAME:-aivax_eve_ring}"
export PYTHONPATH="${ROOT}:${ROOT}/core:${ROOT}/mirror${PYTHONPATH:+:${PYTHONPATH}}"
cd "$ROOT"

EXTRA=(
  --adapters-no-reload
  --workers "$W"
  --cpu-list "$CPL"
  --shm-name "$NAME"
  --shm-meta "$META"
  --log-level "${LOG_LEVEL:-INFO}"
  --policy-api-timeout-ms "${AIVAX_POLICY_API_TIMEOUT_MS:-15000}"
  --policy-api-acquire-timeout-ms "${AIVAX_POLICY_API_ACQUIRE_TIMEOUT_MS:-200}"
  --policy-api-max-inflight 256
  --policy-api-pool-limit 256
)

# URL host/port split stays in this script (not the systemd unit) so bash
# ${hp%%:*} is not mangled by systemd's %% → % specifier pass.
if [[ "${AIVAX_POLICY_API:-0}" == "1" ]]; then
  url="${AIVAX_POLICY_SERVER_URL:-http://127.0.0.1:9099}"
  hp="${url#http://}"
  hp="${hp#https://}"
  host="${hp%%:*}"
  port="${hp##*:}"
  EXTRA+=(--server-url "http://${host}" --port "${port}")
else
  EXTRA+=(--server-url "")
fi

exec "$PY" -u "$ROOT/mirror/shm_ring_consumer.py" "${EXTRA[@]}"
