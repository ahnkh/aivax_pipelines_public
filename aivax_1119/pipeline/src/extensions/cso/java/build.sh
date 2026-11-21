#!/usr/bin/env bash
# Compile only the Pipeline-owned worker; never rebuild supplied third_party files.
set -euo pipefail
WORKER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$(cd "$WORKER_DIR/../../../.." && pwd)"
javac --release 17 -encoding UTF-8 \
    -sourcepath "$WORKER_DIR" \
    -cp "$PIPELINE_DIR/third_party/fasoo/cso" \
    -d "$WORKER_DIR" \
    "$WORKER_DIR/CsoJvmWorker.java"
