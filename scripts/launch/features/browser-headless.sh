# DESC: Headless Chromium in the VM, CDP on host:9222 for DevTools/Playwright
# Sourced by colima-feature.sh. Expects colima_require_running already called.
# Provides: feature_enable, feature_disable, feature_status

CDP_PORT="${OBSCURA_CDP_PORT:-9222}"
HEADLESS_PID_NAME="chromium"

feature_enable() {
  echo "⟳ Enabling browser-headless..."
  ssh -F "$SSH_CONFIG" "$SSH_HOST" bash <<'INSTALL'
    set -e
    export PATH="$HOME/.local/bin:$PATH"
    if ls ~/.cache/ms-playwright/chromium-*/chrome-linux/chrome 2>/dev/null | head -1 | grep -q .; then
      echo "✓ playwright chromium already installed"
    else
      echo "⟳ Installing playwright chromium (this is ~200MB and one-time)..."
      # Install playwright as a standalone uv tool — uses the same
      # isolated-venv-per-tool model as pipx but is what the rest of the
      # bootstrap uses.
      if ! command -v playwright &>/dev/null; then
        uv tool install playwright >/dev/null
        export PATH="$HOME/.local/bin:$PATH"
      fi
      # --with-deps installs apt prerequisites via sudo. If sudo is not
      # passwordless, drop --with-deps and warn the user.
      if sudo -n true 2>/dev/null; then
        playwright install --with-deps chromium
      else
        echo "⚠️  passwordless sudo unavailable; skipping --with-deps"
        echo "   if chromium fails to launch, run inside the VM:"
        echo "     playwright install --with-deps chromium"
        playwright install chromium
      fi
    fi
INSTALL

  ssh -F "$SSH_CONFIG" "$SSH_HOST" bash <<EOF
    set -e
    pid_file=\$HOME/.obscura/launch/pids/${HEADLESS_PID_NAME}.pid
    log_file=\$HOME/.obscura/launch/logs/${HEADLESS_PID_NAME}.log
    mkdir -p "\$(dirname "\$pid_file")" "\$(dirname "\$log_file")"
    if [[ -f "\$pid_file" ]] && kill -0 "\$(cat "\$pid_file")" 2>/dev/null; then
      echo "✓ chromium already running (pid \$(cat "\$pid_file"))"
      exit 0
    fi
    chrome_bin=\$(ls -d \$HOME/.cache/ms-playwright/chromium-*/chrome-linux/chrome 2>/dev/null | head -1)
    if [[ -z "\$chrome_bin" ]]; then
      echo "❌ chromium binary not found after install"
      exit 1
    fi
    nohup "\$chrome_bin" \\
      --headless=new \\
      --no-sandbox \\
      --disable-gpu \\
      --disable-dev-shm-usage \\
      --user-data-dir=\$HOME/.obscura/chromium-profile \\
      --remote-debugging-port=${CDP_PORT} \\
      --remote-debugging-address=0.0.0.0 \\
      > "\$log_file" 2>&1 &
    echo \$! > "\$pid_file"
    sleep 1
    if ! kill -0 "\$(cat "\$pid_file")" 2>/dev/null; then
      echo "❌ chromium failed to start (last lines of log):"
      tail -20 "\$log_file" || true
      rm -f "\$pid_file"
      exit 1
    fi
    echo "✓ chromium running (pid \$(cat "\$pid_file"))"
EOF

  # Expose CDP port to host so DevTools / playwright on the Mac can attach.
  forward_port "$CDP_PORT"
  echo
  echo "🌐 CDP available at http://localhost:${CDP_PORT}"
  echo "   • Open chrome://inspect on your Mac and click 'Configure...' → add localhost:${CDP_PORT}"
  echo "   • Or attach Playwright: chromium.connect_over_cdp('http://localhost:${CDP_PORT}')"
}

feature_disable() {
  echo "⟳ Disabling browser-headless..."
  stop_forward "$CDP_PORT"
  ssh -F "$SSH_CONFIG" "$SSH_HOST" bash <<EOF || true
    pid_file=\$HOME/.obscura/launch/pids/${HEADLESS_PID_NAME}.pid
    if [[ -f "\$pid_file" ]]; then
      pid=\$(cat "\$pid_file")
      kill "\$pid" 2>/dev/null || true
      rm -f "\$pid_file"
      echo "✓ chromium stopped (pid \$pid)"
    else
      echo "✓ chromium not running"
    fi
EOF
}

feature_status() {
  local pid_file="$LAUNCH_DIR/pids/colima-fwd-${CDP_PORT}.pid"
  local fwd_pid="(none)"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    fwd_pid=$(cat "$pid_file")
  fi
  echo "  CDP forward host:${CDP_PORT} → VM:${CDP_PORT} (pid: $fwd_pid)"
  if [[ -n "${SSH_CONFIG:-}" ]]; then
    ssh -F "$SSH_CONFIG" "$SSH_HOST" bash <<EOF 2>/dev/null || echo "  (VM unreachable)"
      pid_file=\$HOME/.obscura/launch/pids/${HEADLESS_PID_NAME}.pid
      if [[ -f "\$pid_file" ]] && kill -0 "\$(cat "\$pid_file")" 2>/dev/null; then
        echo "  chromium VM pid \$(cat "\$pid_file") — RUNNING"
      else
        echo "  chromium — not running"
      fi
EOF
  fi
}
