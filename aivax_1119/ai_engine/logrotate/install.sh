#!/usr/bin/env bash
# Install ai_engine logrotate + hourly timer. Does not restart the engine.
# Usage: sudo bash logrotate/install.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/../deploy/common/_lib.sh"
_aivax_deploy_init "${HERE}/../deploy/common"
_aivax_install_engine_logrotate
echo "logrotate installed. engine was not restarted."
echo "  conf:  /etc/logrotate.d/aivax-engine + aivax-apps"
echo "  timer: aivax-engine-logrotate.timer (run-hourly.sh)"
echo "  log:   ${ENGINE_LOG}"
