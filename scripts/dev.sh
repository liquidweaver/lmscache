#!/usr/bin/env bash
# Run LMS Cache locally for development. Data lives under ./.dev so nothing touches the real NAS.
set -euo pipefail
cd "$(dirname "$0")/.."
export LMSCACHE_MODELS="${LMSCACHE_MODELS:-$PWD/.dev/models}"
export LMSCACHE_CONFIG="${LMSCACHE_CONFIG:-$PWD/.dev/config}"
mkdir -p "$LMSCACHE_MODELS/lmstudio" "$LMSCACHE_CONFIG"
[ -d .venv ] || uv venv -q .venv
uv pip install -q -e . --python .venv/bin/python
exec .venv/bin/uvicorn lmscache.app:app --host 0.0.0.0 --port "${PORT:-8080}" --reload --reload-dir lmscache --timeout-graceful-shutdown 2
