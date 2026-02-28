#!/usr/bin/env bash
set -euo pipefail

# Run local (pip-installed or repo) obscura server with the same auth defaults
# used by docker-compose for development.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_NAME="${OBSCURA_ENV:-dev}"
ENV_FILE="config/env/${ENV_NAME}.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

export OBSCURA_AUTH_ENABLED="${OBSCURA_AUTH_ENABLED:-true}"
export OBSCURA_AUTH_ISSUER="${OBSCURA_AUTH_ISSUER:-http://localhost:8081}"
export OBSCURA_AUTH_JWKS_URI="${OBSCURA_AUTH_JWKS_URI:-http://localhost:8081/oauth/v2/keys}"
export OBSCURA_AUTH_AUDIENCE="${OBSCURA_AUTH_AUDIENCE:-obscura-sdk}"

# Map docker-internal endpoints to host endpoints for native/local runs.
if [[ "${OBSCURA_AUTH_JWKS_URI}" == *"zitadel:8080"* ]]; then
  export OBSCURA_AUTH_JWKS_URI="${OBSCURA_AUTH_JWKS_URI/zitadel:8080/localhost:8081}"
fi

export OBSCURA_API_KEYS="${OBSCURA_API_KEYS:-obscura-dev-key-123:dev-user:admin,agent:copilot,agent:claude,agent:localllm,agent:openai,agent:moonshot,agent:read,sync:write,sessions:manage}"
export OBSCURA_DEFAULT_BACKEND="${OBSCURA_DEFAULT_BACKEND:-copilot}"
export OBSCURA_AUTH_MODE="${OBSCURA_AUTH_MODE:-oauth_first}"

export OBSCURA_HOST="${OBSCURA_HOST:-0.0.0.0}"
export OBSCURA_PORT="${OBSCURA_PORT:-8080}"

if command -v obscura >/dev/null 2>&1; then
  exec obscura serve --host "$OBSCURA_HOST" --port "$OBSCURA_PORT"
fi

if command -v uv >/dev/null 2>&1; then
  exec uv run python -m uvicorn obscura.server:create_app --factory --host "$OBSCURA_HOST" --port "$OBSCURA_PORT"
fi

echo "Could not find 'obscura' or 'uv' on PATH." >&2
exit 1
