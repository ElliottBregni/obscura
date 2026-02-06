# FV-Copilot Installation Guide

## Prerequisites

### Required
- **Git** - For version control
- **Python 3.x** - For sync.py (ships with macOS)

### Optional (Recommended)
- **fswatch** - For watch mode (`brew install fswatch`)
- **Obsidian** - Download from https://obsidian.md
  - Provides visual navigation, backlinks, and graph view
  - **Not required** - You can edit markdown files in any editor (VS Code, vim, etc.)
  - The vault is just a folder with `.md` files and symlinks

### Recommended MCP Servers
The vault includes MCP configuration for extended AI agent functionality.

Current MCP config: `mcp-config.json`

#### Installed MCP Servers
- **github-mcp-server** - GitHub integration (repos, PRs, issues, actions)

#### To Add More MCP Servers
Manually edit `mcp-config.json` or use your agent's MCP configuration commands.

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

1. Clone or copy `~/FV-Copilot`
2. Open in Obsidian (optional) or any editor
3. Add repos to `repos/INDEX.md`
4. Run sync: `python3 sync.py --mode symlink`

## Adding New Repos

```bash
# Add to index
echo "~/git/YourRepo" >> repos/INDEX.md

# Sync (creates per-file symlinks)
python3 sync.py --repo ~/git/YourRepo --mode symlink

# Or dry-run first
python3 sync.py --repo ~/git/YourRepo --mode symlink --dry-run
```

This creates per-file symlinks:
- `repo/.github/skills/x.md` → `vault/skills/x.md`
- `repo/.claude/skills/x.md` → `vault/skills/x.md`
- Nested `.github/` for modules matching vault structure

**File Organization:**
```
.github/
├── copilot-instructions.md    # Main instructions (from vault repo mirror)
├── instructions/               # Instruction files
│   └── *.md
└── skills/                     # Skill files
    └── *.md
```

## MCP Configuration

- **Location**: `mcp-config.json` (vault root)
- **Template**: `mcp-config.template.json`

## Notes

- `.gitignore` excludes symlinked content and temporary files
- Commit only vault-specific content
- `repos/` contains repo mirror directories (vault content that maps into repos)
