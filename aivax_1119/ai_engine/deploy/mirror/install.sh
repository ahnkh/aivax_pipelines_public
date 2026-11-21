#!/usr/bin/env bash
# Install Mirror engine (+ metrics/lifecycle).
# Usage: sudo bash deploy/mirror/install.sh
#        SKIP_ENV=1 INSTALL_METRICS=0 INSTALL_LIFECYCLE=0 sudo -E bash ...
#        SKIP_MODE_SWITCH=1 / SKIP_ATTACH_CLEANUP=1 / SKIP_MODE_JSON=1
#
# /etc/aivax/aivax.env (if present) overlays into ai_engine.env:
#   AIVAX_SHM_WORKERS, AIVAX_SHM_CPU_LIST, AIVAX_SHM_MAX_INFLIGHT,
#   AIVAX_POLICY_API_TIMEOUT_MS, AIVAX_POLICY_SERVER_URL, AIVAX_CPU_AI_ENGINE, …
# SKIP_ENV=1 keeps an existing ai_engine.env but still applies that overlay.
#
# Migrates legacy aivax-engine-shm → aivax-engine-mirror.
# Writes /etc/aivax/mode.json (mode=mirror) for management UI.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/../common/_lib.sh"
_aivax_deploy_init "$HERE"

SKIP_ENV="${SKIP_ENV:-${SKIP_COMMON:-0}}"
INSTALL_METRICS="${INSTALL_METRICS:-1}"
INSTALL_LIFECYCLE="${INSTALL_LIFECYCLE:-1}"

if [[ "$SKIP_ENV" != "1" ]]; then
  _aivax_install_engine_env
else
  echo "SKIP_ENV=1 — leave ai_engine.env template alone, still overlay from aivax.env"
  _aivax_overlay_engine_env_from_site
fi

_aivax_prepare_mode_switch mirror

chmod +x "${ENGINE_ROOT}/scripts/mount_attach_tmpfs.sh" 2>/dev/null || true
chmod +x "${ENGINE_ROOT}/scripts/run_shm_consumer.sh"

# Retire legacy SHM unit name
if [[ -f "$SYSTEMD/aivax-engine-shm.service" ]] || systemctl cat aivax-engine-shm.service &>/dev/null; then
  echo "migrate: disable/remove legacy aivax-engine-shm.service"
  systemctl disable --now aivax-engine-shm.service 2>/dev/null || true
  rm -f "$SYSTEMD/aivax-engine-shm.service"
  rmdir "$SYSTEMD/aivax-engine-shm.service.d" 2>/dev/null || true
  systemctl reset-failed aivax-engine-shm.service 2>/dev/null || true
fi

_aivax_render <"${HERE}/aivax-engine-mirror.service" >"$SYSTEMD/aivax-engine-mirror.service"

UNITS=(aivax-engine-mirror.service)
if [[ "$INSTALL_METRICS" == "1" ]]; then
  _aivax_render <"${HERE}/aivax-engine-metrics.service" >"$SYSTEMD/aivax-engine-metrics.service"
  UNITS+=(aivax-engine-metrics.service)
fi
if [[ "$INSTALL_LIFECYCLE" == "1" ]]; then
  _aivax_render <"${HERE}/aivax-engine-lifecycle.service" >"$SYSTEMD/aivax-engine-lifecycle.service"
  UNITS+=(aivax-engine-lifecycle.service)
fi

systemctl daemon-reload
for u in "${UNITS[@]}"; do
  systemctl enable "$u"
done

META="${AIVAX_SHM_META:-/tmp/aivax_eve_ring.meta.json}"
SHM_NAME="${AIVAX_SHM_NAME:-aivax_eve_ring}"
SEG="/dev/shm/${SHM_NAME}"

if systemctl is-active --quiet aivax-mirror.service 2>/dev/null && [[ -f "$META" && -e "$SEG" ]]; then
  systemctl start aivax-engine-mirror.service
  echo "Mirror engine started (aivax-mirror active + meta + segment OK)."
else
  echo "Mirror engine enabled but NOT started."
  echo "  Need: aivax-mirror.service active, plus $META and $SEG"
  echo "  Then: systemctl start aivax-engine-mirror"
fi

if [[ "$INSTALL_METRICS" == "1" ]]; then
  systemctl start aivax-engine-metrics.service || true
fi
if [[ "$INSTALL_LIFECYCLE" == "1" ]]; then
  systemctl start aivax-engine-lifecycle.service || true
fi

echo
echo "[mode.json] product mode → mirror"
_aivax_write_mode_json mirror

echo "Mirror installed."
echo "  units: ${UNITS[*]}"
echo "  env:   /etc/aivax/ai_engine.env"
echo "  mode:  /etc/aivax/mode.json"
echo "  doc:   ${ENGINE_ROOT}/docs/DEPLOY_MIRROR_SHM_1G.md"
for u in "${UNITS[@]}"; do
  systemctl is-enabled "$u" || true
  systemctl is-active "$u" || true
done
