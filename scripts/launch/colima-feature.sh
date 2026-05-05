#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  Obscura Launch Kit — feature toggle
#  Enable / disable / inspect optional capabilities for the colima VM.
#  Each feature is a sourced module under scripts/launch/features/<name>.sh
#  exposing `feature_enable`, `feature_disable`, `feature_status`.
#
#  Usage:
#    colima-feature.sh enable NAME [extra args]
#    colima-feature.sh disable NAME
#    colima-feature.sh status [NAME]      # all features if omitted
#    colima-feature.sh list               # show available features
# ─────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
FEATURES_DIR="$SCRIPT_DIR/features"
# shellcheck source=_colima_lib.sh
source "$SCRIPT_DIR/_colima_lib.sh"

usage() {
  sed -n '3,/^# ──*$/p' "$0" | sed 's/^# \?//' >&2
  exit 1
}

list_features() {
  if [[ ! -d "$FEATURES_DIR" ]]; then
    echo "(no features directory at $FEATURES_DIR)"
    return
  fi
  local f name desc
  for f in "$FEATURES_DIR"/*.sh; do
    [[ -e "$f" ]] || continue
    name=$(basename "$f" .sh)
    [[ "$name" == _* ]] && continue
    desc=$(awk '/^# DESC:/ { sub(/^# DESC: */, ""); print; exit }' "$f")
    printf "  %-20s %s\n" "$name" "${desc:-(no description)}"
  done
}

load_feature() {
  local name="$1"
  local path="$FEATURES_DIR/${name}.sh"
  if [[ ! -f "$path" ]]; then
    echo "❌ unknown feature: $name" >&2
    echo "Available:" >&2
    list_features >&2
    exit 1
  fi
  unset -f feature_enable feature_disable feature_status 2>/dev/null || true
  # shellcheck disable=SC1090
  source "$path"
  for fn in feature_enable feature_disable feature_status; do
    if ! declare -F "$fn" >/dev/null; then
      echo "❌ feature $name is missing $fn()" >&2
      exit 1
    fi
  done
}

[[ $# -gt 0 ]] || usage

trap '[[ -n "${SSH_CONFIG:-}" ]] && rm -f "$SSH_CONFIG"' EXIT

ACTION="$1"; shift || true

case "$ACTION" in
  -h|--help) usage;;
  list)
    list_features
    ;;
  enable)
    [[ $# -ge 1 ]] || { echo "❌ enable requires NAME" >&2; usage; }
    NAME="$1"; shift
    load_feature "$NAME"
    colima_require_running false
    feature_enable "$@"
    ;;
  disable)
    [[ $# -ge 1 ]] || { echo "❌ disable requires NAME" >&2; usage; }
    NAME="$1"; shift
    load_feature "$NAME"
    # disable should be tolerant of stopped VM (just clears local state),
    # but most disables need ssh — try and let feature handle gracefully.
    colima_require_running false 2>/dev/null || true
    feature_disable "$@"
    ;;
  status)
    if [[ $# -ge 1 ]]; then
      NAME="$1"; shift
      load_feature "$NAME"
      feature_status "$@"
    else
      # All features
      colima_require_running false 2>/dev/null || true
      for f in "$FEATURES_DIR"/*.sh; do
        [[ -e "$f" ]] || continue
        name=$(basename "$f" .sh)
        [[ "$name" == _* ]] && continue
        echo "── $name ──"
        load_feature "$name"
        feature_status || true
      done
    fi
    ;;
  *)
    echo "❌ unknown action: $ACTION" >&2
    usage
    ;;
esac
