#!/usr/bin/env bash
set -euo pipefail

if [[ "${OBSCURA_AUTH_ENABLED:-true}" != "true" ]]; then
  exit 0
fi

JWKS_URI="${OBSCURA_AUTH_JWKS_URI:-}"
if [[ -z "$JWKS_URI" ]]; then
  exit 0
fi

TIMEOUT_SECS="${OBSCURA_JWKS_WAIT_TIMEOUT_SECS:-180}"
INTERVAL_SECS="${OBSCURA_JWKS_WAIT_INTERVAL_SECS:-3}"
STRICT_MODE="${OBSCURA_JWKS_STRICT:-false}"
HOST_HEADER="${OBSCURA_AUTH_HOST_HEADER:-}"
export JWKS_URI="$JWKS_URI"
export TIMEOUT_SECS="$TIMEOUT_SECS"
export INTERVAL_SECS="$INTERVAL_SECS"
export STRICT_MODE="$STRICT_MODE"
export HOST_HEADER="$HOST_HEADER"

python - <<'PY'
import json
import os
import sys
import time
from urllib import request
from urllib.error import URLError, HTTPError

uri = os.environ.get("JWKS_URI")
timeout_secs = int(os.environ.get("TIMEOUT_SECS", "180"))
interval_secs = int(os.environ.get("INTERVAL_SECS", "3"))
host_header = os.environ.get("HOST_HEADER", "").strip()

headers = {}
if host_header:
    headers["Host"] = host_header

deadline = time.time() + timeout_secs
last_err = ""

while time.time() < deadline:
    try:
        req = request.Request(uri, headers=headers)
        with request.urlopen(req, timeout=5) as resp:
            status = getattr(resp, "status", 200)
            body = resp.read().decode("utf-8", errors="replace")
        if status == 200:
            data = json.loads(body)
            keys = data.get("keys", []) if isinstance(data, dict) else []
            if isinstance(keys, list) and len(keys) > 0:
                print(f"JWKS ready with {len(keys)} key(s)")
                sys.exit(0)
            last_err = "jwks returned empty keys"
        else:
            last_err = f"jwks http {status}"
    except HTTPError as exc:
        last_err = f"jwks http {exc.code}"
    except URLError as exc:
        last_err = str(exc)
    except Exception as exc:
        last_err = str(exc)

    time.sleep(interval_secs)

strict_mode = os.environ.get("STRICT_MODE", "false").lower() == "true"
msg = f"Timed out waiting for JWKS at {uri}: {last_err}"
if strict_mode:
    print(msg, file=sys.stderr)
    sys.exit(1)

print(f"{msg}. Continuing because OBSCURA_JWKS_STRICT=false.", file=sys.stderr)
sys.exit(0)
PY
