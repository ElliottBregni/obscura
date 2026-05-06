#!/usr/bin/env bash
# Bootstrap Ghostty + Obscura for macOS and Linux.
#
# What this does:
#   1. Installs Ghostty if it is missing (Homebrew on macOS/Linux).
#   2. Writes a managed Ghostty config with sensible defaults.
#   3. Adds a small launcher script that opens Ghostty in the Obscura repo.
#
# Usage:
#   ./ghostty_obscura.sh
#   ./ghostty_obscura.sh --repo ~/dev/obscura
#   ./ghostty_obscura.sh --force
set -euo pipefail

REPO_ROOT="${OBSCURA_REPO:-$HOME/dev/obscura}"
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO_ROOT="$2"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      sed -n '1,25p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! -d "$REPO_ROOT" ]]; then
  echo "missing repo: $REPO_ROOT" >&2
  exit 1
fi

OS="$(uname -s)"
case "$OS" in
  Darwin)
    GHOSTTY_CONFIG_DIR="$HOME/Library/Application Support/com.mitchellh.ghostty"
    ;;
  Linux)
    GHOSTTY_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/ghostty"
    ;;
  *)
    echo "unsupported OS: $OS" >&2
    exit 1
    ;;
esac

GHOSTTY_CONFIG="$GHOSTTY_CONFIG_DIR/config"
LAUNCHER_DIR="$HOME/.local/bin"
LAUNCHER="$LAUNCHER_DIR/obscura-ghostty"

need_brew_install() {
  if command -v ghostty >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v brew >/dev/null 2>&1; then
    echo "Ghostty is missing and Homebrew is not installed." >&2
    echo "Install Homebrew or install Ghostty manually, then rerun this script." >&2
    exit 1
  fi
  brew install ghostty
}

write_config() {
  mkdir -p "$GHOSTTY_CONFIG_DIR"
  if [[ -e "$GHOSTTY_CONFIG" && "$FORCE" -ne 1 ]]; then
    echo "Ghostty config already exists: $GHOSTTY_CONFIG" >&2
    echo "Use --force to replace it." >&2
    exit 1
  fi

  cat > "$GHOSTTY_CONFIG" <<'EOF'
# Managed by scripts/launch/ghostty_obscura.sh
theme = "dark:Catppuccin Mocha,light:Catppuccin Latte"
auto-update = false
window-padding-x = 12
window-padding-y = 12
cursor-style = "block"
EOF
}

write_launcher() {
  mkdir -p "$LAUNCHER_DIR"
  cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec ghostty --working-directory="$REPO_ROOT"
EOF
  chmod +x "$LAUNCHER"
}

need_brew_install
write_config
write_launcher

echo "installed Ghostty config: $GHOSTTY_CONFIG"
echo "installed launcher:      $LAUNCHER"
echo "open Obscura with:       $LAUNCHER"
