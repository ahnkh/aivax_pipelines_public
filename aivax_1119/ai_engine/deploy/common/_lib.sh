#!/usr/bin/env bash
# Shared helpers for deploy/*/install.sh
# Usage from caller: source .../_lib.sh; _aivax_deploy_init "$HERE"
#
# Python policy:
#   - Do NOT create an ai_engine-private venv.
#   - Prefer the host's existing shared/default env (site aivax-venv, or PATH python3).
#   - Fall back to /usr/bin/python3 only if nothing else is available.
#
# Env ownership:
#   /etc/aivax/ai_engine.env  ← this tree only
#   /etc/aivax/pipeline.env   ← pipeline/deploy only (do not touch here)
#   /etc/aivax/mode.json      ← product mode for management UI (inline|mirror)
#                                written by deploy/*/install.sh; see MODE_INLINE_MIRROR.md

_aivax_resolve_python() {
  local c
  for c in \
    /home1/aivax/aivax-venv/bin/python3 \
    ${VIRTUAL_ENV:+"${VIRTUAL_ENV}/bin/python3"} \
    "$(command -v python3 2>/dev/null || true)" \
    /usr/bin/python3
  do
    [[ -n "$c" && -x "$c" ]] || continue
    if [[ -e "$c" ]]; then
      echo "$c"
      return 0
    fi
  done
  echo "/usr/bin/python3"
}

_aivax_deploy_init() {
  local caller_dir="${1:?caller dir}"
  DEPLOY_DIR="$(cd "${caller_dir}/.." && pwd)"
  if [[ "$(basename "$DEPLOY_DIR")" != "deploy" ]]; then
    DEPLOY_DIR="$(cd "${caller_dir}" && pwd)"
  fi
  ENGINE_ROOT="$(cd "${DEPLOY_DIR}/.." && pwd)"
  COMMON_DIR="${DEPLOY_DIR}/common"
  SYSTEMD=/etc/systemd/system
  ETC_AIVAX=/etc/aivax
  AIVAX_PYTHON="$(_aivax_resolve_python)"
  # Site root is /home1/aivax — default logs under ai_engine tree.
  ENGINE_LOG="${AIVAX_ENGINE_LOG:-/home1/aivax/ai_engine/ai_engine.log}"
  ENGINE_SHM_LOG="${AIVAX_ENGINE_SHM_LOG:-/home1/aivax/ai_engine/ai_engine_shm.log}"
  mkdir -p "$(dirname "$ENGINE_LOG")" "$(dirname "$ENGINE_SHM_LOG")" 2>/dev/null || true
}

_aivax_render() {
  sed \
    -e "s|@AIVAX_ENGINE_ROOT@|${ENGINE_ROOT}|g" \
    -e "s|@AIVAX_PYTHON@|${AIVAX_PYTHON}|g" \
    -e "s|@AIVAX_ENGINE_LOG@|${ENGINE_LOG}|g" \
    -e "s|@AIVAX_ENGINE_SHM_LOG@|${ENGINE_SHM_LOG}|g"
}

# Upsert KEY=value in dest file (safe for simple values: digits, commas, URLs).
_aivax_env_upsert() {
  local dest="$1" key="$2" val="$3"
  [[ -n "$key" && -n "$val" ]] || return 0
  if grep -q "^${key}=" "$dest" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$dest"
  else
    echo "${key}=${val}" >>"$dest"
  fi
}

# Overlay SHM / policy knobs from site /etc/aivax/aivax.env onto ai_engine.env.
# Suricata already reads rings/slots from aivax.env; consumer needs the same
# workers/CPU/timeout in ai_engine.env. Missing keys → leave template defaults.
_aivax_overlay_engine_env_from_site() {
  local site="${AIVAX_ENV:-${ETC_AIVAX}/aivax.env}"
  local dest="${ETC_AIVAX}/ai_engine.env"
  if [[ ! -f "$site" ]]; then
    echo "Site overlay: no $site — keep ai_engine.env as-is"
    return 0
  fi
  if [[ ! -f "$dest" ]]; then
    echo "Site overlay: no $dest yet — skip"
    return 0
  fi

  set -a
  # shellcheck disable=SC1090
  source "$site"
  set +a

  local -a keys=(
    AIVAX_SHM_WORKERS
    AIVAX_SHM_CPU_LIST
    AIVAX_SHM_MAX_INFLIGHT
    AIVAX_SHM_NAME
    AIVAX_SHM_META
    AIVAX_POLICY_API
    AIVAX_POLICY_SERVER_URL
    AIVAX_POLICY_API_TIMEOUT_MS
    AIVAX_CPU_AI_ENGINE
  )
  local k v n=0
  echo "Site overlay from $site → $dest"
  for k in "${keys[@]}"; do
    v="${!k:-}"
    [[ -n "$v" ]] || continue
    _aivax_env_upsert "$dest" "$k" "$v"
    echo "  $k=$v"
    n=$((n + 1))
  done
  echo "  overlay keys applied: $n"
}

# Install ai_engine-owned env only. Never writes pipeline.env or pipeline drop-ins.
# After template install, overlays matching keys from /etc/aivax/aivax.env when present.
_aivax_install_engine_env() {
  mkdir -p "$ETC_AIVAX"

  install -m 0644 "$COMMON_DIR/ai_engine.env" "$ETC_AIVAX/ai_engine.env"
  if grep -q '^AIVAX_ENGINE_ROOT=' "$ETC_AIVAX/ai_engine.env"; then
    sed -i "s|^AIVAX_ENGINE_ROOT=.*|AIVAX_ENGINE_ROOT=${ENGINE_ROOT}|" "$ETC_AIVAX/ai_engine.env"
  else
    echo "AIVAX_ENGINE_ROOT=${ENGINE_ROOT}" >>"$ETC_AIVAX/ai_engine.env"
  fi
  sed -i '/^AIVAX_VENV_PYTHON=/d' "$ETC_AIVAX/ai_engine.env"

  _aivax_overlay_engine_env_from_site

  echo "Installed: $ETC_AIVAX/ai_engine.env"
  echo "Python (shared/default, no private venv): $AIVAX_PYTHON"
}

# Back-compat alias
_aivax_install_common() {
  _aivax_install_engine_env
}

# Stop the other mode, reconcile lifecycle WAL, clear leftover attachment files.
# Safe for sequential Inline ↔ Mirror switches on one host (shared tmpfs).
# Knobs:
#   SKIP_MODE_SWITCH=1     — no-op
#   SKIP_ATTACH_CLEANUP=1  — stop opposite units only (no reconcile/wipe)
_aivax_reconcile_attachments() {
  local py="${AIVAX_PYTHON:-python3}"
  local root="${ENGINE_ROOT:?}"
  local script="${root}/core/attachment_lifecycle.py"
  [[ -f "$script" ]] || return 0
  export PYTHONPATH="${root}:${root}/core:${root}/mirror${PYTHONPATH:+:${PYTHONPATH}}"
  if ! "$py" -u "$script" --reconcile-only; then
    echo "WARN: attachment_lifecycle --reconcile-only failed (continuing cleanup)" >&2
  fi
}

_aivax_wipe_attach_tmpfs() {
  local dir="${AIVAX_ATTACH_TMPFS:-/run/aivax/attachments}"
  if [[ ! -d "$dir" ]]; then
    echo "  attach tmpfs: $dir (missing — skip wipe)"
    return 0
  fi
  # Remove contents only; keep mount point / mount options.
  find "$dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
  local left
  left="$(find "$dir" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')"
  echo "  attach tmpfs wiped: $dir (remaining entries=${left:-0})"
}

# $1 = target mode: inline | mirror
_aivax_prepare_mode_switch() {
  local target="${1:?inline|mirror}"
  if [[ "${SKIP_MODE_SWITCH:-0}" == "1" ]]; then
    echo "[mode-switch] SKIP_MODE_SWITCH=1"
    return 0
  fi

  echo "[mode-switch] prepare → ${target}"
  case "$target" in
    mirror)
      # Inline / legacy UDS engine off before Mirror lifecycle owns attachments.
      for u in \
        aivax-engine-inline.service \
        aivax-engine.service \
        aivax-policy.service \
        aivax-ai-engine.service
      do
        if systemctl cat "$u" &>/dev/null; then
          echo "  stop $u"
          systemctl stop "$u" 2>/dev/null || true
        fi
      done
      ;;
    inline)
      # Mirror consumer + lifecycle/metrics off before Inline uses same tmpfs.
      for u in \
        aivax-engine-mirror.service \
        aivax-engine-shm.service \
        aivax-engine-lifecycle.service \
        aivax-engine-metrics.service \
        aivax-shm-consumer.service \
        aivax-lifecycle.service \
        aivax-metrics.service
      do
        if systemctl cat "$u" &>/dev/null; then
          echo "  stop $u"
          systemctl stop "$u" 2>/dev/null || true
        fi
      done
      ;;
    *)
      echo "ERROR: _aivax_prepare_mode_switch: unknown target '$target'" >&2
      return 1
      ;;
  esac

  if [[ "${SKIP_ATTACH_CLEANUP:-0}" == "1" ]]; then
    echo "  SKIP_ATTACH_CLEANUP=1 — units stopped, no reconcile/wipe"
    return 0
  fi

  echo "  reconcile attachment lifecycle"
  _aivax_reconcile_attachments
  echo "  wipe leftover attachment files"
  _aivax_wipe_attach_tmpfs
}

# Write /etc/aivax/mode.json for management / Outer (atomic tmp → rename).
# $1 = inline | mirror | unknown
# Knob: SKIP_MODE_JSON=1
_aivax_write_mode_json() {
  local mode="${1:?inline|mirror|unknown}"
  case "$mode" in
    inline|mirror|unknown) ;;
    *)
      echo "ERROR: _aivax_write_mode_json: mode must be inline|mirror|unknown (got '$mode')" >&2
      return 1
      ;;
  esac
  if [[ "${SKIP_MODE_JSON:-0}" == "1" ]]; then
    echo "[mode.json] SKIP_MODE_JSON=1"
    return 0
  fi

  local etc="${ETC_AIVAX:-/etc/aivax}"
  mkdir -p "$etc"
  local dest="${etc}/mode.json"
  local tmp
  tmp="$(mktemp "${etc}/.mode.json.XXXXXX")"
  local ts src
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
  case "$mode" in
    inline) src="ai_engine/deploy/inline/install.sh" ;;
    mirror) src="ai_engine/deploy/mirror/install.sh" ;;
    unknown) src="ai_engine/deploy/*/uninstall.sh" ;;
  esac

  cat >"$tmp" <<EOF
{
  "schema_version": 1,
  "mode": "${mode}",
  "updated_at": "${ts}",
  "source": "${src}"
}
EOF
  chmod 0644 "$tmp"
  mv -f "$tmp" "$dest"
  echo "Installed: $dest (mode=${mode})"
}
