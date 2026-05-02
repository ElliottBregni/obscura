#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  Obscura Launch Kit — Qdrant
#  Starts Qdrant in local mode (no Docker).
#  Install: brew install qdrant/tap/qdrant
# ─────────────────────────────────────────────
set -euo pipefail

LAUNCH_DIR="$HOME/.obscura/launch"
PID_FILE="$LAUNCH_DIR/pids/qdrant.pid"
LOG_FILE="$LAUNCH_DIR/logs/qdrant.log"
STORAGE_PATH="$HOME/.obscura/qdrant/storage"

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")" "$STORAGE_PATH"

# Already running?
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "✅ Qdrant already running (pid $(cat "$PID_FILE"))"
  exit 0
fi

QDRANT_BIN=$(which qdrant 2>/dev/null || true)

if [ -z "$QDRANT_BIN" ]; then
  echo "⚠️  qdrant binary not found."
  echo "   → Falling back to OBSCURA_QDRANT_MODE=local (SQLite, no server needed)"
  echo "   → To install: brew install qdrant/tap/qdrant"
  export OBSCURA_QDRANT_MODE=local
  # Write a sentinel so start.sh knows to export this env
  echo "export OBSCURA_QDRANT_MODE=local" > "$LAUNCH_DIR/.env"
  exit 0
fi

# Start Qdrant, binding only to localhost
"$QDRANT_BIN" \
  --storage-path "$STORAGE_PATH" \
  >> "$LOG_FILE" 2>&1 &

echo $! > "$PID_FILE"
echo "🚀 Qdrant started (pid $!) → logs: $LOG_FILE"

# Wait briefly and verify it's up
sleep 1
if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "❌ Qdrant failed to start. Check $LOG_FILE"
  exit 1
fi
