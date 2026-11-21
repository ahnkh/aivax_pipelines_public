#!/usr/bin/env bash
# Stop/disable Mirror engine units and remove their files.
# Usage: sudo bash deploy/mirror/uninstall.sh
#        REMOVE_ENV=1 / REMOVE_MODE=1 sudo -E bash ...
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/../common/_lib.sh"
_aivax_deploy_init "$HERE"
SYSTEMD=/etc/systemd/system
REMOVE_ENV="${REMOVE_ENV:-${REMOVE_COMMON:-0}}"
REMOVE_MODE="${REMOVE_MODE:-0}"

UNITS=(
  aivax-engine-mirror.service
  aivax-engine-shm.service
  aivax-engine-metrics.service
  aivax-engine-lifecycle.service
)

for u in "${UNITS[@]}"; do
  systemctl disable --now "$u" 2>/dev/null || true
  rm -f "$SYSTEMD/$u"
  rmdir "$SYSTEMD/${u}.d" 2>/dev/null || true
  systemctl reset-failed "$u" 2>/dev/null || true
done

systemctl daemon-reload

if [[ "$REMOVE_ENV" == "1" ]]; then
  bash "${HERE}/../common/uninstall_common.sh"
fi

if [[ "$REMOVE_MODE" == "1" ]]; then
  rm -f /etc/aivax/mode.json
  echo "Removed /etc/aivax/mode.json"
elif [[ "${SKIP_MODE_JSON:-0}" != "1" ]]; then
  if systemctl is-active --quiet aivax-engine-inline.service 2>/dev/null; then
    echo "mode.json: leave as-is (aivax-engine-inline still active)"
  else
    _aivax_write_mode_json unknown
  fi
fi

echo "Mirror uninstalled (aivax-engine-mirror + metrics/lifecycle; legacy shm cleared)."
