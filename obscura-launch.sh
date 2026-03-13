#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  Obscura Local Launch Kit — master startup script
#  Run this before starting Claude Code / obscura CLI.
#
#  Usage:
#    ./obscura-launch.sh          # start all services
#    ./obscura-launch.sh stop     # stop all services
#    ./obscura-launch.sh status   # check service status
#
#  Add to ~/.zshrc for auto-start:
#    [ -f ~/dev/obscura/obscura-launch.sh ] && \
#      bash ~/dev/obscura/obscura-launch.sh quiet
# ═══════════════════════════════════════════════════════════
set -euo pipefail

OBSCURA_HOME="${OBSCURA_HOME:-$HOME/.obscura}"
LAUNCH_DIR="$OBSCURA_HOME/launch"
PID_DIR="$LAUNCH_DIR/pids"
LOG_DIR="$LAUNCH_DIR/logs"
ENV_FILE="$LAUNCH_DIR/.env"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { echo "  $*"; }
ok()   { echo "✅ $*"; }
warn() { echo "⚠️  $*"; }
fail() { echo "❌ $*"; }
hdr()  { echo; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; echo "  🔭 $*"; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }

is_running() {
  local pidfile="$PID_DIR/$1.pid"
  [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

write_pid() { echo "$1" > "$PID_DIR/$2.pid"; }

stop_service() {
  local name="$1" pidfile="$PID_DIR/$1.pid"
  if [ -f "$pidfile" ]; then
    local pid; pid=$(cat "$pidfile")
    kill -0 "$pid" 2>/dev/null && kill "$pid" && log "🛑 Stopped $name (pid $pid)" || log "⚪ $name not running"
    rm -f "$pidfile"
  else
    log "⚪ $name — no pidfile"
  fi
}

init_dirs() {
  mkdir -p "$PID_DIR" "$LOG_DIR" "$OBSCURA_HOME/qdrant/storage"
  touch "$ENV_FILE"
}

start_qdrant() {
  log "Checking Qdrant..."
  if is_running qdrant; then ok "Qdrant already running (pid $(cat "$PID_DIR/qdrant.pid"))"; return; fi

  # Check ~/.obscura/bin first (our install location), then fall back to PATH
  local bin
  bin="$OBSCURA_HOME/bin/qdrant"
  [ -x "$bin" ] || bin=$(which qdrant 2>/dev/null || true)
  if [ -z "$bin" ] || [ ! -x "$bin" ]; then
    warn "qdrant binary not found — using OBSCURA_QDRANT_MODE=local (SQLite, no server)"
    warn "To install: curl -fsSL https://github.com/qdrant/qdrant/releases/latest/download/qdrant-aarch64-apple-darwin.tar.gz | tar -xz -C ~/.obscura/bin"
    grep -q "OBSCURA_QDRANT_MODE" "$ENV_FILE" 2>/dev/null \
      || echo "export OBSCURA_QDRANT_MODE=local" >> "$ENV_FILE"
    return
  fi

  # Qdrant v1.x uses env vars for config (no --storage-path flag)
  QDRANT__STORAGE__STORAGE_PATH="$OBSCURA_HOME/qdrant/storage" \
    "$bin" >> "$LOG_DIR/qdrant.log" 2>&1 &
  write_pid $! qdrant
  sleep 2
  is_running qdrant \
    && ok "Qdrant started (pid $(cat "$PID_DIR/qdrant.pid"))" \
    || fail "Qdrant failed — check $LOG_DIR/qdrant.log"
}

start_fv_backend() {
  log "Checking fv-backend..."
  if is_running fv-backend; then ok "fv-backend already running (pid $(cat "$PID_DIR/fv-backend.pid"))"; return; fi

  local py="$PROJECT_DIR/.venv/bin/python"
  [ -f "$py" ] || py="python3"

  $py -c "import fv_backend" 2>/dev/null || { warn "fv_backend not importable — skipping"; return; }

  cd "$PROJECT_DIR"
  $py -m fv_backend.server >> "$LOG_DIR/fv-backend.log" 2>&1 &
  write_pid $! "fv-backend"
  sleep 1
  is_running "fv-backend" \
    && ok "fv-backend started (pid $(cat "$PID_DIR/fv-backend.pid"))" \
    || fail "fv-backend failed — check $LOG_DIR/fv-backend.log"
}

show_status() {
  hdr "Obscura Service Status"
  for svc in qdrant fv-backend; do
    is_running "$svc" \
      && ok "$svc  running (pid $(cat "$PID_DIR/$svc.pid"))" \
      || log "⚪ $svc  not running"
  done
  [ -s "$ENV_FILE" ] && { echo; log "Env overrides:"; sed 's/^/     /' "$ENV_FILE"; }
}

stop_all() {
  hdr "Stopping Obscura services"
  stop_service qdrant
  stop_service fv-backend
  ok "Done"
}

CMD="${1:-start}"
case "$CMD" in
  stop)   stop_all ;;
  status) show_status ;;
  quiet)
    init_dirs
    [ -f "$ENV_FILE" ] && source "$ENV_FILE" || true
    start_qdrant 2>/dev/null; start_fv_backend 2>/dev/null ;;
  *)
    hdr "Obscura Local Launch Kit"
    init_dirs
    [ -f "$ENV_FILE" ] && source "$ENV_FILE" || true
    start_qdrant
    start_fv_backend
    echo
    ok "Done. To apply env: source $ENV_FILE"
    ;;
esac
