#!/usr/bin/env bash
# Obscura installer — works on macOS, Linux, and WSL
set -euo pipefail

VERSION="${OBSCURA_VERSION:-latest}"
INSTALL_DIR="${OBSCURA_INSTALL_DIR:-$HOME/.local/bin}"
OBSCURA_HOME="${OBSCURA_HOME:-$HOME/.obscura}"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[obscura]${NC} $*"; }
warn()  { echo -e "${YELLOW}[obscura]${NC} $*"; }
error() { echo -e "${RED}[obscura]${NC} $*" >&2; exit 1; }

# --- Preflight ---

command -v python3 >/dev/null 2>&1 || error "python3 not found. Install Python 3.13+."

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if (( PY_MAJOR < 3 || PY_MINOR < 13 )); then
    error "Python 3.13+ required (found $PY_VERSION)"
fi

# Prefer uv, fall back to pip
if command -v uv >/dev/null 2>&1; then
    INSTALLER="uv"
elif command -v pipx >/dev/null 2>&1; then
    INSTALLER="pipx"
else
    INSTALLER="pip"
fi

info "Python $PY_VERSION detected, using $INSTALLER"

# --- Install ---

case "$INSTALLER" in
    uv)
        info "Installing obscura with uv..."
        if [ "$VERSION" = "latest" ]; then
            uv tool install obscura
        else
            uv tool install "obscura==$VERSION"
        fi
        ;;
    pipx)
        info "Installing obscura with pipx..."
        if [ "$VERSION" = "latest" ]; then
            pipx install obscura
        else
            pipx install "obscura==$VERSION"
        fi
        ;;
    pip)
        info "Installing obscura with pip..."
        mkdir -p "$INSTALL_DIR"
        if [ "$VERSION" = "latest" ]; then
            python3 -m pip install --user obscura
        else
            python3 -m pip install --user "obscura==$VERSION"
        fi
        ;;
esac

# --- Post-install: create ~/.obscura/ structure ---

info "Setting up $OBSCURA_HOME..."
for dir in output memory vector_memory plugins specs state mcp hooks; do
    mkdir -p "$OBSCURA_HOME/$dir"
done

# --- Verify ---

if command -v obscura >/dev/null 2>&1; then
    info "Installed successfully: $(obscura --version 2>/dev/null || echo 'obscura ready')"
else
    warn "obscura installed but not on PATH."
    warn "Add to your shell profile:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

info "Data directory: $OBSCURA_HOME"
info "Run 'obscura' to start."
