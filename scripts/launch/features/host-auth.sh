# DESC: Copy the Mac's obscura-auth session into the VM (~/.obscura/credentials.json)
# Sourced by colima-feature.sh.
#
# How it works:
#   1. On the Mac, the obscura-auth session lives either in Keychain (default)
#      or in ~/.obscura/credentials.json (override / fallback).
#   2. Inside the VM there's no Keychain, so obscura's CliSession code falls
#      through to ~/.obscura/credentials.json.
#   3. This feature reads the host-side session via a Python one-liner that
#      mirrors what `obscura-auth` itself reads, then scp's it into the VM
#      with mode 0600.
#
# Re-run this whenever you re-authenticate on the host (`obscura-auth login`)
# or when the access token expires and you've refreshed locally.

CRED_REL_PATH=".obscura/credentials.json"
HOST_SESSION_TMP=""

_extract_host_session() {
  # Returns 0 with the session JSON written to $1, or non-zero on failure.
  local out_file="$1"
  python3 - "$out_file" <<'PY'
import json, os, sys
from pathlib import Path

# Mirror obscura.auth.cli_session: try keyring first, then fall back file.
service = "obscura-cli"
username = "supabase-session"

payload = None
try:
    import keyring
    payload = keyring.get_password(service, username)
except Exception:
    payload = None

if not payload:
    home = Path(os.environ.get("OBSCURA_HOME") or (Path.home() / ".obscura"))
    f = Path(os.environ.get("OBSCURA_CREDENTIALS_FILE") or (home / "credentials.json"))
    if f.is_file():
        payload = f.read_text()

if not payload:
    print("no host session found (Keychain miss + no credentials.json)", file=sys.stderr)
    sys.exit(2)

# Validate it's parseable JSON with the expected fields.
try:
    data = json.loads(payload)
    for k in ("access_token", "refresh_token", "expires_at", "user_id"):
        if k not in data:
            raise KeyError(k)
except Exception as e:
    print(f"host session is unreadable: {e}", file=sys.stderr)
    sys.exit(3)

Path(sys.argv[1]).write_text(payload)
PY
}

feature_enable() {
  echo "⟳ Bridging host obscura-auth session into VM..."

  HOST_SESSION_TMP=$(mktemp)
  trap '[[ -n "$HOST_SESSION_TMP" && -f "$HOST_SESSION_TMP" ]] && rm -f "$HOST_SESSION_TMP"' RETURN

  if ! _extract_host_session "$HOST_SESSION_TMP"; then
    echo "❌ Could not read host session." >&2
    echo "   Run \`obscura-auth login\` on your Mac first." >&2
    return 1
  fi
  chmod 600 "$HOST_SESSION_TMP"

  ssh -F "$SSH_CONFIG" "$SSH_HOST" "mkdir -p ~/.obscura && chmod 0700 ~/.obscura"
  scp -F "$SSH_CONFIG" -q "$HOST_SESSION_TMP" "$SSH_HOST:~/$CRED_REL_PATH"
  ssh -F "$SSH_CONFIG" "$SSH_HOST" "chmod 0600 ~/$CRED_REL_PATH"

  echo "✓ session written to ~/$CRED_REL_PATH in VM"
  echo "  re-run this feature after \`obscura-auth login\` or token refresh."
}

feature_disable() {
  echo "⟳ Removing VM-side credentials..."
  ssh -F "$SSH_CONFIG" "$SSH_HOST" "rm -f ~/$CRED_REL_PATH" 2>/dev/null || true
  echo "✓ ~/$CRED_REL_PATH removed in VM"
}

feature_status() {
  if [[ -n "${SSH_CONFIG:-}" ]]; then
    ssh -F "$SSH_CONFIG" "$SSH_HOST" bash <<EOF 2>/dev/null || echo "  (VM unreachable)"
      if [[ -f ~/$CRED_REL_PATH ]]; then
        ts=\$(stat -c '%y' ~/$CRED_REL_PATH 2>/dev/null || stat -f '%Sm' ~/$CRED_REL_PATH 2>/dev/null)
        echo "  credentials.json present (mtime: \$ts)"
      else
        echo "  credentials.json NOT present"
      fi
EOF
  fi
}
