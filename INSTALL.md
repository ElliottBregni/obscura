# FV-Copilot Installation Guide

## Prerequisites

## Prerequisites

### Required
- **Git** - For version control
- **GitHub Copilot CLI** - Install with:
  ```bash
  brew install copilot-cli
  # or
  npm install -g @github/copilot
  ```

### Optional (Recommended)
- **Obsidian** - Download from https://obsidian.md
  - Provides visual navigation, backlinks, and graph view
  - **Not required** - You can edit markdown files in any editor (VS Code, vim, etc.)
  - The vault is just a folder with `.md` files and symlinks

### Recommended MCP Servers
The Copilot CLI uses Model Context Protocol (MCP) servers for extended functionality.

Current MCP config location: `copilot-cli/mcp-config.json` (symlinked to `~/.copilot/mcp-config.json`)

#### Installed MCP Servers
- **github-mcp-server** - GitHub integration (repos, PRs, issues, actions)

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
