#!/bin/bash
# FV-Copilot Vault Installer
# Easy one-command setup for developers

set -e

VAULT_NAME="FV-Copilot"
VAULT_PATH="$HOME/$VAULT_NAME"

echo "🚀 FV-Copilot Vault Installer"
echo "================================"
echo ""

# Check if vault already exists
if [ -d "$VAULT_PATH" ]; then
    echo "⚠️  Vault already exists at $VAULT_PATH"
    read -p "   Overwrite? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "❌ Installation cancelled"
        exit 1
    fi
    echo "   Backing up existing vault to $VAULT_PATH.backup..."
    mv "$VAULT_PATH" "$VAULT_PATH.backup.$(date +%Y%m%d_%H%M%S)"
fi

echo "📁 Creating vault structure..."
mkdir -p "$VAULT_PATH"/{repos,scratch,thinking,_attachments}

echo "🔗 Linking Copilot CLI config..."
if [ -d "$HOME/.copilot" ]; then
    ln -s "$HOME/.copilot" "$VAULT_PATH/copilot-cli"
    echo "   ✓ Linked ~/.copilot → $VAULT_NAME/copilot-cli"
else
    echo "   ⚠️  ~/.copilot not found (Copilot CLI may not be installed)"
    echo "   Run 'copilot' to initialize, then run this installer again"
fi

echo "📋 Copying scripts and docs..."
cd "$VAULT_PATH"

# Create .gitignore
cat > .gitignore << 'EOF'
# Obsidian metadata
.obsidian/
.DS_Store

# Symlinked repos (tracked in their respective repos)
repos/

# Symlinked Copilot CLI config (user-specific, do not commit)
copilot-cli/

# Package files (MCP servers, plugins, etc.)
**/pkg/
pkg/
*.pkg

# Vault-only folders (can be committed to vault repo if desired)
scratch/
thinking/
_attachments/
EOF

# Create README
cat > README.md << 'EOF'
# FV-Copilot Vault

Obsidian vault for managing GitHub Copilot context across multiple code repositories.

## Structure

```
~/FV-Copilot/
├── repos/              → Repo-specific .github content
│   └── RepoName/       → IS the .github folder content
├── docs/               → Vault documentation
├── copilot-cli/        → Symlink to ~/.copilot (CLI config)
├── scratch/            → Private notes
├── thinking/           → Working notes
└── _attachments/       → Vault-only assets
```

## How It Works

**Flattened structure:**
- `vault/repos/RepoName/` directly contains `.github` content
- `repo/.github` symlinks to `vault/repos/RepoName/`
- No nested `.github` folders in vault!

**Nested modules:**
- `.github` only created where code exists in repo
- Vault can have extra folders (skills/, instructions/) - no symlink needed

## Linking Repos

From any git repo:
```bash
cd ~/git/YourRepo
~/FV-Copilot/sync-github.sh --dry-run  # Test first
~/FV-Copilot/sync-github.sh            # Apply
```

Creates: `repo/.github` → `vault/repos/RepoName/`

## Rules

✅ **DO:**
- Edit `.github` content in Obsidian
- Create vault-only folders (skills/, docs/)
- Work in `.github` directories where code lives

❌ **DON'T:**
- Create `.github` symlinks manually
- Edit symlink targets directly
- Commit `repos/` or `copilot-cli/` to vault repo

## Documentation

See `docs/` folder:
- `INSTALL.md` - Installation guide
- `QUICKSTART.md` - Quick start
- `GITHUB-INTEGRATION.md` - How .github works
- `NO-OBSIDIAN.md` - Using without Obsidian
- `MCP-README.md` - MCP configuration
EOF

echo "📜 Creating installation guide..."
mkdir -p docs
cat > docs/INSTALL.md << 'EOF'
# FV-Copilot Installation Guide

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/your-org/FV-Copilot/main/install.sh | bash
```

Or clone and run:
```bash
git clone https://github.com/your-org/FV-Copilot.git ~/FV-Copilot
cd ~/FV-Copilot
chmod +x *.sh
```

## Prerequisites

### Optional but Recommended
- **Obsidian** (optional, but helpful) - Download from https://obsidian.md
  - The vault is just markdown files - works without Obsidian!
  - See `docs/NO-OBSIDIAN.md` for alternatives

### Required
- **GitHub Copilot CLI** - Install with:
  ```bash
  gh extension install github/gh-copilot
  ```
- **Git** - For version control

### Recommended MCP Servers
The Copilot CLI uses Model Context Protocol (MCP) servers for extended functionality.

See `docs/MCP-README.md` for configuration details.

## Vault Setup

1. (Optional) Open `~/FV-Copilot` in Obsidian
2. All repo `.github/` folders are symlinked to `repos/`
3. CLI config (including MCP) is at `copilot-cli/`

## Adding Repos

From any git repository:
```bash
# Test first with dry-run
cd ~/git/YourRepo
~/FV-Copilot/sync-github.sh --dry-run

# Apply if looks good
~/FV-Copilot/sync-github.sh
```

This intelligently:
- Finds all .github content in vault
- Creates symlinks for root + nested modules
- Only links where actual code directories exist

## Structure

```
~/git/YourRepo/
├── .github/ → vault/repos/YourRepo/
└── platform/module/
    └── .github/ → vault/repos/YourRepo/platform/module/
```

Vault can have extra folders (skills/, instructions/) without repo matches.

## MCP Configuration

- **Location**: `copilot-cli/mcp-config.json`
- **Manage**: Use `/mcp` commands in Copilot CLI
- **Enable/Disable**: `/mcp enable <server>` or `/mcp disable <server>`
- **Package installs**: Stored in `copilot-cli/pkg/` (gitignored)

## Notes

- `.gitignore` excludes symlinked content and pkg files
- Commit only vault-specific content (docs, scratch, thinking, etc.)
- Do not commit `repos/` or `copilot-cli/` - these are symlinks
EOF

echo "🔧 Creating helper scripts..."

cat > setup-vault.sh << 'SETUPEOF'
#!/bin/bash
# Bootstrap script to recreate vault structure on new machines

VAULT_PATH="$HOME/FV-Copilot"

# Create vault directories
mkdir -p "$VAULT_PATH"/{scratch,thinking,_attachments,repos,docs}

# Symlink ~/.copilot (CLI config)
if [ -d "$HOME/.copilot" ]; then
    ln -sf "$HOME/.copilot" "$VAULT_PATH/copilot-cli"
    echo "✓ Symlinked ~/.copilot → $VAULT_PATH/copilot-cli"
else
    echo "⚠️  ~/.copilot not found - run Copilot CLI once to initialize"
fi

echo "✓ Vault structure created at $VAULT_PATH"
echo ""
echo "Next: Copy sync-github.sh and docs from repo"
echo "Then use sync-github.sh to link your repositories"
SETUPEOF

chmod +x setup-vault.sh

echo ""
echo "✅ Installation complete!"
echo ""
echo "Next steps:"
echo "  1. (Optional) Open Obsidian: File → Open Vault → $VAULT_PATH"
echo "  2. Link your repos:"
echo "     cd ~/git/YourRepo"
echo "     $VAULT_PATH/sync-github.sh --dry-run"
echo "     $VAULT_PATH/sync-github.sh"
echo ""
echo "  3. (Optional) Initialize git in vault:"
echo "     cd $VAULT_PATH"
echo "     git init"
echo "     git add docs/ scratch/ thinking/ *.md *.sh"
echo "     git commit -m 'Initial vault setup'"
echo ""
echo "📖 Read $VAULT_PATH/README.md and $VAULT_PATH/docs/ for more info"
