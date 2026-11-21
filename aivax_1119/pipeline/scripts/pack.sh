#!/usr/bin/env bash
# 폐쇄망 이송용 Pipeline tar.gz. Valkey는 packaging에서 별도 제공한다.
#
#   ./scripts/pack.sh              # → dist/pipeline-<arch>.tar.gz
#   ./scripts/pack.sh --out /tmp
#
# 현장:
#   cd /home1/aivax && tar -xzf pipeline-x86_64.tar.gz
#   서비스 등록·기동은 AIVAX packaging 설치기가 수행한다.
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

생성물 최상단: pipeline/
풀기: cd /home1/aivax && tar -xzf …
Valkey 서버는 AIVAX packaging이 별도로 설치한다.
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
stage="$(mktemp -d "${TMPDIR:-/tmp}/aivax-pipeline-pack.XXXXXX")"
trap 'rm -rf "$stage"' EXIT

mkdir -p "${stage}/pipeline"
echo "=== pack pipeline (${ver}, ${ARCH}) ==="
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
    --exclude='.TEMP' \
    --exclude='trace-log' \
    --exclude='.idea' \
    --exclude='.vscode' \
    . | tar -C "${stage}/pipeline" -xf -

(
    cd "$stage"
    find pipeline -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >pipeline/SHA256SUMS
)

out="${OUT}/pipeline-${ARCH}.tar.gz"
if command -v pigz >/dev/null 2>&1; then
    tar -cf - -C "$stage" pipeline | pigz > "$out"
else
    # Keep standalone packaging usable on machines without pigz.
    tar -czf "$out" -C "$stage" pipeline
fi
echo "생성: $out ($(du -h "$out" | awk '{print $1}'))"
echo "현장: cd /home1/aivax && tar -xzf $(basename "$out")"
echo "서비스 등록·기동: AIVAX packaging 설치기 사용"
