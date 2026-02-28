#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_NAME="${OBSCURA_ENV:-dev}"
if [[ $# -gt 0 ]]; then
  case "$1" in
    dev|staging|prod)
      ENV_NAME="$1"
      shift
      ;;
  esac
fi

ENV_FILE="config/env/${ENV_NAME}.env"
OVERLAY_FILE="docker-compose.${ENV_NAME}.yml"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Unknown environment '$ENV_NAME' (missing $ENV_FILE)" >&2
  exit 1
fi

if [[ ! -f "$OVERLAY_FILE" ]]; then
  echo "Unknown environment '$ENV_NAME' (missing $OVERLAY_FILE)" >&2
  exit 1
fi

# Force environment file values to win over host shell exports. This keeps
# api-keys/auth settings deterministic across local shells and CI.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Host OAuth passthrough for dev shells:
# - GH CLI often stores oauth token in keychain (not hosts.yml), so inject env.
if [[ "$ENV_NAME" == "dev" && -z "${GH_TOKEN:-}" && -z "${GITHUB_TOKEN:-}" ]]; then
  if command -v gh >/dev/null 2>&1; then
    GH_HOST_TOKEN="$(gh auth token 2>/dev/null || true)"
    if [[ -n "$GH_HOST_TOKEN" ]]; then
      export GH_TOKEN="$GH_HOST_TOKEN"
      export GITHUB_TOKEN="$GH_HOST_TOKEN"
    fi
  fi
fi

exec docker compose \
  --env-file "$ENV_FILE" \
  -f docker-compose.base.yml \
  -f "$OVERLAY_FILE" \
  "$@"
