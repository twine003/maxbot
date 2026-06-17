#!/usr/bin/env bash
# Restart helper (Linux / macOS). The bot can call this to bounce itself.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
CHAT_ID="${1:?Usage: restart.sh <chat_id>}"

sleep 5  # let the current Telegram message flush
echo "$CHAT_ID" > "$HERE/restart_marker"

# Kill the maxbot process for THIS deployment, then relaunch.
DEPLOY="$(basename "$HERE")"
pkill -f "maxbot.*$DEPLOY" 2>/dev/null || true
sleep 2
exec "$HERE/start.sh"
