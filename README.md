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
~/FV-Copilot/link-repo.sh
```

This creates `repo/.copilot/` → `vault/repos/{repo-name}/dot.copilot/`

**Note**: Hidden files (`.copilot`, `.claude`) are stored as `dot.copilot`, `dot.claude` in the vault to avoid Obsidian conflicts.

## Rules

- Files in `repos/{repo-name}/` are **real repo files** (via symlink)
- Files in `copilot-cli/` are **CLI config** (user-specific, not committed)
- Files outside these are **vault-only** (not in git)
- Edit in **any editor** - Obsidian provides visual benefits but isn't required
- Use scratch/thinking for iteration before promotion

## Commands

### Syncing Directories
- `./sync-copilot.sh <path> [--dry-run]` - Sync `.copilot` directories (private context)
- `./sync-github.sh [--dry-run]` - Sync `.github` directory (official instructions)

**Recommended workflow:**
1. Test with `--dry-run` first
2. Apply changes
3. Edit in vault (Obsidian or any editor)
4. Commit from repo when ready

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
