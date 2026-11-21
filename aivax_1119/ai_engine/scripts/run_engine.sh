#!/bin/bash
# AI Policy Engine 실행 래퍼
# 기본: Inline ChatGate UDS (4B length JSON) | 옵션: Suricata Lua eve | shm
# SSLproxy는 사용하지 않음.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}"
export PYTHONPATH="${ROOT}:${ROOT}/core:${ROOT}/mirror${PYTHONPATH:+:${PYTHONPATH}}"

MODE="default"
LOG_LEVEL="${LOG_LEVEL:-INFO}"
PAYLOAD_PREVIEW_BYTES="${PAYLOAD_PREVIEW_BYTES:-256}"
LOG_PAYLOAD="${AIVAX_LOG_PAYLOAD:-0}"
# Explicit socket only (CLI / AIVAX_POLICY_SOCKET / legacy SSLPROXY_SOCKET env).
# Do not default-bind a second socket in eve mode.
POLICY_SOCKET_CLI=""
SURICATA_EVE_SOCKET="${SURICATA_EVE_SOCKET:-/tmp/suricata-eve.sock}"
DEFAULT_POLICY_SOCKET="/run/aivax/policy.sock"
EXTRA_ARGS=()
# Host shared/default python — do not create an ai_engine-private venv.
if [[ -x /home1/aivax/aivax-venv/bin/python3 ]]; then
    PY=/home1/aivax/aivax-venv/bin/python3
elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python3" ]]; then
    PY="${VIRTUAL_ENV}/bin/python3"
else
    PY="$(command -v python3 || echo /usr/bin/python3)"
fi

usage() {
    cat <<'EOF'
Usage: scripts/run_engine.sh [OPTIONS] [-- EXTRA_ARGS...]

Options:
  -m, --mode MODE           default | suricata | shm  (default: default)
  -l, --log-level LEVEL     로그 레벨 (기본: INFO, 또는 $LOG_LEVEL)
  -p, --payload-preview-bytes N
      --policy-socket PATH  ChatGate UDS (기본: /run/aivax/policy.sock)
      --sslproxy-socket PATH
                            레거시 alias of --policy-socket (SSLproxy 사용 안 함)
      --suricata-socket PATH
  -h, --help

Modes:
  default    Inline ChatGate UDS 프레임 (aivax-engine-inline 과 동일 바이너리)
  suricata   Suricata Lua eve Mode
  shm        Suricata SHM ring consumer (Mirror 기본)

Examples:
  ./scripts/run_engine.sh
  ./scripts/run_engine.sh -m shm -- --workers 8 --server-url ''
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--mode)
            MODE="${2:?mode 값이 필요합니다 (default|suricata|shm)}"
            shift 2
            ;;
        -l|--log-level)
            LOG_LEVEL="${2:?log level 값이 필요합니다}"
            shift 2
            ;;
        -p|--payload-preview-bytes)
            PAYLOAD_PREVIEW_BYTES="${2:?payload-preview-bytes 값이 필요합니다}"
            shift 2
            ;;
        --policy-socket|--sslproxy-socket)
            POLICY_SOCKET_CLI="${2:?policy socket 경로가 필요합니다}"
            shift 2
            ;;
        --suricata-socket)
            SURICATA_EVE_SOCKET="${2:?suricata socket 경로가 필요합니다}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            EXTRA_ARGS+=("$@")
            break
            ;;
        -*)
            echo "알 수 없는 옵션: $1" >&2
            usage >&2
            exit 1
            ;;
        *)
            echo "알 수 없는 인자: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ -n "${POLICY_SOCKET_CLI}" ]]; then
    POLICY_SOCKET="${POLICY_SOCKET_CLI}"
    POLICY_SOCKET_EXPLICIT=1
elif [[ -n "${AIVAX_POLICY_SOCKET:-}" ]]; then
    POLICY_SOCKET="${AIVAX_POLICY_SOCKET}"
    POLICY_SOCKET_EXPLICIT=1
elif [[ -n "${SSLPROXY_SOCKET:-}" ]]; then
    # 레거시 env 이름만 허용. 기본값을 /tmp/sslproxy-… 로 채우지 않음.
    POLICY_SOCKET="${SSLPROXY_SOCKET}"
    POLICY_SOCKET_EXPLICIT=1
else
    POLICY_SOCKET="${DEFAULT_POLICY_SOCKET}"
    POLICY_SOCKET_EXPLICIT=0
fi

COMMON_ARGS=(
    --log-level "${LOG_LEVEL}"
    --payload-preview-bytes "${PAYLOAD_PREVIEW_BYTES}"
    --socket-mode "${AIVAX_SOCKET_MODE:-660}"
)
if [[ "${LOG_PAYLOAD}" == "1" || "${LOG_PAYLOAD}" == "true" || "${LOG_PAYLOAD}" == "yes" ]]; then
    COMMON_ARGS+=(--log-payload)
fi

case "${MODE}" in
    default|sslproxy|chatgate|inline)
        SOCKET="${POLICY_SOCKET}"
        EXTRA_SOCKETS=()
        ;;
    suricata|eve)
        SOCKET="${AIVAX_EVE_SOCKET:-${SURICATA_EVE_SOCKET}}"
        EXTRA_SOCKETS=()
        if [[ -n "${AIVAX_EVE_SOCKET_B:-}" ]]; then
            EXTRA_SOCKETS+=("${AIVAX_EVE_SOCKET_B}")
        elif [[ -d /run/aivax/card1 ]]; then
            EXTRA_SOCKETS+=("/run/aivax/card1/suricata-eve.sock")
        fi
        if [[ "${POLICY_SOCKET_EXPLICIT}" == "1" ]]; then
            EXTRA_SOCKETS+=("${POLICY_SOCKET}")
        fi
        ;;
    shm|suricata-shm)
        echo "[run_engine] mode=shm shm_name=${AIVAX_SHM_NAME:-aivax_eve_ring} log_level=${LOG_LEVEL}" >&2
        echo "[run_engine] Suricata plugin must create the ring first (export mode auto/shm)." >&2
        SHM_ARGS=(
            --log-level "${LOG_LEVEL}"
            --payload-preview-bytes "${PAYLOAD_PREVIEW_BYTES}"
            --adapters-no-reload
            --workers "${AIVAX_SHM_WORKERS:-16}"
        )
        if [[ -n "${AIVAX_SHM_CPU_LIST:-}" ]]; then
            SHM_ARGS+=(--cpu-list "${AIVAX_SHM_CPU_LIST}")
        fi
        if [[ -n "${AIVAX_POLICY_API_TIMEOUT_MS:-}" ]]; then
            SHM_ARGS+=(--policy-api-timeout-ms "${AIVAX_POLICY_API_TIMEOUT_MS}")
        fi
        if [[ -n "${AIVAX_POLICY_SERVER_URL:-}" ]]; then
            _purl="${AIVAX_POLICY_SERVER_URL#http://}"
            _purl="${_purl#https://}"
            SHM_ARGS+=(--server-url "http://${_purl%%:*}" --port "${_purl##*:}")
        fi
        if [[ "${LOG_PAYLOAD}" == "1" || "${LOG_PAYLOAD}" == "true" || "${LOG_PAYLOAD}" == "yes" ]]; then
            SHM_ARGS+=(--log-payload)
        fi
        exec "${PY}" -u "${ROOT}/mirror/shm_ring_consumer.py" \
            "${SHM_ARGS[@]}" \
            ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
        ;;
    *)
        echo "알 수 없는 mode: ${MODE} (default|suricata|shm)" >&2
        usage >&2
        exit 1
        ;;
esac

echo "[run_engine] mode=${MODE} socket=${SOCKET} log_level=${LOG_LEVEL}" >&2
echo "[run_engine] cwd=$(pwd) PYTHONPATH=${PYTHONPATH}" >&2
if [[ ${#EXTRA_SOCKETS[@]} -gt 0 ]]; then
    echo "[run_engine] also listening extra sockets: ${EXTRA_SOCKETS[*]}" >&2
fi

ENGINE_ARGS=(
    --socket "${SOCKET}"
    "${COMMON_ARGS[@]}"
)
for es in "${EXTRA_SOCKETS[@]+"${EXTRA_SOCKETS[@]}"}"; do
    ENGINE_ARGS+=("--extra-socket" "${es}")
done

exec "${PY}" -u "${ROOT}/core/ai_policy_engine.py" \
    "${ENGINE_ARGS[@]}" \
    ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
