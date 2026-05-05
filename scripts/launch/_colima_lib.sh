# Shared helpers for scripts/launch/colima*.sh
# Source this; do not execute directly.
#
# Exports:
#   COLIMA_PROFILE   (default: openshell)
#   SSH_HOST         colima-${COLIMA_PROFILE}
#   LAUNCH_DIR       ~/.obscura/launch
#   SSH_CONFIG       (set by colima_require_running; caller cleans up)
# Functions:
#   colima_require_running [start_if_stopped=true|false]
#   sync_dir SPEC                  # SPEC = LOCAL  or  LOCAL:REMOTE
#   forward_port SPEC              # -L: host:LEFT → VM:RIGHT
#   reverse_port SPEC              # -R: VM:LEFT   → host:RIGHT
#   bidirectional_port SPEC        # opens both -L LEFT:RIGHT and -R RIGHT:LEFT
#   stop_forward SPEC | stop_reverse SPEC | stop_bidirectional SPEC
#   list_tunnels                   # show both directions

COLIMA_PROFILE="${COLIMA_PROFILE:-openshell}"
LAUNCH_DIR="${LAUNCH_DIR:-$HOME/.obscura/launch}"
SSH_HOST="colima-${COLIMA_PROFILE}"

_colima_init_ssh_config() {
  if [[ -z "${SSH_CONFIG:-}" ]]; then
    SSH_CONFIG=$(mktemp)
    colima ssh-config --profile "$COLIMA_PROFILE" > "$SSH_CONFIG"
  fi
}

colima_require_running() {
  local start_if_stopped="${1:-true}"
  if ! command -v colima &>/dev/null; then
    echo "❌ colima not found. Install: brew install colima" >&2
    exit 1
  fi
  local status
  status=$(colima status "$COLIMA_PROFILE" 2>&1 || true)
  if echo "$status" | grep -qi "is not running"; then
    if [[ "$start_if_stopped" == "true" ]]; then
      echo "⟳ Starting colima '$COLIMA_PROFILE'..."
      colima start "$COLIMA_PROFILE"
      echo "✓ colima '$COLIMA_PROFILE' started."
    else
      echo "❌ colima '$COLIMA_PROFILE' is not running. Run scripts/launch/colima.sh first." >&2
      exit 1
    fi
  else
    echo "✓ colima '$COLIMA_PROFILE' is running."
  fi
  _colima_init_ssh_config
}

# Split SPEC into LOCAL and REMOTE parts; defaults REMOTE = ~/$(basename LOCAL).
_split_sync_spec() {
  local spec="$1"
  if [[ "$spec" == *:* ]]; then
    SYNC_LOCAL="${spec%%:*}"
    SYNC_REMOTE="${spec#*:}"
  else
    SYNC_LOCAL="$spec"
    SYNC_REMOTE="~/$(basename "$spec")"
  fi
  SYNC_LOCAL="${SYNC_LOCAL/#\~/$HOME}"
}

sync_dir() {
  local SYNC_LOCAL SYNC_REMOTE
  _split_sync_spec "$1"
  if [[ ! -d "$SYNC_LOCAL" ]]; then
    echo "⚠️  skipping '$1': $SYNC_LOCAL is not a directory" >&2
    return 1
  fi
  echo "⟳ Syncing $SYNC_LOCAL → $SSH_HOST:$SYNC_REMOTE"
  ssh -F "$SSH_CONFIG" "$SSH_HOST" "mkdir -p $SYNC_REMOTE"
  # .git is included by default so the synced dir is a usable repo in the VM.
  # Excluded: regenerable build artifacts and platform-specific virtualenvs.
  # Override with OBSCURA_SYNC_EXCLUDES (space-separated tar --exclude patterns).
  local default_excludes=(
    '__pycache__'
    '*.pyc'
    '.venv'
    'node_modules'
    '*.egg-info'
    'build'
    'dist'
    '.DS_Store'
  )
  local exclude_args=()
  if [[ -n "${OBSCURA_SYNC_EXCLUDES:-}" ]]; then
    # shellcheck disable=SC2206
    local custom_excludes=( $OBSCURA_SYNC_EXCLUDES )
    for pat in "${custom_excludes[@]}"; do
      exclude_args+=(--exclude="$pat")
    done
  else
    for pat in "${default_excludes[@]}"; do
      exclude_args+=(--exclude="$pat")
    done
  fi
  # -h dereferences symlinks during archiving so that e.g. ~/.obscura → /repo/.obscura
  # syncs the *contents* of /repo/.obscura, not a dangling symlink.
  COPYFILE_DISABLE=1 tar -C "$SYNC_LOCAL" -h -czf - "${exclude_args[@]}" . \
    | ssh -F "$SSH_CONFIG" "$SSH_HOST" \
      "tar -C $SYNC_REMOTE -xzf - 2>/dev/null && find $SYNC_REMOTE -name '._*' -delete"
  echo "✓ synced $SYNC_LOCAL"
}

# Split SPEC into LEFT and RIGHT ports; defaults LEFT = RIGHT.
# For --forward: LEFT = host port, RIGHT = VM port  (-L LEFT:localhost:RIGHT)
# For --reverse: LEFT = VM port,   RIGHT = host port (-R LEFT:localhost:RIGHT)
_split_port_spec() {
  local spec="$1"
  if [[ "$spec" == *:* ]]; then
    PORT_LEFT="${spec%%:*}"
    PORT_RIGHT="${spec#*:}"
  else
    PORT_LEFT="$spec"
    PORT_RIGHT="$spec"
  fi
}

# Internal: open an SSH tunnel.
#   $1 = direction: "L" (host→VM, -L) or "R" (VM→host, -R)
#   $2 = LEFT port (the port being created/listened on)
#   $3 = RIGHT port (where traffic ends up)
_open_tunnel() {
  local dir="$1" left="$2" right="$3"
  local ssh_flag pid_prefix label_left label_right
  case "$dir" in
    L) ssh_flag="-L"; pid_prefix="colima-fwd"; label_left="host"; label_right="VM";;
    R) ssh_flag="-R"; pid_prefix="colima-rev"; label_left="VM";   label_right="host";;
    *) echo "❌ bad tunnel direction: $dir" >&2; return 1;;
  esac
  mkdir -p "$LAUNCH_DIR/pids" "$LAUNCH_DIR/logs"
  local pid_file="$LAUNCH_DIR/pids/${pid_prefix}-${left}.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "✓ ${label_left}:${left} → ${label_right}:${right} already open (pid $(cat "$pid_file"))"
    return 0
  fi
  rm -f "$pid_file"
  # Disable ControlMaster: colima's ssh-config enables it, which would cause
  # a tunnel ssh to attach to the existing master, register the forward, and
  # exit immediately (multiplexed). The forward would then die when the master
  # persistence expires. Forcing a dedicated connection keeps the tunnel up
  # for the lifetime of this ssh process.
  ssh -F "$SSH_CONFIG" -nNT \
    -o ControlMaster=no -o ControlPath=none \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    "$ssh_flag" "${left}:localhost:${right}" "$SSH_HOST" \
    >>"$LAUNCH_DIR/logs/colima-tunnels.log" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" > "$pid_file"
  sleep 1.5
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    echo "❌ failed: ${label_left}:${left} → ${label_right}:${right} (see $LAUNCH_DIR/logs/colima-tunnels.log)" >&2
    return 1
  fi
  echo "🔗 ${label_left}:${left} → ${label_right}:${right} (pid $pid)"
}

forward_port() {
  local PORT_LEFT PORT_RIGHT
  _split_port_spec "$1"
  _open_tunnel L "$PORT_LEFT" "$PORT_RIGHT"
}

reverse_port() {
  local PORT_LEFT PORT_RIGHT
  _split_port_spec "$1"
  _open_tunnel R "$PORT_LEFT" "$PORT_RIGHT"
}

# Internal: stop a tunnel by direction + LEFT port.
_stop_tunnel() {
  local dir="$1" left="$2" pid_prefix label
  case "$dir" in
    L) pid_prefix="colima-fwd"; label="host";;
    R) pid_prefix="colima-rev"; label="VM";;
    *) echo "❌ bad tunnel direction: $dir" >&2; return 1;;
  esac
  local pid_file="$LAUNCH_DIR/pids/${pid_prefix}-${left}.pid"
  if [[ ! -f "$pid_file" ]]; then
    echo "✓ no active tunnel on ${label}:${left}"
    return 0
  fi
  local pid
  pid=$(cat "$pid_file")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    echo "✓ stopped ${label}:${left} (pid $pid)"
  fi
  rm -f "$pid_file"
}

stop_forward() {
  local PORT_LEFT PORT_RIGHT
  _split_port_spec "$1"
  _stop_tunnel L "$PORT_LEFT"
}

stop_reverse() {
  local PORT_LEFT PORT_RIGHT
  _split_port_spec "$1"
  _stop_tunnel R "$PORT_LEFT"
}

# Open both directions for the same logical port. Useful when the same service
# could be initiated from either side, or when you want symmetrical reachability.
# SPEC = LEFT:RIGHT  →  -L LEFT:localhost:RIGHT  AND  -R RIGHT:localhost:LEFT
# SPEC = PORT        →  -L PORT:localhost:PORT   AND  -R PORT:localhost:PORT
bidirectional_port() {
  local PORT_LEFT PORT_RIGHT
  _split_port_spec "$1"
  _open_tunnel L "$PORT_LEFT" "$PORT_RIGHT"
  _open_tunnel R "$PORT_RIGHT" "$PORT_LEFT"
}

stop_bidirectional() {
  local PORT_LEFT PORT_RIGHT
  _split_port_spec "$1"
  _stop_tunnel L "$PORT_LEFT"
  _stop_tunnel R "$PORT_RIGHT"
}

# Reverse-forward a Unix socket (SSH StreamLocalForward).
# $1 = host (Mac) socket path
# $2 = VM socket path
# $3 = pidfile name (under $LAUNCH_DIR/pids/)
forward_unix_socket_reverse() {
  local host_sock="$1" vm_sock="$2" pid_name="$3"
  if [[ ! -S "$host_sock" ]]; then
    echo "❌ host socket missing: $host_sock" >&2
    return 1
  fi
  mkdir -p "$LAUNCH_DIR/pids" "$LAUNCH_DIR/logs"
  local pid_file="$LAUNCH_DIR/pids/${pid_name}.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "✓ socket reverse already open: $vm_sock → $host_sock (pid $(cat "$pid_file"))"
    return 0
  fi
  rm -f "$pid_file"
  # Make sure the VM-side socket path's parent dir exists, and stale socket cleared.
  ssh -F "$SSH_CONFIG" "$SSH_HOST" \
    "mkdir -p \"\$(dirname '$vm_sock')\" && rm -f '$vm_sock'"
  ssh -F "$SSH_CONFIG" -nNT \
    -o ControlMaster=no -o ControlPath=none \
    -o ExitOnForwardFailure=yes \
    -o StreamLocalBindUnlink=yes \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -R "${vm_sock}:${host_sock}" "$SSH_HOST" \
    >>"$LAUNCH_DIR/logs/colima-tunnels.log" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" > "$pid_file"
  sleep 1.5
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    echo "❌ socket reverse failed (see $LAUNCH_DIR/logs/colima-tunnels.log)" >&2
    return 1
  fi
  echo "🔗 socket: VM:$vm_sock → host:$host_sock (pid $pid)"
}

list_tunnels() {
  local pid_dir="$LAUNCH_DIR/pids"
  if [[ ! -d "$pid_dir" ]]; then
    echo "(no active tunnels)"
    return 0
  fi
  local found=0 pid_file pid base port label arrow
  for pid_file in "$pid_dir"/colima-fwd-*.pid "$pid_dir"/colima-rev-*.pid; do
    [[ -e "$pid_file" ]] || continue
    pid=$(cat "$pid_file")
    base=$(basename "$pid_file")
    if [[ "$base" == colima-fwd-* ]]; then
      label="host"; arrow="→ VM"
      port="${base#colima-fwd-}"
    else
      label="VM"; arrow="→ host"
      port="${base#colima-rev-}"
    fi
    port="${port%.pid}"
    if kill -0 "$pid" 2>/dev/null; then
      echo "🔗 ${label}:${port} ${arrow} (pid $pid)"
      found=1
    else
      rm -f "$pid_file"
    fi
  done
  [[ $found -eq 1 ]] || echo "(no active tunnels)"
}
