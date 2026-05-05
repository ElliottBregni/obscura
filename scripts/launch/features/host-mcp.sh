# DESC: Reverse-bridge a Mac-side MCP server (TCP or Unix socket) into the VM
# Sourced by colima-feature.sh.
#
# Usage:
#   colima-feature.sh enable host-mcp tcp NAME PORT [VM_PORT]
#       Bridge host:PORT → VM:PORT (or VM:VM_PORT) so VM-side obscura's
#       MCP client can connect to localhost:PORT in the VM.
#
#   colima-feature.sh enable host-mcp socket NAME HOST_SOCK [VM_SOCK]
#       Bridge a Unix-socket MCP server. Defaults VM_SOCK to
#       /tmp/obscura-mcp/<name>.sock.
#
#   colima-feature.sh disable host-mcp NAME
#   colima-feature.sh status  host-mcp [NAME]
#
# State for active bridges is recorded under
#   ~/.obscura/launch/state/host-mcp/<NAME>.conf
# so disable/status can find them later.
#
# Notes:
#   • Stdio MCP servers are NOT directly bridgeable — wrap them in a TCP
#     listener first (e.g. `socat TCP-LISTEN:PORT,fork EXEC:'mcp-server'`)
#     then bridge that PORT.
#   • obscura's MCP client config (~/.obscura/mcp/core.json) needs an entry
#     pointing at localhost:PORT (TCP) or the bridged socket path. This
#     feature does not edit core.json — see the README in scripts/launch/.

STATE_DIR="$HOME/.obscura/launch/state/host-mcp"

_save_state() {
  local name="$1" content="$2"
  mkdir -p "$STATE_DIR"
  printf "%s\n" "$content" > "$STATE_DIR/${name}.conf"
}

_load_state() {
  local name="$1"
  [[ -f "$STATE_DIR/${name}.conf" ]] && cat "$STATE_DIR/${name}.conf"
}

_clear_state() {
  rm -f "$STATE_DIR/${1}.conf"
}

feature_enable() {
  local kind="${1:-}" name="${2:-}"
  case "$kind" in
    tcp)
      [[ $# -ge 3 ]] || { echo "❌ usage: enable host-mcp tcp NAME PORT [VM_PORT]" >&2; return 1; }
      local host_port="$3" vm_port="${4:-$3}"
      reverse_port "${vm_port}:${host_port}"
      _save_state "$name" "kind=tcp host_port=$host_port vm_port=$vm_port"
      echo "→ MCP server reachable inside VM at: localhost:${vm_port}"
      ;;
    socket)
      [[ $# -ge 3 ]] || { echo "❌ usage: enable host-mcp socket NAME HOST_SOCK [VM_SOCK]" >&2; return 1; }
      local host_sock="$3"
      local vm_sock="${4:-/tmp/obscura-mcp/${name}.sock}"
      ssh -F "$SSH_CONFIG" "$SSH_HOST" "mkdir -p \"\$(dirname '$vm_sock')\""
      forward_unix_socket_reverse "$host_sock" "$vm_sock" "host-mcp-${name}"
      _save_state "$name" "kind=socket host_sock=$host_sock vm_sock=$vm_sock"
      echo "→ MCP server reachable inside VM at: $vm_sock"
      ;;
    *)
      echo "❌ kind must be 'tcp' or 'socket'" >&2
      return 1
      ;;
  esac
}

feature_disable() {
  local name="${1:-}"
  if [[ -z "$name" ]]; then
    echo "❌ usage: disable host-mcp NAME" >&2
    return 1
  fi
  local state
  state=$(_load_state "$name") || true
  if [[ -z "$state" ]]; then
    echo "✓ no recorded host-mcp bridge named '$name'"
    return 0
  fi
  # shellcheck disable=SC2086
  eval "$state"
  case "${kind:-}" in
    tcp)
      stop_reverse "${vm_port}:${host_port}"
      ;;
    socket)
      local pid_file="$LAUNCH_DIR/pids/host-mcp-${name}.pid"
      if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        kill "$(cat "$pid_file")" 2>/dev/null || true
      fi
      rm -f "$pid_file"
      ssh -F "$SSH_CONFIG" "$SSH_HOST" "rm -f '$vm_sock'" 2>/dev/null || true
      ;;
  esac
  _clear_state "$name"
  echo "✓ host-mcp '$name' disabled"
}

feature_status() {
  local name="${1:-}"
  if [[ -n "$name" ]]; then
    local state
    state=$(_load_state "$name") || true
    if [[ -z "$state" ]]; then
      echo "  $name: (none)"
    else
      echo "  $name: $state"
    fi
    return 0
  fi
  if [[ ! -d "$STATE_DIR" ]] || ! ls "$STATE_DIR"/*.conf >/dev/null 2>&1; then
    echo "  (no host-mcp bridges configured)"
    return 0
  fi
  local f n
  for f in "$STATE_DIR"/*.conf; do
    n=$(basename "$f" .conf)
    echo "  $n: $(cat "$f")"
  done
}
