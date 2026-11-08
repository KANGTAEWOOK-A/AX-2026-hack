#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/python" -m uvicorn app.main:app \
  --host "${WR_HOST:-0.0.0.0}" --port "${WR_PORT:-8501}" \
  --limit-concurrency 16 --timeout-keep-alive 5
