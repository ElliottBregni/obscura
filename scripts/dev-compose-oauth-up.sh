#!/usr/bin/env bash
set -euo pipefail

# Start compose with OAuth-derived env vars from the host shell.
# This is intended for local development only.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if command -v gh >/dev/null 2>&1; then
  if GH_HOST_TOKEN="$(gh auth token 2>/dev/null)"; then
    export GH_TOKEN="${GH_TOKEN:-$GH_HOST_TOKEN}"
    export GITHUB_TOKEN="${GITHUB_TOKEN:-$GH_HOST_TOKEN}"
  fi
fi

# Claude CLI does not provide a stable token-print command across versions.
# Prefer existing API-key env vars when present.
if [[ -z "${ANTHROPIC_API_KEY:-}" && -n "${CLAUDE_API_KEY:-}" ]]; then
  export ANTHROPIC_API_KEY="$CLAUDE_API_KEY"
fi

exec ./scripts/compose-env.sh dev up -d --build "$@"
