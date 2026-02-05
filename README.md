# FV-Copilot Vault

Obsidian vault for managing GitHub Copilot context across repositories.

## 📂 Structure

- **repos/** - Repository contexts (symlinked to `.github` in each repo)
- **docs/** - [Vault documentation](docs/)
- **copilot-cli/** - CLI config (symlinked to `~/.copilot`)
- **scratch/** - Private notes
- **thinking/** - Working drafts

## 🚀 Quick Start

```bash
# Link a repository
cd ~/git/YourRepo
~/FV-Copilot/sync-github.sh

# Edit in Obsidian or any editor
```

## 📚 Documentation

- [Installation Guide](docs/INSTALL.md)
- [Quick Start](docs/QUICKSTART.md)  
- [GitHub Integration](docs/GITHUB-INTEGRATION.md)
- [No Obsidian?](docs/NO-OBSIDIAN.md)

See [docs/](docs/) folder for all guides.
