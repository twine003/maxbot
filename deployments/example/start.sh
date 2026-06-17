#!/usr/bin/env bash
# Launch maxbot for this deployment (Linux / macOS).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"

# Prefer the repo-root .venv created by install.py; fall back to python3 on PATH.
if [ -x "$HERE/../../.venv/bin/python" ]; then
    PY="$HERE/../../.venv/bin/python"
else
    PY="${PYTHON:-python3}"
fi

exec "$PY" -m maxbot --config "$HERE/bot.toml"
