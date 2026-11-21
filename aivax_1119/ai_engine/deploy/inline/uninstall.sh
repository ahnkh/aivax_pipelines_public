#!/usr/bin/env bash
# Stop/disable Inline engine unit and remove its files.
# Usage: sudo bash deploy/inline/uninstall.sh
#        REMOVE_ENV=1 sudo -E bash deploy/inline/uninstall.sh
#        REMOVE_MODE=1 — delete /etc/aivax/mode.json instead of mode=unknown
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/../common/_lib.sh"
_aivax_deploy_init "$HERE"
SYSTEMD=/etc/systemd/system
REMOVE_ENV="${REMOVE_ENV:-${REMOVE_COMMON:-0}}"
REMOVE_MODE="${REMOVE_MODE:-0}"

for u in aivax-engine-inline.service aivax-engine.service aivax-policy.service; do
  systemctl disable --now "$u" 2>/dev/null || true
  rm -f "$SYSTEMD/$u" "$SYSTEMD/${u}.d/cpu.conf"
  rmdir "$SYSTEMD/${u}.d" 2>/dev/null || true
  systemctl reset-failed "$u" 2>/dev/null || true
done

systemctl daemon-reload

if [[ "$REMOVE_ENV" == "1" ]]; then
  bash "${HERE}/../common/uninstall_common.sh"
fi

# Mark product mode unknown unless Mirror is still the active product intent
# (mirror install would have rewritten mode.json). Stale inline → unknown.
if [[ "$REMOVE_MODE" == "1" ]]; then
  rm -f /etc/aivax/mode.json
  echo "Removed /etc/aivax/mode.json"
elif [[ "${SKIP_MODE_JSON:-0}" != "1" ]]; then
  if systemctl is-active --quiet aivax-engine-mirror.service 2>/dev/null; then
    echo "mode.json: leave as-is (aivax-engine-mirror still active)"
  else
    _aivax_write_mode_json unknown
  fi
fi

echo "Inline uninstalled (aivax-engine-inline; legacy names cleared if present)."
