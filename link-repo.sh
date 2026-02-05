#!/bin/bash
# Link a repo's .copilot directory to the Obsidian vault
# Usage: Run from repo root OR pass repo path as argument

set -e

VAULT_PATH="$HOME/FV-Copilot"
REPO_PATH="${1:-$(pwd)}"

# Ensure we're in a git repo
if [ ! -d "$REPO_PATH/.git" ]; then
    echo "Error: $REPO_PATH is not a git repository"
    exit 1
fi

# Get repo name
REPO_NAME=$(basename "$REPO_PATH")

# Create vault location for this repo (using dot.copilot convention)
VAULT_REPO_PATH="$VAULT_PATH/repos/$REPO_NAME"
mkdir -p "$VAULT_REPO_PATH"

# Remove existing .copilot if it's already a symlink
if [ -L "$REPO_PATH/.copilot" ]; then
    echo "Removing existing symlink at $REPO_PATH/.copilot"
    rm "$REPO_PATH/.copilot"
fi

# If .copilot exists as a directory, move it to vault as dot.copilot
if [ -d "$REPO_PATH/.copilot" ]; then
    echo "Moving existing .copilot directory to vault as dot.copilot..."
    mv "$REPO_PATH/.copilot" "$VAULT_REPO_PATH/dot.copilot"
fi

# Create symlink from repo to vault (using dot.copilot in vault)
ln -s "$VAULT_REPO_PATH/dot.copilot" "$REPO_PATH/.copilot"

echo "✓ Linked $REPO_NAME/.copilot → FV-Copilot/repos/$REPO_NAME/dot.copilot"
echo "✓ Edit context in Obsidian, changes appear in repo instantly"
