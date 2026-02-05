# FV-Copilot

**Obsidian-powered context management for Copilot CLI and code repositories**

Centralize LLM skills, architectural notes, and development context across all your repos in a single Obsidian vault—without polluting your code repos during iteration.

## 🚀 Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/your-org/FV-Copilot/main/install.sh | bash
```

Or manual install:
```bash
git clone https://github.com/your-org/FV-Copilot.git ~/FV-Copilot
cd ~/FV-Copilot
chmod +x install.sh
./install.sh
```

## ✨ Features

- 📝 **Editor agnostic** - Use Obsidian, VS Code, vim, or any markdown editor
- 🔗 **Symlink-based architecture** - Edit anywhere, changes instantly appear in repos
- 🔄 **Bidirectional sync** - Merge content from repo and vault automatically
- 📁 **Multi-repo support** - Manage context for unlimited repos in one vault
- 🎯 **LLM-optimized** - Direct access for Copilot CLI, Claude, Cursor, etc.
- 🔒 **No repo pollution** - Hidden files stored as `dot.copilot` in vault
- 🛠️ **MCP integration** - View/edit Copilot CLI MCP config in Obsidian

## 📖 Usage

### Link a repository

```bash
cd ~/git/YourRepo
~/FV-Copilot/sync-copilot.sh . --dry-run  # Test first
~/FV-Copilot/sync-copilot.sh .            # Apply
```

### Link nested modules

```bash
~/FV-Copilot/sync-copilot.sh platform/service --dry-run
~/FV-Copilot/sync-copilot.sh platform/service
```

### Structure

```
~/FV-Copilot/
├── repos/
│   └── YourRepo/
│       └── dot.copilot/          # Repo context (symlinked)
├── copilot-cli/                  # CLI config (symlinked to ~/.copilot)
├── scratch/                      # Private notes
├── thinking/                     # Working drafts
└── _attachments/                 # Vault-only files
```

## 🎓 How It Works

1. **Vault stores everything** - All `.copilot` content lives in `~/FV-Copilot/repos/`
2. **Repos use symlinks** - `repo/.copilot` → `vault/repos/RepoName/dot.copilot/`
3. **Obsidian sees folders** - Hidden files appear as `dot.copilot` (visible in sidebar)
4. **LLMs see real files** - Symlinks are transparent to Copilot CLI

## 🔧 Prerequisites

- **Git** - Required for repo management
- **GitHub Copilot CLI** - Required for LLM context (install: `brew install copilot-cli`)
- **Obsidian** - *Optional but recommended* for visual navigation and editing
  - Download from https://obsidian.md
  - **Without Obsidian**: Edit markdown files in any editor (VS Code, vim, etc.)
  - The vault is just a folder structure—use what you prefer!

## 📚 Documentation

- [INSTALL.md](./INSTALL.md) - Full installation guide
- [README.md](./README.md) - Vault structure and commands
- [DOT-FILES.md](./DOT-FILES.md) - Hidden file convention explained
- [TEST-RESULTS.md](./TEST-RESULTS.md) - Script test coverage

## 🤝 Contributing

This is a personal vault structure template. Feel free to fork and adapt for your team!

## 📝 License

MIT
