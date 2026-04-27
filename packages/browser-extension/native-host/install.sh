#!/usr/bin/env bash
# Install the Obscura native-messaging host manifest into every detected
# Chromium-based browser on this machine.
#
# The launcher script (obscura-native-host) is self-locating, so this
# installer only has to:
#   1. Compute the absolute path to the launcher.
#   2. Derive the extension ID from packages/browser-extension/manifest.json
#      (the manifest's `key` field pins a deterministic ID). Additional IDs
#      can be passed with --allow <id> or OBSCURA_EXT_ID=<id1,id2>.
#   3. Render com.obscura.host.json.tmpl with those values.
#   4. Copy the rendered manifest into every NativeMessagingHosts directory
#      that exists (Chrome, Chromium, Brave, Edge, Arc, Canary, Vivaldi).
#
# Usage:
#   ./install.sh                      # install with default extension ID
#   ./install.sh --allow <ext-id>     # add an additional allowed origin
#   ./install.sh --uninstall          # remove the installed manifest
#   ./install.sh --print              # print what would be installed
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SELF_DIR/../../.." && pwd)"
LAUNCHER="$SELF_DIR/obscura-native-host"
TEMPLATE="$SELF_DIR/com.obscura.host.json.tmpl"
MANIFEST_NAME="com.obscura.host.json"
EXT_MANIFEST="$REPO_ROOT/packages/browser-extension/manifest.json"

MODE="install"
EXTRA_IDS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow)      EXTRA_IDS+=("$2"); shift 2 ;;
    --allow=*)    EXTRA_IDS+=("${1#*=}"); shift ;;
    --uninstall)  MODE="uninstall"; shift ;;
    --print)      MODE="print"; shift ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *)
      echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Allow OBSCURA_EXT_ID=id1,id2 for extra origins without editing the command line.
if [[ -n "${OBSCURA_EXT_ID:-}" ]]; then
  IFS=',' read -r -a _env_ids <<< "$OBSCURA_EXT_ID"
  EXTRA_IDS+=("${_env_ids[@]}")
fi

# --- sanity checks ---
[[ -f "$LAUNCHER"     ]] || { echo "missing launcher: $LAUNCHER" >&2; exit 1; }
[[ -f "$TEMPLATE"     ]] || { echo "missing template: $TEMPLATE" >&2; exit 1; }
[[ -f "$EXT_MANIFEST" ]] || { echo "missing extension manifest: $EXT_MANIFEST" >&2; exit 1; }
chmod +x "$LAUNCHER"

# --- derive extension ID from manifest `key` ---
# Chrome computes the extension ID as the first 32 hex chars of SHA256(der(pubkey))
# re-mapped 0-9a-f -> a-p.
derive_ext_id() {
  python3 - "$EXT_MANIFEST" <<'PY'
import base64, hashlib, json, sys
m = json.load(open(sys.argv[1]))
key = m.get("key")
if not key:
    sys.exit("extension manifest has no `key` — pass --allow <id> explicitly")
h = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
print("".join(chr(ord("a") + int(c, 16)) for c in h))
PY
}

DEFAULT_ID="$(derive_ext_id)"

# --- build allowed_origins JSON array ---
ALL_IDS=("$DEFAULT_ID" "${EXTRA_IDS[@]+"${EXTRA_IDS[@]}"}")
ORIGINS_JSON="$(
  python3 - "${ALL_IDS[@]}" <<'PY'
import json, sys
seen = []
for i in sys.argv[1:]:
    i = i.strip()
    if i and i not in seen:
        seen.append(i)
print(json.dumps([f"chrome-extension://{i}/" for i in seen]))
PY
)"

# --- render manifest ---
RENDERED="$(mktemp)"
trap 'rm -f "$RENDERED"' EXIT
sed \
  -e "s|__LAUNCHER_PATH__|$LAUNCHER|g" \
  -e "s|__ALLOWED_ORIGINS__|$ORIGINS_JSON|g" \
  "$TEMPLATE" > "$RENDERED"

# --- target directories (per-user, macOS + Linux) ---
UNAME="$(uname -s)"
declare -a DIRS
case "$UNAME" in
  Darwin)
    DIRS=(
      "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
      "$HOME/Library/Application Support/Google/Chrome Canary/NativeMessagingHosts"
      "$HOME/Library/Application Support/Google/Chrome Beta/NativeMessagingHosts"
      "$HOME/Library/Application Support/Google/Chrome Dev/NativeMessagingHosts"
      "$HOME/Library/Application Support/Chromium/NativeMessagingHosts"
      "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts"
      "$HOME/Library/Application Support/Microsoft Edge/NativeMessagingHosts"
      "$HOME/Library/Application Support/Arc/User Data/NativeMessagingHosts"
      "$HOME/Library/Application Support/Vivaldi/NativeMessagingHosts"
    )
    ;;
  Linux)
    DIRS=(
      "$HOME/.config/google-chrome/NativeMessagingHosts"
      "$HOME/.config/google-chrome-beta/NativeMessagingHosts"
      "$HOME/.config/google-chrome-unstable/NativeMessagingHosts"
      "$HOME/.config/chromium/NativeMessagingHosts"
      "$HOME/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts"
      "$HOME/.config/microsoft-edge/NativeMessagingHosts"
      "$HOME/.config/vivaldi/NativeMessagingHosts"
    )
    ;;
  *)
    echo "unsupported OS: $UNAME (Chrome native-messaging on Windows uses the registry — not handled here)" >&2
    exit 1
    ;;
esac

if [[ "$MODE" == "print" ]]; then
  echo "# launcher : $LAUNCHER"
  echo "# origins  : $ORIGINS_JSON"
  echo "# manifest :"
  cat "$RENDERED"
  echo
  echo "# would install to:"
  for d in "${DIRS[@]}"; do
    [[ -d "$(dirname "$d")" ]] && echo "  $d/$MANIFEST_NAME"
  done
  exit 0
fi

installed=0
for DIR in "${DIRS[@]}"; do
  # Only install into browsers that are actually present on this machine
  # (i.e. the parent app-support dir exists).
  [[ -d "$(dirname "$DIR")" ]] || continue
  mkdir -p "$DIR"
  TARGET="$DIR/$MANIFEST_NAME"
  if [[ "$MODE" == "uninstall" ]]; then
    if [[ -f "$TARGET" ]]; then
      rm -f "$TARGET"
      echo "removed:   $TARGET"
      installed=$((installed + 1))
    fi
  else
    cp "$RENDERED" "$TARGET"
    echo "installed: $TARGET"
    installed=$((installed + 1))
  fi
done

if [[ "$installed" -eq 0 ]]; then
  if [[ "$MODE" == "uninstall" ]]; then
    echo "no manifests found to remove."
  else
    echo "no supported browsers detected — nothing installed." >&2
    exit 1
  fi
fi

if [[ "$MODE" == "install" ]]; then
  echo
  echo "Extension ID: $DEFAULT_ID"
  if [[ ${#EXTRA_IDS[@]} -gt 0 ]]; then
    echo "Extra IDs:    ${EXTRA_IDS[*]}"
  fi
  echo "Reload the extension in chrome://extensions (or equivalent) to pick up the new host."
fi
