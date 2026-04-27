#!/usr/bin/env bash
# Wrapper intended to be called by Obscura tasks or CI. Reads env vars:
# OBSCURA_PLAYWRIGHT_URL - target URL (required)
# OBSCURA_PLAYWRIGHT_PROFILE - optional Playwright user-data-dir path to reuse profile
# OBSCURA_PLAYWRIGHT_COOKIES - optional cookies JSON file path to load
# OBSCURA_PLAYWRIGHT_HEADLESS - if set to '1' runs headless

set -euo pipefail
URL="${OBSCURA_PLAYWRIGHT_URL-}"
if [ -z "$URL" ]; then
  echo "Please set OBSCURA_PLAYWRIGHT_URL to the target page URL" >&2
  exit 2
fi
USER_DATA_DIR="${OBSCURA_PLAYWRIGHT_PROFILE-}"
COOKIES_FILE="${OBSCURA_PLAYWRIGHT_COOKIES-}"
HEADLESS_FLAG=""
if [ "${OBSCURA_PLAYWRIGHT_HEADLESS-}" = "1" ]; then
  HEADLESS_FLAG="--headless"
fi
python3 tools/playwright/inject_style.py --url "$URL" $HEADLESS_FLAG ${USER_DATA_DIR:+--user-data-dir "$USER_DATA_DIR"} ${COOKIES_FILE:+--cookies-file "$COOKIES_FILE"}
