# Auto-Sync Methods

All sync operations are handled by `sync.py`.

## **Modes**

### Symlink Mode (Default)
- Creates per-file symlinks: `repo/.github/skills/x.md` → `vault/skills/x.md`
- Recursive: matches vault directory tree to repo directory tree
- Command: `python3 sync.py --mode symlink`

### Copy Mode
- One-way file copy from vault to repo
- Command: `python3 sync.py --mode copy`

### Watch Mode
- Continuous background sync using fswatch
- Re-syncs all repos on any vault file change
- Command: `python3 sync.py --watch`

---

## **Usage**

### One-Shot Sync
```bash
# Sync all repos for all agents
python3 sync.py --mode symlink

# Sync specific repo
python3 sync.py --mode symlink --repo ~/git/FV-Platform-Main

# Sync specific agent only
python3 sync.py --mode symlink --agent copilot
```

### Continuous Watch
```bash
python3 sync.py --watch
```
Runs continuously (Ctrl+C to stop). Requires `fswatch` (`brew install fswatch`).

### Other Operations
```bash
# Remove all symlinks
python3 sync.py --clean

# Merge real files back to vault, then re-symlink
python3 sync.py --merge

# Dry run (preview without changes)
python3 sync.py --mode symlink --dry-run
```

---

## **Installation**

### Option A: Manual (Terminal)
```bash
python3 ~/FV-Copilot/sync.py --watch
```
Foreground, see output, Ctrl+C to stop.

### Option B: Background Service (Recommended)
```bash
~/FV-Copilot/install-launchd-service.sh
```
Auto-starts on login, runs silently.

**Service commands:**
```bash
# Check status
launchctl list | grep obscura

# View logs
tail -f /tmp/obscura-watcher.log

# Stop
launchctl unload ~/Library/LaunchAgents/com.obscura.watcher.plist

# Start
launchctl load ~/Library/LaunchAgents/com.obscura.watcher.plist
```

---

## **What Syncs**

### Domain 1: In-Repo (Symlink Mode)
Vault content is symlinked into each repo's agent directories:
```
vault/skills/x.md        → repo/.github/skills/x.md (copilot)
vault/skills/x.md        → repo/.claude/skills/x.md (claude)
vault/instructions/y.md  → repo/.github/instructions/y.md
```

Recursive matching: if the vault has `repos/RepoName/platform/service/` and the repo has `platform/service/`, a nested `.github/` is created there too.

### Domain 2: System-Level
System-wide agent configs:
```
vault/skills/x.md        → ~/.github/skills/x.md
vault/instructions/y.md  → ~/.claude/instructions/y.md
```

---

## **System Cost**

**Idle:** 30MB RAM, 0% CPU (fswatch)
**Active:** Minimal I/O, ~100ms CPU spike during sync
**Disk:** Symlinks only (zero copy)

---

## **Quick Reference**

| Goal | Command |
|------|---------|
| Full sync | `python3 sync.py --mode symlink` |
| Watch mode | `python3 sync.py --watch` |
| Copy mode | `python3 sync.py --mode copy` |
| Clean all | `python3 sync.py --clean` |
| Merge & relink | `python3 sync.py --merge` |
| One repo | `python3 sync.py --mode symlink --repo ~/git/RepoName` |
| Background service | `install-launchd-service.sh` |
