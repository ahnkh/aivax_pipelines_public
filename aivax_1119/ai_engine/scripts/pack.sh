#!/usr/bin/env bash
# 폐쇄망 이송용 tar.gz. 컴파일 없음 — Python 트리 + deploy 스크립트.
#
#   ./scripts/pack.sh              # → dist/ai-engine-<arch>.tar.gz
#   ./scripts/pack.sh --out /tmp
#
# 현장:
#   cd /home1/aivax && tar -xzf ai-engine-x86_64.tar.gz
#   cd /home1/aivax/ai_engine && sudo SKIP_ENV=1 bash deploy/mirror/install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT="${OUT:-${ROOT}/dist}"
ARCH="$(uname -m)"

usage() {
    cat <<EOF
Usage: scripts/pack.sh [--out DIR]

환경:
  OUT=DIR     --out 과 동일 (기본: ${ROOT}/dist)

생성물 최상단: ai_engine/
풀기: cd /home1/aivax && tar -xzf …
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --out)
            OUT="${2:?}"
            shift 2
            ;;
        *)
            echo "알 수 없는 인자: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

ver="$(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || true)"
ver="${ver:-$(date +%Y%m%d)}"
ver="${ver//\//-}"

mkdir -p "$OUT"
stage="$(mktemp -d "${TMPDIR:-/tmp}/aivax-ai-engine-pack.XXXXXX")"
trap 'rm -rf "$stage"' EXIT

mkdir -p "${stage}/ai_engine"
echo "=== pack ai_engine (${ver}, ${ARCH}) ==="
tar -C "$ROOT" -cf - \
    --exclude='.git' \
    --exclude='dist' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='*.log' \
    --exclude='*.pcap' \
    --exclude='.idea' \
    --exclude='.vscode' \
    . | tar -C "${stage}/ai_engine" -xf -

(
    cd "$stage"
    find ai_engine -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >ai_engine/SHA256SUMS
)

out="${OUT}/ai-engine-${ARCH}.tar.gz"
tar -czf "$out" -C "$stage" ai_engine
echo "생성: $out ($(du -h "$out" | awk '{print $1}'))"
echo "현장: cd /home1/aivax && tar -xzf $(basename "$out")"
echo "      cd /home1/aivax/ai_engine && sudo SKIP_ENV=1 bash deploy/mirror/install.sh"
