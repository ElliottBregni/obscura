#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  Obscura Launch Kit — Colima port tunnel manager
#  Bridge ports between the host and the colima VM via SSH tunnels.
#  Forwards run as background processes; PIDs tracked under
#  ~/.obscura/launch/pids/colima-{fwd,rev}-<port>.pid so they persist
#  beyond this script and can be torn down later.
#
#  Direction:
#    --forward (default):    host:LEFT → VM:RIGHT     (SSH -L)
#                            reach VM services from your Mac (Qdrant, API)
#    --reverse:              VM:LEFT   → host:RIGHT   (SSH -R)
#                            let VM-side obscura reach Mac services
#    --bidirectional / -b:   open both -L and -R for the port pair
#                            (no native bidirectional tunnel exists in SSH;
#                             this is shorthand for two simultaneous tunnels)
#
#  Usage:
#    colima-forward.sh [LEFT:]RIGHT [...]                # add forwards (-L)
#    colima-forward.sh --reverse [LEFT:]RIGHT [...]      # add reverses (-R)
#    colima-forward.sh --bidirectional [LEFT:]RIGHT [...]# both
#    colima-forward.sh --stop SPEC [...]                 # stop forwards
#    colima-forward.sh --stop --reverse SPEC [...]       # stop reverses
#    colima-forward.sh --stop --bidirectional SPEC [...] # stop both
#    colima-forward.sh --stop-all                        # stop all tunnels
#    colima-forward.sh --list                            # show all tunnels
#
#  SPEC formats:
#    PORT             same port on both sides (e.g. 6333)
#    LEFT:RIGHT       map LEFT (the listening port) → RIGHT (target)
#                     (e.g. 8000:80)
#
#  Env overrides:
#    COLIMA_PROFILE  (default: openshell)
# ─────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
# shellcheck source=_colima_lib.sh
source "$SCRIPT_DIR/_colima_lib.sh"

usage() {
  sed -n '3,/^# ──*$/p' "$0" | sed 's/^# \?//' >&2
  exit 1
}

[[ $# -eq 0 ]] && usage

trap '[[ -n "${SSH_CONFIG:-}" ]] && rm -f "$SSH_CONFIG"' EXIT

MODE="forward"   # forward | reverse | bidirectional
STOP=false
LIST=false
STOP_ALL=false
SPECS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)             usage;;
    --list)                LIST=true; shift;;
    --reverse|-r)          MODE="reverse"; shift;;
    --bidirectional|-b)    MODE="bidirectional"; shift;;
    --forward)             MODE="forward"; shift;;
    --stop)                STOP=true; shift;;
    --stop-all)            STOP_ALL=true; shift;;
    --)                    shift; SPECS+=("$@"); break;;
    -*)                    echo "❌ unknown flag: $1" >&2; usage;;
    *)                     SPECS+=("$1"); shift;;
  esac
done

if $LIST; then
  list_tunnels
  exit 0
fi

if $STOP_ALL; then
  pid_dir="$LAUNCH_DIR/pids"
  if [[ -d "$pid_dir" ]]; then
    for pid_file in "$pid_dir"/colima-fwd-*.pid; do
      [[ -e "$pid_file" ]] || continue
      port=$(basename "$pid_file"); port="${port#colima-fwd-}"; port="${port%.pid}"
      _stop_tunnel L "$port"
    done
    for pid_file in "$pid_dir"/colima-rev-*.pid; do
      [[ -e "$pid_file" ]] || continue
      port=$(basename "$pid_file"); port="${port#colima-rev-}"; port="${port%.pid}"
      _stop_tunnel R "$port"
    done
  fi
  exit 0
fi

if $STOP; then
  [[ ${#SPECS[@]} -gt 0 ]] || usage
  for spec in "${SPECS[@]}"; do
    case "$MODE" in
      reverse)        stop_reverse "$spec";;
      bidirectional)  stop_bidirectional "$spec";;
      *)              stop_forward "$spec";;
    esac
  done
  exit 0
fi

# Add new tunnels.
[[ ${#SPECS[@]} -gt 0 ]] || usage
colima_require_running false
for spec in "${SPECS[@]}"; do
  case "$MODE" in
    reverse)        reverse_port "$spec";;
    bidirectional)  bidirectional_port "$spec";;
    *)              forward_port "$spec";;
  esac
done
