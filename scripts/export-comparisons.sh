#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_PATH="${1:-$ROOT_DIR/evals/comparisons-$(date +%Y%m%d-%H%M%S).jsonl}"

cd "$ROOT_DIR"
mkdir -p "$(dirname "$OUTPUT_PATH")"

docker exec llm-gateway-redis redis-cli --raw LRANGE comparisons 0 -1 > "$OUTPUT_PATH"

echo "Exported comparisons to $OUTPUT_PATH"
