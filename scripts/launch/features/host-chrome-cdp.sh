# DESC: Bridge your host Chrome's CDP into the VM so playwright drives the visible browser
# Sourced by colima-feature.sh.
#
# What this does:
#   1. Verifies a Chrome on your Mac is exposing CDP at localhost:<port>.
#   2. Reverse-forwards that port into the VM (-R) so VM:<port> = host:<port>.
#   3. Sets OBSCURA_BROWSER_CDP=http://localhost:<port> in ~/.obscura/.env
#      inside the VM. obscura's playwright tool reads that env var and
#      calls `chromium.connect_over_cdp(...)` instead of launching its own
#      headless instance — the agent ends up driving the visible Chrome
#      on your Mac, with all your real cookies/login state.
#
# To start Chrome with CDP enabled (do this on your Mac, once per session):
#
#   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
#     --remote-debugging-port=9222 \
#     --user-data-dir="$HOME/.config/obscura-chrome-debug"
#
# Use a separate --user-data-dir so it doesn't fight with your normal
# Chrome window. First run: log in to whatever sites the agent needs.
# Cookies persist in the user-data-dir for future runs.

CDP_PORT="${OBSCURA_CDP_PORT:-9222}"
ENV_FILE_REL=".obscura/.env"

feature_enable() {
  echo "⟳ Checking host Chrome CDP at localhost:${CDP_PORT}..."
  local chrome_info
  if ! chrome_info=$(curl -sf "http://localhost:${CDP_PORT}/json/version" 2>/dev/null); then
    cat >&2 <<EOF
❌ No Chrome found listening on host:${CDP_PORT}.

Start one with:

  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
    --remote-debugging-port=${CDP_PORT} \\
    --user-data-dir="\$HOME/.config/obscura-chrome-debug"

(use a fresh --user-data-dir to avoid fighting with your normal Chrome.)

Then re-run: scripts/launch/colima-feature.sh enable host-chrome-cdp
EOF
    return 1
  fi
  echo "✓ host Chrome present:"
  echo "$chrome_info" | python3 -c "import sys,json; d=json.load(sys.stdin); print('   ', d.get('Browser','?'), '—', d.get('User-Agent','?'))" 2>/dev/null || true

  # Reverse-forward CDP port into the VM.
  reverse_port "$CDP_PORT"

  # Persist OBSCURA_BROWSER_CDP in the VM's ~/.obscura/.env so
  # subsequent obscura processes pick it up automatically.
  ssh -F "$SSH_CONFIG" "$SSH_HOST" bash <<EOF
    set -e
    mkdir -p ~/.obscura
    chmod 0700 ~/.obscura
    env_file=~/${ENV_FILE_REL}
    touch "\$env_file"
    chmod 0600 "\$env_file"
    # Drop any prior OBSCURA_BROWSER_CDP line, then append the new one.
    grep -v '^OBSCURA_BROWSER_CDP=' "\$env_file" > "\$env_file.tmp" || true
    echo 'OBSCURA_BROWSER_CDP=http://localhost:${CDP_PORT}' >> "\$env_file.tmp"
    mv "\$env_file.tmp" "\$env_file"
EOF
  echo "✓ wrote OBSCURA_BROWSER_CDP=http://localhost:${CDP_PORT} to VM ~/${ENV_FILE_REL}"

  echo
  echo "🎯 VM-side obscura's playwright will now drive your host Chrome."
  echo "   The next \`obscura\` invocation in the VM picks this up automatically."
}

feature_disable() {
  echo "⟳ Disabling host-chrome-cdp..."
  stop_reverse "$CDP_PORT"
  ssh -F "$SSH_CONFIG" "$SSH_HOST" bash <<EOF
    env_file=~/${ENV_FILE_REL}
    if [[ -f "\$env_file" ]]; then
      grep -v '^OBSCURA_BROWSER_CDP=' "\$env_file" > "\$env_file.tmp" || true
      mv "\$env_file.tmp" "\$env_file"
    fi
    echo "✓ removed OBSCURA_BROWSER_CDP from VM ~/${ENV_FILE_REL}"
EOF
}

feature_status() {
  if curl -sf "http://localhost:${CDP_PORT}/json/version" >/dev/null 2>&1; then
    echo "  host Chrome CDP: localhost:${CDP_PORT} — UP"
  else
    echo "  host Chrome CDP: localhost:${CDP_PORT} — not running"
  fi
  local pid_file="$LAUNCH_DIR/pids/colima-rev-${CDP_PORT}.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "  reverse-forward VM:${CDP_PORT} → host:${CDP_PORT}: pid $(cat "$pid_file")"
  else
    echo "  reverse-forward: not running"
  fi
  if [[ -n "${SSH_CONFIG:-}" ]]; then
    ssh -F "$SSH_CONFIG" "$SSH_HOST" \
      "grep '^OBSCURA_BROWSER_CDP=' ~/${ENV_FILE_REL} 2>/dev/null || echo '  (env not set in VM)'"
  fi
}
