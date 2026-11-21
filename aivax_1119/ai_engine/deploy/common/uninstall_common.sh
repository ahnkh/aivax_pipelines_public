#!/usr/bin/env bash
# Remove ai_engine.env only. Does not remove pipeline.env or pipeline/valkey units.
# Usage: sudo bash deploy/common/uninstall_common.sh
set -euo pipefail
ETC_AIVAX=/etc/aivax

rm -f "$ETC_AIVAX/ai_engine.env"
# legacy shared name (pre env-split)
rm -f "$ETC_AIVAX/aivax-1g-cpu.env"
rmdir "$ETC_AIVAX" 2>/dev/null || true

echo "Removed $ETC_AIVAX/ai_engine.env (pipeline.env left intact if present)."
