#!/usr/bin/env bash
# Hourly maxsize check for all /etc/logrotate.d/aivax-* configs.
set -euo pipefail
shopt -s nullglob
cfgs=()
for c in /etc/logrotate.d/aivax-engine /etc/logrotate.d/aivax-mirror /etc/logrotate.d/aivax-apps; do
  [[ -f "$c" ]] && cfgs+=("$c")
done
[[ ${#cfgs[@]} -eq 0 ]] && exit 0
exec /usr/sbin/logrotate "${cfgs[@]}"
