#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  Obscura Launch Kit — host-side Chrome CDP launcher
#  Run on your Mac (NOT inside the VM). Starts Chrome with a debugging
#  port + dedicated user-data-dir so playwright (in the VM, via the
#  reverse-forwarded CDP port) can drive a visible browser session.
#
#  Pairs with scripts/launch/features/host-chrome-cdp.sh — that one
#  reverse-forwards the port into the VM and writes the env var.
#
#  Usage:
#    host-chrome-cdp.sh                  # start (idempotent — no-op if up)
#    host-chrome-cdp.sh --status         # is CDP listening?
#    host-chrome-cdp.sh --stop           # stop the debug Chrome
#    host-chrome-cdp.sh --restart        # stop + start
#
#  Options:
#    --port PORT             CDP port           (default: 9222)
#    --user-data-dir PATH    profile dir        (default: ~/.config/obscura-chrome-debug)
#    --app PATH              .app bundle path   (default: Google Chrome)
#
#  Env equivalents:
#    OBSCURA_CDP_PORT, OBSCURA_CHROME_USER_DATA, OBSCURA_CHROME_APP
# ─────────────────────────────────────────────
set -euo pipefail

CDP_PORT="${OBSCURA_CDP_PORT:-9222}"
USER_DATA_DIR="${OBSCURA_CHROME_USER_DATA:-$HOME/.config/obscura-chrome-debug}"
CHROME_APP="${OBSCURA_CHROME_APP:-/Applications/Google Chrome.app}"

ACTION=start
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)            CDP_PORT="$2"; shift 2;;
    --user-data-dir)   USER_DATA_DIR="$2"; shift 2;;
    --app)             CHROME_APP="$2"; shift 2;;
    --status)          ACTION=status; shift;;
    --stop)            ACTION=stop; shift;;
    --restart)         ACTION=restart; shift;;
    -h|--help)
      sed -n '3,/^# ──*$/p' "$0" | sed 's/^# \?//'
      exit 0;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done

CHROME_BIN="$CHROME_APP/Contents/MacOS/$(basename "$CHROME_APP" .app)"
[[ -x "$CHROME_BIN" ]] || CHROME_BIN="$CHROME_APP/Contents/MacOS/Google Chrome"

is_cdp_up() {
  curl -sf "http://localhost:$CDP_PORT/json/version" >/dev/null 2>&1
}

cdp_pid() {
  # Find the Chrome process bound to the CDP port. Use lsof since pgrep on
  # macOS doesn't see the listening port directly.
  lsof -tiTCP:"$CDP_PORT" -sTCP:LISTEN 2>/dev/null | head -1
}

start_chrome() {
  if is_cdp_up; then
    echo "✓ Chrome CDP already up at http://localhost:$CDP_PORT (pid $(cdp_pid))"
    return 0
  fi
  if [[ ! -x "$CHROME_BIN" ]]; then
    echo "❌ Chrome binary not found at: $CHROME_BIN" >&2
    echo "   Override with --app /path/to/YourBrowser.app or OBSCURA_CHROME_APP" >&2
    exit 1
  fi
  mkdir -p "$USER_DATA_DIR"
  echo "⟳ Starting Chrome with CDP on port $CDP_PORT"
  echo "   profile: $USER_DATA_DIR"
  nohup "$CHROME_BIN" \
    --remote-debugging-port="$CDP_PORT" \
    --user-data-dir="$USER_DATA_DIR" \
    --no-first-run \
    --no-default-browser-check \
    --disable-features=IsolateOrigins,site-per-process \
    >/dev/null 2>&1 &
  disown
  # Wait up to ~3s for the port to bind.
  for _ in 1 2 3 4 5 6; do
    sleep 0.5
    is_cdp_up && break
  done
  if ! is_cdp_up; then
    echo "❌ Chrome failed to start CDP listener within 3s." >&2
    echo "   It may already be running with a *different* user-data-dir," >&2
    echo "   in which case --remote-debugging-port is silently ignored." >&2
    echo "   Either --stop the existing Chrome or use a unique --user-data-dir." >&2
    exit 1
  fi
  echo "✓ Chrome CDP up at http://localhost:$CDP_PORT (pid $(cdp_pid))"
  echo
  echo "Next: bridge into the VM with"
  echo "  scripts/launch/colima-feature.sh enable host-chrome-cdp"
}

stop_chrome() {
  local pid
  pid=$(cdp_pid)
  if [[ -z "$pid" ]]; then
    echo "✓ no Chrome listening on port $CDP_PORT"
    return 0
  fi
  # Kill the parent Chrome process; helper processes will follow.
  local parent
  parent=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
  # If the parent is a Chrome process too, prefer it (the actual app).
  if [[ -n "$parent" ]] && ps -p "$parent" -o command= 2>/dev/null | grep -q 'Google Chrome'; then
    kill "$parent" 2>/dev/null && echo "✓ stopped Chrome (parent pid $parent)"
  else
    kill "$pid" 2>/dev/null && echo "✓ stopped Chrome (pid $pid)"
  fi
  # Give it a moment to exit; force if it doesn't.
  sleep 1
  if is_cdp_up; then
    pid=$(cdp_pid)
    [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null && echo "  forced kill -9 $pid"
  fi
}

case "$ACTION" in
  status)
    if is_cdp_up; then
      echo "✓ Chrome CDP up at http://localhost:$CDP_PORT (pid $(cdp_pid))"
      echo "--- /json/version ---"
      curl -s "http://localhost:$CDP_PORT/json/version" | python3 -m json.tool
      echo "--- /json/list (first page) ---"
      curl -s "http://localhost:$CDP_PORT/json/list" \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d[0] if d else {}, indent=2))'
    else
      echo "✗ Chrome CDP not running on port $CDP_PORT"
      exit 1
    fi
    ;;
  stop)
    stop_chrome
    ;;
  restart)
    stop_chrome
    sleep 1
    start_chrome
    ;;
  start)
    start_chrome
    ;;
esac
