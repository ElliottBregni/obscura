#!/usr/bin/env bash
# start-a2a.sh - Start Obscura with A2A (Agent-to-Agent) protocol enabled
#
# This script configures and launches the Obscura SDK server with A2A support,
# enabling agent discovery, real-time communication, and cross-agent collaboration.
#
# Usage:
#   ./scripts/start-a2a.sh [OPTIONS]
#
# Options:
#   --name NAME          Agent name (default: "Obscura Agent")
#   --desc DESCRIPTION   Agent description
#   --port PORT          HTTP server port (default: 8080)
#   --grpc-port PORT     gRPC server port (default: 50051, 0=disabled)
#   --redis-url URL      Redis URL for persistent task storage (default: in-memory)
#   --auth/--no-auth     Enable/disable authentication (default: disabled)
#   --help               Show this help message

set -euo pipefail

# Default configuration
OBSCURA_PORT="${OBSCURA_PORT:-8080}"
OBSCURA_HOST="${OBSCURA_HOST:-0.0.0.0}"
OBSCURA_A2A_ENABLED="true"
OBSCURA_A2A_GRPC_PORT="${OBSCURA_A2A_GRPC_PORT:-50051}"
OBSCURA_A2A_AGENT_NAME="${OBSCURA_A2A_AGENT_NAME:-Obscura Agent}"
OBSCURA_A2A_AGENT_DESCRIPTION="${OBSCURA_A2A_AGENT_DESCRIPTION:-Multi-agent SDK with A2A support}"
OBSCURA_A2A_REDIS_URL="${OBSCURA_A2A_REDIS_URL:-}"
OBSCURA_AUTH_ENABLED="${OBSCURA_AUTH_ENABLED:-false}"
OBSCURA_OTEL_ENABLED="${OTEL_ENABLED:-false}"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --name)
      OBSCURA_A2A_AGENT_NAME="$2"
      shift 2
      ;;
    --desc)
      OBSCURA_A2A_AGENT_DESCRIPTION="$2"
      shift 2
      ;;
    --port)
      OBSCURA_PORT="$2"
      shift 2
      ;;
    --grpc-port)
      OBSCURA_A2A_GRPC_PORT="$2"
      shift 2
      ;;
    --redis-url)
      OBSCURA_A2A_REDIS_URL="$2"
      shift 2
      ;;
    --auth)
      OBSCURA_AUTH_ENABLED="true"
      shift
      ;;
    --no-auth)
      OBSCURA_AUTH_ENABLED="false"
      shift
      ;;
    --help)
      head -n 20 "$0" | grep "^#" | sed 's/^# *//'
      exit 0
      ;;
    *)
      echo "Error: Unknown option $1"
      echo "Run with --help for usage information"
      exit 1
      ;;
  esac
done

# Display startup banner
cat <<EOF

╔═══════════════════════════════════════════════════════════════╗
║                   OBSCURA A2A SERVER                          ║
╚═══════════════════════════════════════════════════════════════╝

Configuration:
  Agent Name:    $OBSCURA_A2A_AGENT_NAME
  Description:   $OBSCURA_A2A_AGENT_DESCRIPTION
  HTTP Port:     $OBSCURA_PORT
  gRPC Port:     $OBSCURA_A2A_GRPC_PORT
  Redis:         ${OBSCURA_A2A_REDIS_URL:-in-memory}
  Auth Enabled:  $OBSCURA_AUTH_ENABLED

Endpoints:
  Agent Card:    http://localhost:$OBSCURA_PORT/.well-known/agent.json
  JSON-RPC:      http://localhost:$OBSCURA_PORT/a2a/rpc
  REST API:      http://localhost:$OBSCURA_PORT/a2a/v1/
  SSE Streaming: http://localhost:$OBSCURA_PORT/a2a/v1/tasks/streaming

EOF

# Export environment variables
export OBSCURA_PORT
export OBSCURA_HOST
export OBSCURA_A2A_ENABLED
export OBSCURA_A2A_GRPC_PORT
export OBSCURA_A2A_AGENT_NAME
export OBSCURA_A2A_AGENT_DESCRIPTION
export OBSCURA_A2A_REDIS_URL
export OBSCURA_AUTH_ENABLED
export OTEL_ENABLED="$OBSCURA_OTEL_ENABLED"

# Start the server
echo "Starting Obscura SDK server with A2A enabled..."
echo

# Determine Python command (prefer uv if available)
if command -v uv &> /dev/null; then
    exec uv run uvicorn obscura.server:app --host "$OBSCURA_HOST" --port "$OBSCURA_PORT" --factory
else
    exec python -m uvicorn obscura.server:app --host "$OBSCURA_HOST" --port "$OBSCURA_PORT" --factory
fi
