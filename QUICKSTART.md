# Quick Start for New Developers

## One-Line Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/your-org/FV-Copilot/main/install.sh)
```

## Manual Install

```bash
# Clone the installer
git clone https://github.com/your-org/FV-Copilot.git ~/FV-Copilot

# Run installer
cd ~/FV-Copilot
./install.sh
```

## What Gets Installed

- `~/FV-Copilot/` - Your Obsidian vault
- Symlink to `~/.copilot` (Copilot CLI config)
- Helper scripts for syncing repos
- Documentation and examples

## After Install

### Option 1: With Obsidian (Recommended)
1. **Open in Obsidian**
   ```
   File → Open Vault → ~/FV-Copilot
   ```
   - Visual file browser
   - Markdown preview
   - Backlinks and graph view

### Option 2: Without Obsidian
1. **Use any editor**
   ```bash
   cd ~/FV-Copilot
   code .          # VS Code
   vim README.md   # Vim
   # Any editor works—it's just markdown files!
   ```

### Link Your First Repo (Either Way)
   ```bash
   cd ~/git/YourRepo
   ~/FV-Copilot/sync-copilot.sh . --dry-run  # Preview
   ~/FV-Copilot/sync-copilot.sh .            # Apply
   ```

3. **Edit in Obsidian, commit from repo when ready**

## Sharing the Installer

Send teammates:
```
bash <(curl -fsSL https://your-company-url/install.sh)
```

Or add to your team's onboarding docs!

## Requirements

- Obsidian (https://obsidian.md)
- GitHub Copilot CLI
- Git

Takes < 1 minute to install! 🚀
