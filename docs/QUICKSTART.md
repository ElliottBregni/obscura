# Quick Start for New Developers

## Setup

```bash
# Clone the vault
git clone https://github.com/your-org/FV-Copilot.git ~/FV-Copilot
```

## What You Get

- `~/FV-Copilot/` - Your vault (skills, instructions, docs)
- `sync.py` - Single Python script for all sync operations
- Documentation and examples

## Link Your First Repo

```bash
# Add repo to index
echo "~/git/FV-Platform-Main" >> ~/FV-Copilot/repos/INDEX.md

# Sync (creates per-file symlinks)
cd ~/FV-Copilot
python3 sync.py --repo ~/git/FV-Platform-Main --mode symlink
```

This creates:
- `repo/.github/skills/` → vault skill files (copilot)
- `repo/.claude/skills/` → vault skill files (claude)
- Nested `.github/` for modules (if vault content exists)

## Using the Vault

### Option 1: With Obsidian (Recommended)
1. **Open in Obsidian**
   ```
   File → Open Vault → ~/FV-Copilot
   ```
   - Visual file browser
   - Markdown preview
   - Graph view for navigation

2. **Edit content**
   - Navigate to `skills/` or `instructions/`
   - Edit any `.md` file
   - Changes appear instantly in all linked repos via symlinks

### Option 2: Without Obsidian
```bash
cd ~/FV-Copilot
code .          # VS Code
vim skills/python.md   # Vim
# Any editor works — it's just markdown files!
```

## Sync Commands

```bash
# Sync all repos for all agents
python3 sync.py --mode symlink

# Sync one repo, one agent
python3 sync.py --repo ~/git/MyRepo --agent copilot --mode symlink

# Preview without changes
python3 sync.py --mode symlink --dry-run

# Watch mode (auto-sync on file changes)
python3 sync.py --watch

# Remove all symlinks
python3 sync.py --clean
```

## Requirements

- Python 3.x (for sync.py)
- Git
- fswatch (optional, for watch mode: `brew install fswatch`)
- Obsidian (optional, https://obsidian.md)

Takes < 1 minute to set up!
