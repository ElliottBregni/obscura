#!/usr/bin/env bash
set -euo pipefail

# One-command auth readiness check for the local Docker dev stack.
# - Verifies Zitadel health + OIDC discovery + JWKS key presence
# - Verifies SDK ingress auth diagnostics endpoint
# - Verifies provider egress auth readiness for Copilot/Claude
#
# Usage:
#   ./scripts/dev-auth-bootstrap-check.sh
#   ./scripts/dev-auth-bootstrap-check.sh --start
#   ./scripts/dev-auth-bootstrap-check.sh --fix

START_STACK=false
FIX_MODE=false
ENV_NAME="${OBSCURA_ENV:-dev}"
for arg in "$@"; do
  if [[ "$arg" == "dev" || "$arg" == "staging" || "$arg" == "prod" ]]; then
    ENV_NAME="$arg"
  fi
  if [[ "$arg" == "--start" ]]; then
    START_STACK=true
  fi
  if [[ "$arg" == "--fix" ]]; then
    FIX_MODE=true
    START_STACK=true
  fi
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_env() {
  "$ROOT_DIR/scripts/compose-env.sh" "$ENV_NAME" "$@"
}

if $START_STACK; then
  compose_env up -d cockroachdb zitadel redis otel-collector obscura-sdk web-ui
fi

require_running() {
  local svc="$1"
  local status
  status="$(compose_env ps "$svc" | tail -n +2 || true)"
  if [[ -z "$status" ]]; then
    echo "ERROR: service '$svc' not found"
    return 1
  fi
  if ! echo "$status" | grep -q "Up"; then
    echo "ERROR: service '$svc' is not running"
    return 1
  fi
  return 0
}

for svc in web-ui obscura-sdk zitadel; do
  require_running "$svc"
done

web_exec() {
  compose_env exec -T web-ui sh -lc "$1"
}

API_KEY="${OBSCURA_CHECK_API_KEY:-obscura-dev-key-123}"

collect_state() {
  zitadel_health="$(web_exec "wget -qO- http://zitadel:8080/debug/healthz 2>/dev/null || true")"
  # Zitadel routes are host-routed in this dev setup; use Host: localhost.
  oidc_json="$(web_exec "wget --header='Host: localhost' -qO- http://zitadel:8080/.well-known/openid-configuration 2>/dev/null || true")"
  jwks_json="$(web_exec "wget --header='Host: localhost' -qO- http://zitadel:8080/oauth/v2/keys 2>/dev/null || true")"
  diag_json="$(web_exec "wget --header='X-API-Key: ${API_KEY}' -qO- http://obscura-sdk:8080/api/v1/auth/diagnostics 2>/dev/null || true")"
  providers_json="$(web_exec "wget --header='X-API-Key: ${API_KEY}' -qO- http://obscura-sdk:8080/api/v1/providers/health 2>/dev/null || true")"
}

wait_for_jwks_keys() {
  local attempts=24
  local delay=5
  local raw_json=""
  local count=0
  local i
  for ((i=0; i<attempts; i++)); do
    raw_json="$(web_exec "wget --header='Host: localhost' -qO- http://zitadel:8080/oauth/v2/keys 2>/dev/null || true")"
    count="$(printf "%s" "$raw_json" | python -c 'import json,sys
try:
    j=json.load(sys.stdin)
    print(len(j.get("keys", [])))
except Exception:
    print(0)
')"
    if [[ "${count:-0}" -gt 0 ]]; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

evaluate_state() {
  ZITADEL_HEALTH_OK=false
  OIDC_OK=false
  JWKS_KEYS_COUNT=0
  DIAG_OK=false
  INGRESS_OK=false
  COPILOT_OK=false
  CLAUDE_OK=false
  OVERALL_READY=false

  if [[ "$zitadel_health" == "ok" ]]; then
    ZITADEL_HEALTH_OK=true
  fi

  if echo "$oidc_json" | grep -q '"issuer"'; then
    OIDC_OK=true
  fi

  if [[ -n "$jwks_json" ]]; then
    JWKS_KEYS_COUNT="$(printf "%s" "$jwks_json" | python -c 'import json,sys
try:
    j=json.load(sys.stdin)
    print(len(j.get("keys", [])))
except Exception:
    print(0)
')"
  fi

  if echo "$diag_json" | grep -q '"auth_enabled"'; then
    DIAG_OK=true
  fi

  if $ZITADEL_HEALTH_OK && $OIDC_OK && $DIAG_OK; then
    INGRESS_OK=true
  fi

  if [[ -n "$providers_json" ]]; then
    COPILOT_OK="$(printf "%s" "$providers_json" | python -c 'import json,sys
try:
    j=json.load(sys.stdin)
    v={p.get("backend"): p.get("ok", False) for p in j.get("providers", [])}
    print("true" if bool(v.get("copilot")) else "false")
except Exception:
    print("false")
')"
    CLAUDE_OK="$(printf "%s" "$providers_json" | python -c 'import json,sys
try:
    j=json.load(sys.stdin)
    v={p.get("backend"): p.get("ok", False) for p in j.get("providers", [])}
    print("true" if bool(v.get("claude")) else "false")
except Exception:
    print("false")
')"
  fi

  if $INGRESS_OK && [[ "$JWKS_KEYS_COUNT" -gt 0 ]] && [[ "$COPILOT_OK" == "true" ]] && [[ "$CLAUDE_OK" == "true" ]]; then
    OVERALL_READY=true
  fi
}

print_report() {
  echo "==== Obscura Dev Auth Report ===="
  echo "zitadel_health_ok=${ZITADEL_HEALTH_OK}"
  echo "oidc_discovery_ok=${OIDC_OK}"
  echo "jwks_keys_count=${JWKS_KEYS_COUNT}"
  echo "sdk_auth_diagnostics_ok=${DIAG_OK}"
  echo "ingress_ok=${INGRESS_OK}"
  echo "copilot_egress_ok=${COPILOT_OK}"
  echo "claude_egress_ok=${CLAUDE_OK}"
  echo "overall_ready=${OVERALL_READY}"
  echo "================================="
}

inject_host_tokens() {
  local gh_token=""
  if command -v gh >/dev/null 2>&1; then
    gh_token="$(gh auth token 2>/dev/null || true)"
  fi

  if [[ -n "$gh_token" || -n "${ANTHROPIC_API_KEY:-}" || -n "${CLAUDE_API_KEY:-}" ]]; then
    GH_TOKEN="$gh_token" \
    GITHUB_TOKEN="$gh_token" \
    ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
    CLAUDE_API_KEY="${CLAUDE_API_KEY:-}" \
    compose_env up -d obscura-sdk >/dev/null
  fi
}

reset_identity_stack() {
  compose_env down -v >/dev/null
  compose_env up -d cockroachdb zitadel redis otel-collector obscura-sdk web-ui >/dev/null
  # Allow identity bootstrap to settle.
  wait_for_jwks_keys || true
}

collect_state
evaluate_state

if $FIX_MODE && [[ "$OVERALL_READY" != "true" ]]; then
  # Try safe provider token injection first.
  inject_host_tokens

  collect_state
  evaluate_state

  # If identity ingress is still unhealthy, do one clean reset attempt.
  if [[ "$INGRESS_OK" != "true" || "$JWKS_KEYS_COUNT" -eq 0 ]]; then
    reset_identity_stack
    inject_host_tokens
    collect_state
    evaluate_state
  fi
fi

print_report

if [[ "$OVERALL_READY" != "true" ]]; then
  echo "Suggested fixes:"
  if [[ "$JWKS_KEYS_COUNT" -eq 0 ]]; then
    echo "- Zitadel JWKS has 0 keys. Recreate identity state and allow Zitadel bootstrap to complete."
  fi
  if [[ "$COPILOT_OK" != "true" ]]; then
    echo "- Copilot: docker exec -it obscura-sdk gh auth login -h github.com"
  fi
  if [[ "$CLAUDE_OK" != "true" ]]; then
    echo "- Claude: set ANTHROPIC_API_KEY or CLAUDE_API_KEY in config/env/${ENV_NAME}.env or shell env"
  fi
  exit 1
fi

exit 0
