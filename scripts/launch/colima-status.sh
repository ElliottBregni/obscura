#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  Obscura Launch Kit — consolidated status view
#  Shows: colima state, active tunnels, feature health.
# ─────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
# shellcheck source=_colima_lib.sh
source "$SCRIPT_DIR/_colima_lib.sh"

trap '[[ -n "${SSH_CONFIG:-}" ]] && rm -f "$SSH_CONFIG"' EXIT

echo "━━━ Colima ━━━"
colima status "$COLIMA_PROFILE" 2>&1 || true

echo
echo "━━━ Tunnels ━━━"
list_tunnels

echo
echo "━━━ Features ━━━"
"$SCRIPT_DIR/colima-feature.sh" status 2>/dev/null || echo "(unable to read feature status)"
