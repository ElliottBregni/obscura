#!/usr/bin/env bash
# Start Obscura with A2A enabled

set -euo pipefail

# Configuration
OBSCURA_PORT="${OBSCURA_PORT:-8080}"
OBSCURA_HOST="${OBSCURA_HOST:-0.0.0.0}"
OBSCURA_A2A_ENABLED="true"
OBSCURA_A2A_AGENT_NAME="${OBSCURA_A2A_AGENT_NAME:-Claude A2A Agent}"
OBSCURA_AUTH_ENABLED="${OBSCURA_AUTH_ENABLED:-false}"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --name) OBSCURA_A2A_AGENT_NAME="$2"; shift 2 ;;
    --port) OBSCURA_PORT="$2"; shift 2 ;;
    --help) 
      echo "Usage: $0 [--name NAME] [--port PORT]"
      exit 0 ;;
    *) shift ;;
  esac
done

# Display config
cat <<BANNER

╔═══════════════════════════════════════════════════════════════╗
║                   OBSCURA A2A SERVER                          ║
╚═══════════════════════════════════════════════════════════════╝

Configuration:
  Agent Name:    $OBSCURA_A2A_AGENT_NAME
  HTTP Port:     $OBSCURA_PORT

Endpoints:
  Agent Card:    http://localhost:$OBSCURA_PORT/.well-known/agent.json
  JSON-RPC:      http://localhost:$OBSCURA_PORT/a2a/rpc
  REST API:      http://localhost:$OBSCURA_PORT/a2a/v1/
  SSE Streaming: http://localhost:$OBSCURA_PORT/a2a/v1/tasks/streaming

BANNER

# Export and start
export OBSCURA_PORT OBSCURA_HOST OBSCURA_A2A_ENABLED
export OBSCURA_A2A_AGENT_NAME OBSCURA_AUTH_ENABLED

cd /Users/elliottbregni/dev/obscura-main
exec python -m uvicorn obscura.server:app --factory --host "$OBSCURA_HOST" --port "$OBSCURA_PORT"
