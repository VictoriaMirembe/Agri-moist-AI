#!/usr/bin/env sh
# One-step setup and start for macOS and Linux: ./run.sh
# Creates .venv, installs the requirements, copies .env.example to .env if there is
# no .env yet, then starts the app. Safe to run again: later runs skip what is done.
set -e
cd "$(dirname "$0")"

PORT="${APP_PORT:-8000}"

# Find Python 3.12 or newer.
PYTHON=""
for candidate in python3.13 python3.12 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "Python 3.12 or newer is required. Install it from https://www.python.org/downloads/ and run ./run.sh again."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "==> Creating the virtual environment (.venv) with $("$PYTHON" --version)"
  "$PYTHON" -m venv .venv
fi

# Reinstall only when requirements.txt has changed since the last install.
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "==> Installing requirements (first run takes a minute or two)"
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  touch .venv/.installed
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env from .env.example (the app runs without any keys; add them to .env later)"
fi

echo ""
echo "==> AgriMoist AI is starting on http://localhost:$PORT"
echo "    Officer screens: http://localhost:$PORT    Farmer page: http://localhost:$PORT/farmer"
echo "    Press Ctrl+C to stop."
echo ""
exec .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port "$PORT"
