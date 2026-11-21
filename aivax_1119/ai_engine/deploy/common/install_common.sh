#!/usr/bin/env bash
# Install ai_engine.env only (/etc/aivax/ai_engine.env).
# Does NOT touch pipeline.env or pipeline/valkey drop-ins.
# Usage: sudo bash deploy/common/install_common.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_aivax_deploy_init "$HERE"
_aivax_install_engine_env
echo "Next: sudo bash deploy/inline/install.sh   OR   sudo bash deploy/mirror/install.sh"
