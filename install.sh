#!/usr/bin/env bash
# One-command installer for maxbot (Linux / macOS).
# Usage: ./install.sh [--name my-bot]
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"
exec "$PY" "$HERE/install.py" "$@"
