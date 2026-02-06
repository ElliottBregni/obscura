#!/bin/bash
# Install watcher as macOS background service
# Usage: ./install-launchd-service.sh

VAULT_PATH="$HOME/FV-Copilot"
PLIST_PATH="$HOME/Library/LaunchAgents/com.fv-copilot.watch-and-sync.plist"

echo "📦 Installing Copilot watcher as background service..."

# Create LaunchAgent plist
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.fv-copilot.watch-and-sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/bregnie/FV-Copilot/watch-and-sync.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/fv-copilot-watcher.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/fv-copilot-watcher.log</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
PLIST

echo "✅ Plist created: $PLIST_PATH"

# Load the service
launchctl load "$PLIST_PATH" 2>/dev/null && {
    echo "✅ Service loaded and running"
    sleep 2
    launchctl list | grep fv-copilot
} || {
    echo "⚠️  Service may already be loaded. Try:"
    echo "   launchctl unload $PLIST_PATH"
    echo "   launchctl load $PLIST_PATH"
}

echo ""
echo "📋 Service commands:"
echo "   Check status:  launchctl list | grep fv-copilot"
echo "   View logs:     tail -f /tmp/fv-copilot-watcher.log"
echo "   Stop service:  launchctl unload $PLIST_PATH"
echo "   Start service: launchctl load $PLIST_PATH"
