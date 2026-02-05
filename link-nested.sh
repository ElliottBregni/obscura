#!/bin/bash
# Link a nested .copilot directory within a repo to the vault
# Usage: ./link-nested.sh <relative-path-from-repo-root>
# Example: ./link-nested.sh platform/partview_core/partview_service

set -e

VAULT_PATH="$HOME/FV-Copilot"
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)

if [ -z "$REPO_ROOT" ]; then
    echo "Error: Must run from within a git repository"
    exit 1
fi

if [ -z "$1" ]; then
    echo "Usage: $0 <relative-path-from-repo-root>"
    echo "Example: $0 platform/partview_core/partview_service"
    exit 1
fi

RELATIVE_PATH="$1"
REPO_NAME=$(basename "$REPO_ROOT")
VAULT_MODULE_PATH="$VAULT_PATH/repos/$REPO_NAME/$RELATIVE_PATH/dot.copilot"
REPO_MODULE_PATH="$REPO_ROOT/$RELATIVE_PATH/.copilot"

# Create vault directory
mkdir -p "$VAULT_MODULE_PATH"

# If .copilot exists in repo, move it to vault
if [ -d "$REPO_MODULE_PATH" ] && [ ! -L "$REPO_MODULE_PATH" ]; then
    echo "Moving existing .copilot to vault..."
    mv "$REPO_MODULE_PATH"/* "$VAULT_MODULE_PATH/" 2>/dev/null || true
    rmdir "$REPO_MODULE_PATH"
fi

# Remove existing symlink if present
if [ -L "$REPO_MODULE_PATH" ]; then
    rm "$REPO_MODULE_PATH"
fi

# Create symlink
ln -s "$VAULT_MODULE_PATH" "$REPO_MODULE_PATH"

echo "✓ Linked $RELATIVE_PATH/.copilot → vault/repos/$REPO_NAME/$RELATIVE_PATH/dot.copilot"
