#!/bin/bash
# Wind Recheck macOS launcher (Apple Silicon + Intel)
set -euo pipefail

cd "$(dirname "$0")"

pause_on_error() {
  echo
  echo "[ERROR] $1"
  echo "See Mac_Quick_Start.md for troubleshooting."
  echo
  read -r -p "Press Return to close... " _ || true
  exit 1
}

echo "============================================"
echo "  WIND RECHECK - macOS launcher"
echo "============================================"
echo

PYTHON=""
for candidate in .venv/bin/python python3.12 python3.11 python3.13 python3.10 python3.14 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) else 1)' >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  pause_on_error "Python 3.10-3.14 is required. Python 3.12 is recommended for a new environment."
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] Creating Python virtual environment ($PYTHON) ..."
  "$PYTHON" -m venv .venv || pause_on_error "Could not create .venv. Check that Python is installed correctly."
fi

VENV_PY=".venv/bin/python"
REQ_HASH="$(shasum -a 256 requirements.txt | awk '{print $1}')"
OLD_HASH="$(cat .venv/.wind_recheck_deps_stamp 2>/dev/null || true)"
if [ "$REQ_HASH" != "$OLD_HASH" ]; then
  echo "[2/3] Installing dependencies (requires internet on first run) ..."
  "$VENV_PY" -m pip install -r requirements.txt || pause_on_error "Dependency installation failed. Check internet connection and Python version."
  printf '%s\n' "$REQ_HASH" > .venv/.wind_recheck_deps_stamp
else
  echo "[2/3] Dependencies already installed."
fi

if [ ! -f .env ]; then
  cp .env.example .env || pause_on_error "Could not create the .env configuration file."
  echo
  echo "[NOTICE] Created .env from .env.example."
  echo "         Configure AISTREAM_WIND_URL for real inference."
  echo "         The UI works without the API, but model inference does not."
fi

if ! grep -Eq '^AISTREAM_WIND_URL=[^[:space:]]+' .env; then
  echo "[NOTICE] AISTREAM_WIND_URL is not configured in .env."
fi

PORT=""
for candidate in 8000 8001 8002 8003 8004; do
  if ! lsof -nP -iTCP:"$candidate" -sTCP:LISTEN >/dev/null 2>&1; then
    PORT="$candidate"
    break
  fi
done

if [ -z "$PORT" ]; then
  pause_on_error "Ports 8000-8004 are already in use. Stop an older instance and try again."
fi

URL="http://127.0.0.1:${PORT}"
echo

echo "[3/3] Starting local web server at $URL"
echo "       Press Control+C in this Terminal window to stop."
echo

(
  for attempt in $(seq 1 30); do
    if curl -fsS "$URL/api/health" >/dev/null 2>&1; then
      open "$URL" >/dev/null 2>&1 || true
      exit 0
    fi
    sleep 1
  done
) &

exec "$VENV_PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
