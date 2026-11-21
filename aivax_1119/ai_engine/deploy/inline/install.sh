#!/usr/bin/env bash
# Install + start Inline ChatGate engine (aivax-engine-inline).
#
# Default: air-gap friendly (no PyPI). Deps must already be in
# /home1/aivax/aivax-venv (aiohttp, brotli, zstandard).
#
# Usage:
#   sudo bash deploy/inline/install.sh
#   SKIP_PIP=0 sudo -E bash deploy/inline/install.sh
#
# Knobs: SKIP_ENV SKIP_PIP SKIP_START DRY_RUN SKIP_MODE_SWITCH SKIP_ATTACH_CLEANUP
#        SKIP_MODE_JSON
# Writes /etc/aivax/mode.json (mode=inline) for management UI.
#
# Migrates legacy units: aivax-policy / aivax-engine → aivax-engine-inline
#
# Doc: docs/INSTALLER_INLINE.md · docs/DEPLOY_INLINE_1G.md · docs/MODE_INLINE_MIRROR.md
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/../common/_lib.sh"
_aivax_deploy_init "$HERE"

SKIP_ENV="${SKIP_ENV:-${SKIP_COMMON:-0}}"
SKIP_PIP="${SKIP_PIP:-1}"
SKIP_START="${SKIP_START:-0}"
DRY_RUN="${DRY_RUN:-0}"

_run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  DRY  $*"
  else
    "$@"
  fi
}

_retire_legacy_inline_units() {
  local u
  for u in aivax-engine.service aivax-policy.service; do
    if [[ -f "$SYSTEMD/$u" ]] || systemctl cat "$u" &>/dev/null; then
      echo "  migrate: disable/remove legacy $u"
      systemctl disable --now "$u" 2>/dev/null || true
      rm -f "$SYSTEMD/$u" "$SYSTEMD/${u}.d/cpu.conf"
      rmdir "$SYSTEMD/${u}.d" 2>/dev/null || true
      systemctl reset-failed "$u" 2>/dev/null || true
    fi
  done
}

echo "=== ai_engine Inline install (aivax-engine-inline) ==="
echo "  ENGINE_ROOT = $ENGINE_ROOT"
echo "  PYTHON      = $AIVAX_PYTHON"
echo "  LOG         = $ENGINE_LOG"
echo "  SYSTEMD     = $SYSTEMD"
echo "  SKIP_ENV=$SKIP_ENV SKIP_PIP=$SKIP_PIP SKIP_START=$SKIP_START DRY_RUN=$DRY_RUN"
echo

if [[ ! -f "${ENGINE_ROOT}/core/ai_policy_engine.py" ]]; then
  echo "ERROR: ${ENGINE_ROOT}/core/ai_policy_engine.py missing" >&2
  exit 1
fi
if [[ ! -x "$AIVAX_PYTHON" && ! -e "$AIVAX_PYTHON" ]]; then
  echo "ERROR: python not found: $AIVAX_PYTHON" >&2
  exit 1
fi

if [[ "$SKIP_ENV" == "1" ]]; then
  echo "[1/5] SKIP_ENV=1 — leave ai_engine.env template alone, still overlay from aivax.env"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  DRY  _aivax_overlay_engine_env_from_site"
  else
    _aivax_overlay_engine_env_from_site
  fi
else
  echo "[1/5] install /etc/aivax/ai_engine.env"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  DRY  _aivax_install_engine_env"
  else
    _aivax_install_engine_env
  fi
fi

if [[ "$SKIP_PIP" == "1" ]]; then
  echo "[2/5] SKIP_PIP=1 (air-gap default) — expecting deps in $AIVAX_PYTHON"
  if [[ "$DRY_RUN" != "1" ]]; then
    if ! "$AIVAX_PYTHON" -c "import aiohttp, brotli, zstandard" 2>/dev/null; then
      echo "ERROR: aiohttp/brotli/zstandard missing in $AIVAX_PYTHON" >&2
      exit 1
    fi
  fi
else
  echo "[2/5] pip install -r requirements.txt → $AIVAX_PYTHON"
  REQ="${ENGINE_ROOT}/requirements.txt"
  if [[ -f "$REQ" ]]; then
    pip_bin="$(dirname "$AIVAX_PYTHON")/pip"
    if [[ "$DRY_RUN" == "1" ]]; then
      echo "  DRY  $pip_bin install -r $REQ"
    else
      if [[ -x "$pip_bin" ]]; then
        "$pip_bin" install -r "$REQ"
      else
        "$AIVAX_PYTHON" -m pip install -r "$REQ"
      fi
    fi
  else
    echo "  WARN: missing $REQ — skip pip"
  fi
fi

echo "[2b/5] mode-switch prepare (stop Mirror, reconcile/wipe attachments)"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "  DRY  _aivax_prepare_mode_switch inline"
else
  _aivax_prepare_mode_switch inline
fi

echo "[3/5] render aivax-engine-inline.service → $SYSTEMD"
UNIT_DST="$SYSTEMD/aivax-engine-inline.service"
DROPIN_DIR="$SYSTEMD/aivax-engine-inline.service.d"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "  DRY  migrate legacy aivax-engine / aivax-policy"
  echo "  DRY  render ${HERE}/aivax-engine-inline.service > $UNIT_DST"
  _aivax_render <"${HERE}/aivax-engine-inline.service" | grep -E '^(ExecStart|WorkingDirectory|Environment=PYTHONPATH|StandardOutput)=' | sed 's/^/  /'
else
  _retire_legacy_inline_units
  mkdir -p "$DROPIN_DIR"
  _aivax_render <"${HERE}/aivax-engine-inline.service" >"$UNIT_DST"
  install -m 0644 "${HERE}/aivax-engine-inline.cpu.conf" "$DROPIN_DIR/cpu.conf"
fi

echo "[4/5] systemctl daemon-reload"
_run systemctl daemon-reload

echo "[5/5] enable --now"
if [[ "$SKIP_START" == "1" ]]; then
  echo "  SKIP_START=1 — unit installed, not started"
  if [[ "$DRY_RUN" != "1" ]]; then
    systemctl enable aivax-engine-inline.service || true
  fi
else
  if systemctl cat aivax-valkey.service &>/dev/null; then
    _run systemctl enable --now aivax-valkey.service || true
  else
    echo "  note: aivax-valkey.service not installed yet"
  fi
  if systemctl cat aivax-pipeline.service &>/dev/null; then
    _run systemctl enable --now aivax-pipeline.service || true
  else
    echo "  note: aivax-pipeline.service not installed yet"
  fi
  _run systemctl enable --now aivax-engine-inline.service
fi

echo
if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN complete — no changes written."
  exit 0
fi

echo "[mode.json] product mode → inline"
_aivax_write_mode_json inline

echo "Inline installed."
echo "  unit:   aivax-engine-inline.service"
echo "  sock:   /run/aivax/policy.sock"
echo "  mode:   /etc/aivax/mode.json"
echo "  log:    ${ENGINE_LOG}"
echo "  doc:    ${ENGINE_ROOT}/docs/INSTALLER_INLINE.md"
if [[ "$SKIP_START" != "1" ]]; then
  systemctl is-active aivax-engine-inline.service || true
  ls -l /run/aivax/policy.sock 2>/dev/null || echo "  (sock not yet — check journalctl -u aivax-engine-inline)"
fi
