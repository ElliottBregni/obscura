#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  Obscura Launch Kit — end-to-end "up"
#  One command to bring host + VM stack online. Each step is idempotent;
#  re-running is safe and fast when things are already up.
#
#    1. Host Chrome with CDP at localhost:9222         (host-chrome-cdp.sh)
#    2. Host Qdrant if installed and not running        (best-effort)
#    3. Colima VM (default: openshell)                  (colima.sh)
#    4. obscura-agent install via `uv tool` in VM       (colima.sh)
#    5. Reverse-forwards: host CDP → VM, host Qdrant → VM
#    6. Auto-synced config dirs (.obscura .codex .claude .copilot)
#    7. Auth bridged from host Keychain to VM
#    8. browser-bridge feature enabled                  (extension socket)
#    9. host-chrome-cdp feature enabled                 (env var in VM)
#   10. Drops into obscura REPL inside the VM
#
#  Args after `--` go to the obscura CLI (single-shot prompt, etc).
#
#  Usage:
#    scripts/launch/up.sh                       # interactive REPL
#    scripts/launch/up.sh -- "explain this"     # single-shot prompt
#    scripts/launch/up.sh --no-chrome           # skip host CDP step
#    scripts/launch/up.sh --no-vm               # host CDP only, no VM
#
#  Env overrides — anything colima.sh accepts works here too:
#    OBSCURA_GIT_REF, OBSCURA_EXTRAS, OBSCURA_QDRANT_PORT, COLIMA_PROFILE,
#    OBSCURA_CONFIG_DIRS, OBSCURA_AUTH_SYNC, OBSCURA_CDP_PORT
# ─────────────────────────────────────────────
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"

DO_CHROME=true
DO_VM=true
DO_QDRANT_HOST=true
COLIMA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-chrome)  DO_CHROME=false; shift;;
    --no-vm)      DO_VM=false; shift;;
    --no-qdrant)  DO_QDRANT_HOST=false; shift;;
    -h|--help)
      sed -n '3,/^# ──*$/p' "$0" | sed 's/^# \?//'
      exit 0;;
    --)           shift; COLIMA_ARGS+=("--"); COLIMA_ARGS+=("$@"); break;;
    *)            COLIMA_ARGS+=("$1"); shift;;
  esac
done

if $DO_CHROME; then
  echo "━━━ host Chrome (CDP) ━━━"
  "$SCRIPT_DIR/host-chrome-cdp.sh"
  echo
fi

if $DO_QDRANT_HOST; then
  echo "━━━ host Qdrant ━━━"
  if curl -sf -m 1 "http://localhost:${OBSCURA_QDRANT_PORT:-6333}/readyz" >/dev/null 2>&1 \
     || curl -sf -m 1 "http://localhost:${OBSCURA_QDRANT_PORT:-6333}/" >/dev/null 2>&1; then
    echo "✓ Qdrant already responding on localhost:${OBSCURA_QDRANT_PORT:-6333}"
  elif [[ -x "$SCRIPT_DIR/qdrant.sh" ]]; then
    "$SCRIPT_DIR/qdrant.sh" || true
  else
    echo "⚠️  no host Qdrant running and qdrant.sh launcher missing — VM bridge will fail."
    echo "    Install with: brew install qdrant/tap/qdrant, or set --no-qdrant."
  fi
  echo
fi

if $DO_VM; then
  echo "━━━ colima VM + obscura ━━━"
  exec "$SCRIPT_DIR/colima.sh" \
    --feature browser-bridge \
    --feature host-chrome-cdp \
    ${COLIMA_ARGS[@]+"${COLIMA_ARGS[@]}"}
fi

echo "✓ host services up. (Skipped VM because --no-vm was passed.)"
echo "  To launch obscura on host instead, run \`obscura\` in another shell."
