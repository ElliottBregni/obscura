#!/usr/bin/env bash
# Convenience wrapper to run the Playwright injector
set -euo pipefail
if [ -z "${1-}" ]; then
  echo "Usage: $0 <url> [--headless] [wait_ms]"
  exit 2
fi
URL="$1"
HEADLESS_FLAG=""
WAIT_MS=30000
if [ "${2-}" = "--headless" ]; then
  HEADLESS_FLAG="--headless"
  if [ -n "${3-}" ]; then WAIT_MS="$3"; fi
elif [ -n "${2-}" ]; then
  WAIT_MS="$2"
fi
python3 tools/playwright/inject_style.py --url "$URL" $HEADLESS_FLAG --wait "$WAIT_MS"
