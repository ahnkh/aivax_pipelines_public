#!/bin/bash
# AI Engine SHM consumer for lab profile ens4f0+shm.
# Start Suricata first, then:
#   ./scripts/run_ens4f0_shm.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
AIVAX_ROOT="$(cd "${ENGINE_ROOT}/.." && pwd)"
LAB_ENV="${AIVAX_ROOT}/mirror_engine/etc/lab/deploy-profile-ens4f0-shm.env"

if [[ -f "$LAB_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$LAB_ENV"
  set +a
fi

cd "${ENGINE_ROOT}"
export PYTHONPATH="${ENGINE_ROOT}:${ENGINE_ROOT}/core:${ENGINE_ROOT}/mirror${PYTHONPATH:+:${PYTHONPATH}}"

EXTRA=(
  --workers "${AIVAX_SHM_WORKERS:-16}"
  --cpu-list "${AIVAX_SHM_CPU_LIST:-0,1,2,3,4,5,6,7,8,9,10,11,16,17,18,19}"
)

if [[ "${AIVAX_POLICY_API:-0}" == "1" ]]; then
  url="${AIVAX_POLICY_SERVER_URL:-http://127.0.0.1:9099}"
  host_port="${url#http://}"
  host_port="${host_port#https://}"
  EXTRA+=(--server-url "http://${host_port%%:*}" --port "${host_port##*:}")
  EXTRA+=(--policy-api-timeout-ms "${AIVAX_POLICY_API_TIMEOUT_MS:-60000}")
else
  EXTRA+=(--server-url "")
fi

META="${AIVAX_SHM_META:-/tmp/aivax_eve_ring.meta.json}"
echo "[run_ens4f0_shm] waiting for SHM meta: ${META}"
for _ in $(seq 1 90); do
  [[ -f "$META" ]] && break
  sleep 0.5
done
if [[ ! -f "$META" ]]; then
  echo "[run_ens4f0_shm] ERROR: SHM meta missing — start Suricata first" >&2
  echo "  cd ${AIVAX_ROOT}/mirror_engine && sudo -E ./run.sh start" >&2
  exit 1
fi

echo "[run_ens4f0_shm] mode=shm workers=${AIVAX_SHM_WORKERS:-16} cpus=${AIVAX_SHM_CPU_LIST:-} policy_timeout_ms=${AIVAX_POLICY_API_TIMEOUT_MS:-60000}"
exec "${SCRIPT_DIR}/run_engine.sh" -m shm -- "${EXTRA[@]}" "$@"
