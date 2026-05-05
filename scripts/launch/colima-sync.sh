#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  Obscura Launch Kit — Colima sync
#  Sync one or more host directories into the running colima VM.
#  Run any time after scripts/launch/colima.sh has created the VM.
#
#  Usage: scripts/launch/colima-sync.sh DIR[:REMOTE_PATH] [...]
#    DIR              local path; remote defaults to ~/$(basename DIR)
#    DIR:REMOTE_PATH  explicit remote path (e.g. ~/work/foo)
#
#  Env overrides:
#    COLIMA_PROFILE  (default: openshell)
# ─────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
# shellcheck source=_colima_lib.sh
source "$SCRIPT_DIR/_colima_lib.sh"

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 DIR[:REMOTE_PATH] [DIR[:REMOTE_PATH] ...]" >&2
  exit 1
fi

trap '[[ -n "${SSH_CONFIG:-}" ]] && rm -f "$SSH_CONFIG"' EXIT

colima_require_running false

for spec in "$@"; do
  sync_dir "$spec"
done
