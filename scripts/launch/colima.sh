#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  Obscura Launch Kit — Colima bootstrap
#  Starts the colima profile (default: "openshell"), syncs this repo,
#  runs `uv sync`, starts Qdrant, optionally syncs extra dirs and
#  opens host→VM port forwards, then launches obscura inside the VM.
#
#  Usage:
#    scripts/launch/colima.sh [--sync DIR[:REMOTE]] ...
#                             [--forward [LEFT:]RIGHT] ...
#                             [--reverse [LEFT:]RIGHT] ...
#                             [--bidirectional [LEFT:]RIGHT] ...
#                             [--feature NAME] ...
#                             [-- obscura args]
#
#    --sync           repeatable; sync extra host dirs into the VM after boot
#    --forward        repeatable; host:LEFT → VM:RIGHT  (reach VM from Mac)
#    --reverse        repeatable; VM:LEFT   → host:RIGHT (let VM reach Mac)
#    --bidirectional  repeatable; opens both directions for the same port
#    --feature        repeatable; enable a feature module post-boot
#                     (browser-headless, browser-bridge, ...)
#                     run `colima-feature.sh list` to see all features
#    --               everything after is forwarded to the obscura CLI
#
#  Env overrides:
#    COLIMA_PROFILE  (default: openshell)
#    OBSCURA_SRC     (default: repo root inferred from script)
# ─────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." &>/dev/null && pwd)"
# shellcheck source=_colima_lib.sh
source "$SCRIPT_DIR/_colima_lib.sh"

OBSCURA_SRC="${OBSCURA_SRC:-$REPO_ROOT}"
REMOTE_SRC="~/obscura"

# Where the VM bootstrap pulls obscura from. Override any of these inline:
#   OBSCURA_GIT_REF=main scripts/launch/colima.sh ...
OBSCURA_GIT_REPO="${OBSCURA_GIT_REPO:-https://github.com/ElliottBregni/obscura.git}"
OBSCURA_GIT_REF="${OBSCURA_GIT_REF:-main}"
# Old `OBSCURA_PIPX_EXTRAS` name is honored as a fallback for muscle-memory.
OBSCURA_EXTRAS="${OBSCURA_EXTRAS:-${OBSCURA_PIPX_EXTRAS:-full,plugins-all}}"

# PEP 508 direct URL: `name[extras] @ git+URL@ref`.
# Package distribution name is `obscura-agent` (the binary stays `obscura`).
if [[ -n "$OBSCURA_EXTRAS" ]]; then
  TOOL_SPEC="obscura-agent[${OBSCURA_EXTRAS}] @ git+${OBSCURA_GIT_REPO}@${OBSCURA_GIT_REF}"
else
  TOOL_SPEC="obscura-agent @ git+${OBSCURA_GIT_REPO}@${OBSCURA_GIT_REF}"
fi

EXTRA_SYNCS=()
PORT_FORWARDS=()
PORT_REVERSES=()
PORT_BIDIR=()
FEATURES=()
OBSCURA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sync)
      [[ $# -ge 2 ]] || { echo "❌ --sync requires an argument" >&2; exit 1; }
      EXTRA_SYNCS+=("$2"); shift 2;;
    --forward)
      [[ $# -ge 2 ]] || { echo "❌ --forward requires an argument" >&2; exit 1; }
      PORT_FORWARDS+=("$2"); shift 2;;
    --reverse)
      [[ $# -ge 2 ]] || { echo "❌ --reverse requires an argument" >&2; exit 1; }
      PORT_REVERSES+=("$2"); shift 2;;
    --bidirectional)
      [[ $# -ge 2 ]] || { echo "❌ --bidirectional requires an argument" >&2; exit 1; }
      PORT_BIDIR+=("$2"); shift 2;;
    --feature)
      [[ $# -ge 2 ]] || { echo "❌ --feature requires a NAME" >&2; exit 1; }
      FEATURES+=("$2"); shift 2;;
    --)
      shift; OBSCURA_ARGS=("$@"); break;;
    -h|--help)
      sed -n '2,/^# ──*$/p' "$0" | sed 's/^# \?//'
      exit 0;;
    *)
      OBSCURA_ARGS+=("$1"); shift;;
  esac
done

mkdir -p "$LAUNCH_DIR/logs" "$LAUNCH_DIR/pids"

trap '[[ -n "${SSH_CONFIG:-}" ]] && rm -f "$SSH_CONFIG"' EXIT

colima_require_running true

# Note: the obscura source is NOT synced from the host. The `uv tool` install
# below pulls from the configured git ref (default: main on GitHub) so that
# the VM is always running a pushed commit, not whatever's in the local
# working tree. Use `--sync DIR` for any other host dirs you want available
# in the VM.

# Bootstrap: install obscura via `uv tool install`, pulling from the
# configured git ref. uv handles Python install + tool isolation in one
# binary — no pipx, no PEP 668 dance. `--force` re-fetches the ref on
# every boot; uv's own wheel cache keeps subsequent boots fast.
ssh -F "$SSH_CONFIG" "$SSH_HOST" bash -s -- $TOOL_SPEC <<'BOOTSTRAP'
  set -e
  # Rejoin all positional args — TOOL_SPEC contains spaces ("pkg @ url")
  # and ssh re-parses the command string on the remote, splitting at spaces.
  TOOL_SPEC="$*"
  export PATH="$HOME/.local/bin:$PATH"

  # uv → Python 3.13.
  if ! command -v uv &>/dev/null; then
    echo "⟳ Installing uv..."
    sudo apt-get update -qq && sudo apt-get install -y -qq curl git ca-certificates
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
  uv python install 3.13 -q
  PY313=$(uv python find 3.13)

  echo "⟳ Installing obscura via uv tool from $TOOL_SPEC ..."
  uv tool install --python "$PY313" --force "$TOOL_SPEC"

  # Make sure the uv tool bin dir is on PATH for future shells too.
  uv tool update-shell >/dev/null 2>&1 || true
BOOTSTRAP
echo "✓ obscura installed."

# Reverse-forward host:6333 → VM:6333 so VM-side obscura's
# OBSCURA_QDRANT_URL=http://localhost:6333 reaches the Qdrant running on
# your Mac. Override with OBSCURA_QDRANT_PORT=<port> to forward a different
# port; set OBSCURA_QDRANT_PORT=0 to skip if you have your own setup.
OBSCURA_QDRANT_PORT="${OBSCURA_QDRANT_PORT:-6333}"
if [[ "$OBSCURA_QDRANT_PORT" != "0" ]]; then
  echo "⟳ Bridging host Qdrant into VM (VM:${OBSCURA_QDRANT_PORT} → host:${OBSCURA_QDRANT_PORT})..."
  reverse_port "$OBSCURA_QDRANT_PORT"
fi

# Auto-sync host-side CLI-agent config dirs into the VM. Each entry is a
# directory name relative to $HOME. If it exists on the host (resolved
# through symlinks), it's pushed to the same path inside the VM so the
# VM-side CLI sees identical config/state.
# Override with OBSCURA_CONFIG_DIRS=".foo .bar" — empty disables the loop.
OBSCURA_CONFIG_DIRS="${OBSCURA_CONFIG_DIRS-.obscura .codex .claude .copilot}"
if [[ -n "$OBSCURA_CONFIG_DIRS" ]]; then
  for d in $OBSCURA_CONFIG_DIRS; do
    src="$HOME/$d"
    if [[ -d "$src" ]]; then
      sync_dir "${src}:~/${d}"
    fi
  done
fi

# Auto-sync the host's obscura-auth session into the VM so the REPL doesn't
# bail with "Run obscura-auth login first". Set OBSCURA_AUTH_SYNC=0 to skip
# (e.g. running an unauthenticated test).
OBSCURA_AUTH_SYNC="${OBSCURA_AUTH_SYNC:-1}"
if [[ "$OBSCURA_AUTH_SYNC" == "1" ]]; then
  if ! "$SCRIPT_DIR/colima-feature.sh" enable host-auth; then
    echo "⚠️  auth sync failed; obscura will likely refuse to start." >&2
    echo "   Run \`obscura-auth login\` on your Mac, then re-run colima.sh." >&2
  fi
fi

# Sync any extra dirs requested via --sync.
for spec in "${EXTRA_SYNCS[@]:-}"; do
  [[ -n "$spec" ]] || continue
  sync_dir "$spec"
done

# Open any host→VM port forwards requested via --forward.
for spec in "${PORT_FORWARDS[@]:-}"; do
  [[ -n "$spec" ]] || continue
  forward_port "$spec"
done

# Open any VM→host reverse forwards requested via --reverse.
for spec in "${PORT_REVERSES[@]:-}"; do
  [[ -n "$spec" ]] || continue
  reverse_port "$spec"
done

# Open any bidirectional pairs requested via --bidirectional.
for spec in "${PORT_BIDIR[@]:-}"; do
  [[ -n "$spec" ]] || continue
  bidirectional_port "$spec"
done

# Enable any features requested via --feature.
for name in "${FEATURES[@]:-}"; do
  [[ -n "$name" ]] || continue
  echo
  "$SCRIPT_DIR/colima-feature.sh" enable "$name" || \
    echo "⚠️  feature '$name' enable failed; continuing"
done

echo "→ Launching obscura..."
GH_TOKEN=$(gh auth token 2>/dev/null || true)
REMOTE_HOME=$(ssh -F "$SSH_CONFIG" "$SSH_HOST" 'echo $HOME')
ssh -t -F "$SSH_CONFIG" "$SSH_HOST" \
  "GH_TOKEN=${GH_TOKEN} OBSCURA_QDRANT_URL=http://localhost:${OBSCURA_QDRANT_PORT:-6333} OBSCURA_VECTOR_BACKEND=qdrant ${REMOTE_HOME}/.local/bin/obscura ${OBSCURA_ARGS[*]:-}"
