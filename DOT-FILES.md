# Obsidian Hidden Files Convention

## Problem
Obsidian can have conflicts with hidden files (files starting with `.`). This affects `.copilot`, `.claude`, and other dotfiles.

## Solution
In the vault, all hidden files are renamed using the `dot.` prefix:
- `.copilot` → `dot.copilot`
- `.claude` → `dot.claude`
- `.anything` → `dot.anything`

## Implementation

### In Vault (~/FV-Copilot/repos/)
```
FV-Platform-Main/
  ├── dot.claude/                    (visible in Obsidian)
  └── platform/
      └── partview_core/
          ├── dot.claude/            (visible in Obsidian)
          └── partview_service/
              └── dot.claude/        (visible in Obsidian)
```

### In Repo (~/git/FV-Platform-Main/)
```
FV-Platform-Main/
  ├── .copilot → vault/dot.claude    (symlink)
  └── platform/
      └── partview_core/
          ├── .copilot → vault/dot.claude    (symlink)
          └── partview_service/
              └── .copilot → vault/dot.claude    (symlink)
```

## Scripts Updated
- `link-repo.sh` - Creates `dot.copilot` in vault, symlinks as `.copilot` in repo
- `unlink-repo.sh` - Handles `dot.copilot` naming convention

## Manual Conversion
For new hidden dirs in repo:
```bash
# 1. Move to vault with dot. prefix
mv repo/.hidden ~/FV-Copilot/repos/RepoName/dot.hidden

# 2. Symlink back
ln -s ~/FV-Copilot/repos/RepoName/dot.hidden repo/.hidden
```

## Benefits
- ✓ No Obsidian conflicts
- ✓ Hidden files visible in Obsidian sidebar
- ✓ Repos see correct dotfile names
- ✓ Seamless editing in both environments
