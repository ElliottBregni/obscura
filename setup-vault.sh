#!/bin/bash
# Bootstrap script to recreate Obsidian vault structure and symlinks

VAULT_PATH="$HOME/FV-Copilot"

# Create vault directories
mkdir -p "$VAULT_PATH"/{scratch,thinking,_attachments,repos}

# Symlink ~/.copilot (CLI config)
ln -sf "$HOME/.copilot" "$VAULT_PATH/copilot-cli"

echo "✓ Vault structure created at $VAULT_PATH"
echo "✓ Symlinked ~/.copilot → $VAULT_PATH/copilot-cli"
echo ""
echo "Open $VAULT_PATH in Obsidian to start editing."
echo "Use link-repo.sh to add repo-specific .copilot folders."
