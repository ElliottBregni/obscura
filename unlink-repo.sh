#!/bin/bash
# Unlink a repo's .copilot directory and move content back to repo
# Usage: Run from repo root OR pass repo path as argument

set -e

VAULT_PATH="$HOME/FV-Copilot"
REPO_PATH="${1:-$(pwd)}"
REPO_NAME=$(basename "$REPO_PATH")
VAULT_REPO_PATH="$VAULT_PATH/repos/$REPO_NAME/dot.copilot"

if [ ! -L "$REPO_PATH/.copilot" ]; then
    echo "Error: $REPO_PATH/.copilot is not a symlink"
    exit 1
fi

# Copy content back to repo
echo "Moving vault content back to repo..."
rm "$REPO_PATH/.copilot"
mkdir -p "$REPO_PATH/.copilot"
cp -R "$VAULT_REPO_PATH"/* "$REPO_PATH/.copilot/"

echo "✓ Unlinked $REPO_NAME/.copilot"
echo "✓ Content moved back to repo (vault copy remains)"
