#!/bin/bash
set -euo pipefail

LOG="${HOME}/.obscura/logs/browser-extension-host.log"
mkdir -p "$(dirname "$LOG")"

echo "--- ensure_playwright start $(date) ---" >> "$LOG"

# Node / npx check
if command -v npx >/dev/null 2>&1; then
  if npx --no-install playwright --version >/dev/null 2>&1; then
    echo "node-playwright present" >> "$LOG"
  else
    echo "npx present but node playwright not installed" >> "$LOG"
    echo "To install node playwright: npm install --save-dev playwright && npx playwright install" >> "$LOG"
  fi
else
  echo "npx not found" >> "$LOG"
  echo "To use node playwright, install Node and npm (Homebrew: brew install node), then run: npm install --save-dev playwright && npx playwright install" >> "$LOG"
fi

# Python check
if python3 -c "import importlib; importlib.import_module('playwright')" >/dev/null 2>&1; then
  echo "python playwright present" >> "$LOG"
else
  echo "python playwright not installed" >> "$LOG"
  echo "To install python playwright: pip3 install --user playwright && python3 -m playwright install" >> "$LOG"
fi

echo "--- ensure_playwright end $(date) ---" >> "$LOG"
