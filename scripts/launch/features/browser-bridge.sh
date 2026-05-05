# DESC: Reverse-bridge the Mac's obscura browser-extension socket into the VM
# Sourced by colima-feature.sh.
#
# How it works:
#   1. Reads ~/.obscura/browser/active.json on the Mac to find the running
#      host's socket path (created by the obscura process powering the Chrome
#      extension's native messaging host).
#   2. SSH StreamLocalForward (-R) reverse-forwards that Unix socket into the
#      VM at /tmp/obscura-browser/<user>/host-bridge.sock.
#   3. Starts a long-lived heartbeat process in the VM and writes
#      ~/.obscura/browser/active.json there pointing to the bridged socket
#      with the heartbeat PID. obscura's `_alive(pid)` check (os.kill, 0) then
#      passes inside the VM and `attach_if_running` succeeds.
#
# Caveat: bridge stays valid only while the host obscura process keeps the
# original socket alive. If you restart obscura on the Mac, re-enable.

BRIDGE_PID_NAME="browser-bridge-tunnel"
HEARTBEAT_PID_NAME="browser-bridge-heartbeat"
VM_SOCKET_NAME="host-bridge.sock"

_read_host_bridge() {
  # Emits: socket_path<TAB>browser<TAB>profile_id  for the first live host.
  python3 - <<'PY'
import json, os, sys
from pathlib import Path
home = Path(os.environ.get("OBSCURA_HOME") or (Path.home() / ".obscura"))
reg = home / "browser" / "active.json"
try:
    data = json.loads(reg.read_text())
except FileNotFoundError:
    sys.exit(2)
except Exception as e:
    print(f"corrupt registry: {e}", file=sys.stderr)
    sys.exit(3)
hosts = data.get("hosts") or []
for h in hosts:
    pid = int(h.get("pid", -1))
    sock = h.get("socket")
    if pid > 0 and sock and Path(sock).is_socket():
        try:
            os.kill(pid, 0)
            print(f"{sock}\t{h.get('browser','')}\t{h.get('profile_id','')}")
            sys.exit(0)
        except ProcessLookupError:
            continue
sys.exit(4)
PY
}

feature_enable() {
  echo "⟳ Enabling browser-bridge..."

  local out rc
  out=$(_read_host_bridge) || rc=$?
  if [[ -n "${rc:-}" ]]; then
    case "$rc" in
      2) echo "❌ no Mac-side bridge registry (~/.obscura/browser/active.json missing)" >&2;;
      3) echo "❌ Mac-side registry is corrupt" >&2;;
      4) echo "❌ no live host process found in Mac-side registry" >&2;;
      *) echo "❌ failed to read Mac-side registry (rc=$rc)" >&2;;
    esac
    echo "   Start obscura on your Mac with the Chrome extension first." >&2
    return 1
  fi
  local host_socket host_browser host_profile
  IFS=$'\t' read -r host_socket host_browser host_profile <<<"$out"
  echo "✓ found Mac-side bridge: $host_socket (browser=$host_browser, profile=$host_profile)"

  local vm_user vm_socket
  vm_user=$(ssh -F "$SSH_CONFIG" "$SSH_HOST" 'echo $USER')
  vm_socket="/tmp/obscura-browser/${vm_user}/${VM_SOCKET_NAME}"

  # 1. Reverse-forward the unix socket.
  forward_unix_socket_reverse "$host_socket" "$vm_socket" "$BRIDGE_PID_NAME"

  # 2. Start (or reuse) heartbeat in VM, write VM-side active.json.
  ssh -F "$SSH_CONFIG" "$SSH_HOST" bash <<EOF
    set -e
    sock_dir=\$(dirname '$vm_socket')
    mkdir -p "\$sock_dir"
    chmod 0700 "\$sock_dir"

    pid_dir=\$HOME/.obscura/launch/pids
    mkdir -p "\$pid_dir"
    pid_file="\$pid_dir/${HEARTBEAT_PID_NAME}.pid"
    if [[ -f "\$pid_file" ]] && kill -0 "\$(cat "\$pid_file")" 2>/dev/null; then
      heartbeat_pid=\$(cat "\$pid_file")
    else
      nohup sleep infinity >/dev/null 2>&1 &
      heartbeat_pid=\$!
      disown 2>/dev/null || true
      echo "\$heartbeat_pid" > "\$pid_file"
    fi

    mkdir -p \$HOME/.obscura/browser
    cat > \$HOME/.obscura/browser/active.json <<JSON
{
  "hosts": [
    {
      "pid": \$heartbeat_pid,
      "browser": "$host_browser",
      "profile_id": "$host_profile",
      "socket": "$vm_socket",
      "_bridged_from": "$host_socket",
      "_note": "synthesized by scripts/launch/features/browser-bridge.sh"
    }
  ]
}
JSON
    echo "✓ VM-side active.json written (heartbeat pid \$heartbeat_pid)"
EOF

  echo
  echo "🌉 VM-side obscura should now find the host's browser bridge."
  echo "   Re-launch obscura inside the VM (or restart the REPL) for attach_if_running to pick it up."
}

feature_disable() {
  echo "⟳ Disabling browser-bridge..."
  local pid_file="$LAUNCH_DIR/pids/${BRIDGE_PID_NAME}.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    kill "$(cat "$pid_file")" 2>/dev/null || true
    echo "✓ stopped reverse-forward (pid $(cat "$pid_file"))"
  fi
  rm -f "$pid_file"
  ssh -F "$SSH_CONFIG" "$SSH_HOST" bash <<EOF || true
    pid_file=\$HOME/.obscura/launch/pids/${HEARTBEAT_PID_NAME}.pid
    if [[ -f "\$pid_file" ]]; then
      pid=\$(cat "\$pid_file")
      kill "\$pid" 2>/dev/null || true
      rm -f "\$pid_file"
      echo "✓ heartbeat stopped (pid \$pid)"
    fi
    rm -f \$HOME/.obscura/browser/active.json
    echo "✓ VM-side active.json cleared"
EOF
}

feature_status() {
  local pid_file="$LAUNCH_DIR/pids/${BRIDGE_PID_NAME}.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "  socket reverse-forward: pid $(cat "$pid_file") — RUNNING"
  else
    echo "  socket reverse-forward: not running"
  fi
  if [[ -n "${SSH_CONFIG:-}" ]]; then
    ssh -F "$SSH_CONFIG" "$SSH_HOST" bash <<EOF 2>/dev/null || echo "  (VM unreachable)"
      pid_file=\$HOME/.obscura/launch/pids/${HEARTBEAT_PID_NAME}.pid
      if [[ -f "\$pid_file" ]] && kill -0 "\$(cat "\$pid_file")" 2>/dev/null; then
        echo "  VM heartbeat: pid \$(cat "\$pid_file") — RUNNING"
      else
        echo "  VM heartbeat: not running"
      fi
      if [[ -f \$HOME/.obscura/browser/active.json ]]; then
        echo "  VM active.json: present"
      else
        echo "  VM active.json: missing"
      fi
EOF
  fi
}
