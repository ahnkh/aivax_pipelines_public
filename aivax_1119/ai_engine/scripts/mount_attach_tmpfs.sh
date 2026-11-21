#!/bin/bash
# Mount attachment tmpfs for No-Persistent-Disk path contract.
# Requires root. Prefer systemd-tmpfiles / mount unit in production.
set -euo pipefail

if [[ -f /etc/aivax/aivax.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /etc/aivax/aivax.env
  set +a
fi

MOUNT_POINT="${AIVAX_ATTACH_TMPFS:-/run/aivax/attachments}"
SIZE="${AIVAX_ATTACH_TMPFS_SIZE:-8G}"
INODES="${AIVAX_ATTACH_TMPFS_INODES:-200000}"
MODE="${AIVAX_ATTACH_TMPFS_MODE:-0700}"

mkdir -p /run/aivax "$MOUNT_POINT"

OPTS="size=${SIZE},nr_inodes=${INODES},mode=${MODE},nodev,nosuid,noexec"

want_bytes() {
  local s="${1^^}"
  local n="${s%[BKMGT]}"
  n="${n%[B]}"
  case "$s" in
    *T) echo $((n * 1024 * 1024 * 1024 * 1024)) ;;
    *G) echo $((n * 1024 * 1024 * 1024)) ;;
    *M) echo $((n * 1024 * 1024)) ;;
    *K) echo $((n * 1024)) ;;
    *) echo "$n" ;;
  esac
}

remount_or_ok() {
  local cur want
  cur="$(findmnt -bno SIZE "$MOUNT_POINT" 2>/dev/null || echo 0)"
  want="$(want_bytes "$SIZE")"
  if [[ "${cur:-0}" -ge "${want:-0}" && "${cur:-0}" -gt 0 ]]; then
    echo "[tmpfs] already mounted: $MOUNT_POINT"
    findmnt -no OPTIONS,SIZE "$MOUNT_POINT" || true
    return 0
  fi
  echo "[tmpfs] remount size ${SIZE} (was ${cur} bytes) at $MOUNT_POINT"
  if mount -o "remount,${OPTS},noswap" "$MOUNT_POINT" 2>/dev/null \
    || mount -o "remount,${OPTS}" "$MOUNT_POINT"; then
    df -h "$MOUNT_POINT"
    return 0
  fi
  echo "[tmpfs] ERROR: remount failed" >&2
  return 1
}

if mountpoint -q "$MOUNT_POINT"; then
  remount_or_ok
  chmod "$MODE" "$MOUNT_POINT" || true
  exit 0
fi

if mount -t tmpfs -o "${OPTS},noswap" tmpfs "$MOUNT_POINT" 2>/dev/null; then
  echo "[tmpfs] mounted with noswap at $MOUNT_POINT ($OPTS)"
elif mount -t tmpfs -o "${OPTS}" tmpfs "$MOUNT_POINT"; then
  echo "[tmpfs] WARN: noswap unsupported; mounted without noswap"
  echo "[tmpfs] No-Persistent-Disk requires system swap disabled or noswap backport"
else
  echo "[tmpfs] ERROR: failed to mount $MOUNT_POINT" >&2
  exit 1
fi

chmod "$MODE" "$MOUNT_POINT" || true
df -h "$MOUNT_POINT"
