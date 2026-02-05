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
# Obsidian Vault for Code Repos

## Structure

- **repos/** → per-repo context (each repo's `.copilot/` symlinks here)
- **copilot-cli/** → symlink to `~/.copilot` (Copilot CLI config and state)
- **scratch/** → private notes, drafts, experiments
- **thinking/** → working through ideas
- **_attachments/** → vault-only assets

## Linking Repos

From any git repo root:
```bash
~/FV-Copilot/sync-copilot.sh . --dry-run  # Test first
~/FV-Copilot/sync-copilot.sh .            # Apply
```

This creates `repo/.copilot/` → `vault/repos/{repo-name}/dot.copilot/`

**Note**: Hidden files (`.copilot`, `.claude`) are stored as `dot.copilot`, `dot.claude` in the vault to avoid Obsidian conflicts.

## Rules

- Files in `repos/{repo-name}/` are **real repo files** (via symlink)
- Files in `copilot-cli/` are **CLI config** (user-specific, not committed)
- Files outside these are **vault-only** (not in git)
- Edit skills and context in Obsidian, commit from repo when ready
- Use scratch/thinking for iteration before promotion

## Commands

### Syncing .copilot Directories
- `./sync-copilot.sh <path> [--dry-run]` - **Recommended**: Bidirectional sync with merge
  - Merges content from both repo and vault
  - Works forward (repo→vault) or backward (vault→repo)
  - Use `--dry-run` to test without making changes
  - Examples:
    - `./sync-copilot.sh .` - Sync repo root
    - `./sync-copilot.sh platform/service` - Sync nested module
    - `./sync-copilot.sh . --dry-run` - Test first

### Legacy Scripts
- `./link-repo.sh` - Link current repo's .copilot to vault (simple)
- `./link-nested.sh <path>` - Link nested .copilot dir
- `./unlink-repo.sh` - Unlink and move content back to repo
- `./setup-vault.sh` - Recreate vault structure on new machines

## Installation & MCP Config

See [INSTALL.md](./INSTALL.md) for:
- Prerequisites and tools
- MCP server configuration (`copilot-cli/mcp-config.json`)
- Adding new repos to the vault
EOF

echo "📜 Creating installation guide..."
cat > INSTALL.md << 'EOF'
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

### Required
- **Obsidian** - Download from https://obsidian.md
- **GitHub Copilot CLI** - Install with:
  ```bash
  brew install copilot-cli
  # or
  npm install -g @github/copilot
  ```
- **Git** - For version control

### Recommended MCP Servers
The Copilot CLI uses Model Context Protocol (MCP) servers for extended functionality.

Current MCP config location: `copilot-cli/mcp-config.json` (symlinked to `~/.copilot/mcp-config.json`)

#### To Add More MCP Servers
Use the Copilot CLI:
```bash
/mcp add <server-name>
```

Or manually edit `copilot-cli/mcp-config.json`

### Python Tools (if working with Python services)
```bash
# Per-service virtualenv
cd platform/some-service
python3.9 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Node/Serverless Tools
```bash
npm install -g serverless
```

## Vault Setup

1. Open `~/FV-Copilot` in Obsidian
2. Trust the vault when prompted
3. All repo `.copilot/` folders are symlinked to `repos/`
4. CLI config (including MCP) is at `copilot-cli/`

## Adding New Repos

### Recommended: Use sync-copilot.sh
From any git repository:
```bash
# Test first with dry-run
cd ~/git/YourRepo
~/FV-Copilot/sync-copilot.sh . --dry-run

# Apply if looks good
~/FV-Copilot/sync-copilot.sh .
```

This intelligently:
- Merges existing content from repo and vault
- Creates `dot.copilot` in vault
- Symlinks `repo/.copilot` → vault

### For nested modules:
```bash
cd ~/git/YourRepo
~/FV-Copilot/sync-copilot.sh platform/service --dry-run
~/FV-Copilot/sync-copilot.sh platform/service
```

### Legacy method:
```bash
~/FV-Copilot/link-repo.sh
```

This symlinks `repo/.copilot/` to `FV-Copilot/repos/{repo-name}/dot.copilot/`

## MCP Configuration

- **Location**: `copilot-cli/mcp-config.json`
- **Manage**: Use `/mcp` commands in Copilot CLI
- **Enable/Disable**: `/mcp enable <server>` or `/mcp disable <server>`
- **Package installs**: Stored in `copilot-cli/pkg/` (gitignored)

## Notes

- `.gitignore` excludes symlinked content and pkg files
- Commit only vault-specific content (scratch, thinking, etc.)
- Do not commit `repos/` or `copilot-cli/` - these are symlinks to external data
EOF

echo "🔧 Downloading helper scripts..."

# Download or create sync-copilot.sh
cat > sync-copilot.sh << 'SYNCEOF'
#!/bin/bash
# Bidirectional sync/merge script for .copilot directories
# Merges content from repo and vault, creates symlink
# Usage: ./sync-copilot.sh <relative-path> [--dry-run]

set -e

DRY_RUN=false
if [[ "$2" == "--dry-run" ]] || [[ "$1" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "🧪 DRY RUN MODE - No changes will be made"
    echo ""
fi

VAULT_PATH="$HOME/FV-Copilot"
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)

if [ -z "$REPO_ROOT" ]; then
    echo "❌ Error: Must run from within a git repository"
    exit 1
fi

# Handle path argument
if [ -z "$1" ] || [[ "$1" == "--dry-run" ]]; then
    RELATIVE_PATH="."
else
    RELATIVE_PATH="$1"
fi

# Normalize path
if [ "$RELATIVE_PATH" == "." ]; then
    RELATIVE_PATH=""
fi

REPO_NAME=$(basename "$REPO_ROOT")
VAULT_BASE="$VAULT_PATH/repos/$REPO_NAME"

if [ -z "$RELATIVE_PATH" ]; then
    VAULT_DOT_PATH="$VAULT_BASE/dot.copilot"
    REPO_DOT_PATH="$REPO_ROOT/.copilot"
    DISPLAY_PATH="(repo root)"
else
    VAULT_DOT_PATH="$VAULT_BASE/$RELATIVE_PATH/dot.copilot"
    REPO_DOT_PATH="$REPO_ROOT/$RELATIVE_PATH/.copilot"
    DISPLAY_PATH="$RELATIVE_PATH"
fi

echo "📁 Syncing: $DISPLAY_PATH"
echo "  Vault: $VAULT_DOT_PATH"
echo "  Repo:  $REPO_DOT_PATH"
echo ""

# Check what exists
VAULT_EXISTS=false
REPO_EXISTS=false
REPO_IS_SYMLINK=false

if [ -d "$VAULT_DOT_PATH" ]; then
    VAULT_EXISTS=true
    VAULT_FILES=$(find "$VAULT_DOT_PATH" -type f 2>/dev/null | wc -l | tr -d ' ')
    echo "✓ Vault has $VAULT_FILES files"
fi

if [ -L "$REPO_DOT_PATH" ]; then
    REPO_IS_SYMLINK=true
    echo "✓ Repo has symlink (already linked)"
elif [ -d "$REPO_DOT_PATH" ]; then
    REPO_EXISTS=true
    REPO_FILES=$(find "$REPO_DOT_PATH" -type f 2>/dev/null | wc -l | tr -d ' ')
    echo "✓ Repo has $REPO_FILES files"
fi

echo ""

# Plan actions
if [ "$REPO_IS_SYMLINK" = true ]; then
    echo "⏭️  Already symlinked - nothing to do"
    exit 0
fi

# Merge strategy
if [ "$VAULT_EXISTS" = true ] && [ "$REPO_EXISTS" = true ]; then
    echo "🔀 MERGE: Both locations have content"
    echo "   → Copy repo files to vault"
    echo "   → Remove repo directory"
    echo "   → Create symlink"
    
    if [ "$DRY_RUN" = false ]; then
        mkdir -p "$VAULT_DOT_PATH"
        cp -Rn "$REPO_DOT_PATH/"* "$VAULT_DOT_PATH/" 2>/dev/null || true
        cp -Rn "$REPO_DOT_PATH/".* "$VAULT_DOT_PATH/" 2>/dev/null || true
        rm -rf "$REPO_DOT_PATH"
        ln -s "$VAULT_DOT_PATH" "$REPO_DOT_PATH"
        echo "✅ Merged and linked"
    fi
    
elif [ "$REPO_EXISTS" = true ]; then
    echo "➡️  MOVE: Repo → Vault"
    echo "   → Move repo directory to vault as dot.copilot"
    echo "   → Create symlink"
    
    if [ "$DRY_RUN" = false ]; then
        mkdir -p "$(dirname "$VAULT_DOT_PATH")"
        mv "$REPO_DOT_PATH" "$VAULT_DOT_PATH"
        ln -s "$VAULT_DOT_PATH" "$REPO_DOT_PATH"
        echo "✅ Moved and linked"
    fi
    
elif [ "$VAULT_EXISTS" = true ]; then
    echo "⬅️  LINK: Vault → Repo"
    echo "   → Create symlink (vault already has content)"
    
    if [ "$DRY_RUN" = false ]; then
        ln -s "$VAULT_DOT_PATH" "$REPO_DOT_PATH"
        echo "✅ Linked"
    fi
    
else
    echo "➕ CREATE: New empty directory"
    echo "   → Create in vault"
    echo "   → Create symlink"
    
    if [ "$DRY_RUN" = false ]; then
        mkdir -p "$VAULT_DOT_PATH"
        ln -s "$VAULT_DOT_PATH" "$REPO_DOT_PATH"
        echo "✅ Created and linked"
    fi
fi

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "🧪 Dry run complete - no changes made"
    echo "   Run without --dry-run to apply changes"
fi
SYNCEOF

chmod +x sync-copilot.sh

# Create other helper scripts
cat > link-repo.sh << 'LINKEOF'
#!/bin/bash
# Link a repo's .copilot directory to the Obsidian vault
# Usage: Run from repo root OR pass repo path as argument

set -e

VAULT_PATH="$HOME/FV-Copilot"
REPO_PATH="${1:-$(pwd)}"

if [ ! -d "$REPO_PATH/.git" ]; then
    echo "Error: $REPO_PATH is not a git repository"
    exit 1
fi

REPO_NAME=$(basename "$REPO_PATH")
VAULT_REPO_PATH="$VAULT_PATH/repos/$REPO_NAME"
mkdir -p "$VAULT_REPO_PATH"

if [ -L "$REPO_PATH/.copilot" ]; then
    echo "Removing existing symlink at $REPO_PATH/.copilot"
    rm "$REPO_PATH/.copilot"
fi

if [ -d "$REPO_PATH/.copilot" ]; then
    echo "Moving existing .copilot directory to vault as dot.copilot..."
    mv "$REPO_PATH/.copilot" "$VAULT_REPO_PATH/dot.copilot"
fi

ln -s "$VAULT_REPO_PATH/dot.copilot" "$REPO_PATH/.copilot"

echo "✓ Linked $REPO_NAME/.copilot → FV-Copilot/repos/$REPO_NAME/dot.copilot"
echo "✓ Edit context in Obsidian, changes appear in repo instantly"
LINKEOF

chmod +x link-repo.sh

cat > setup-vault.sh << 'SETUPEOF'
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
SETUPEOF

chmod +x setup-vault.sh

echo ""
echo "✅ Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Open Obsidian: File → Open Vault → $VAULT_PATH"
echo "  2. Link your repos:"
echo "     cd ~/git/YourRepo"
echo "     $VAULT_PATH/sync-copilot.sh . --dry-run"
echo "     $VAULT_PATH/sync-copilot.sh ."
echo ""
echo "  3. (Optional) Initialize git in vault for personal notes:"
echo "     cd $VAULT_PATH"
echo "     git init"
echo "     git add scratch/ thinking/ _attachments/ *.md"
echo "     git commit -m 'Initial vault setup'"
echo ""
echo "📖 Read $VAULT_PATH/README.md for more info"
