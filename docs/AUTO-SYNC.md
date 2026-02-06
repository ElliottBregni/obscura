# Auto-Sync Methods

The `watch-and-sync.sh` script supports multiple modes.

## **Modes**

### Symlink Mode
- Creates symlinks: `repo/.github` → `vault/repos/RepoName/`
- Lightweight, one-way (vault = source)
- Command: `~/FV-Copilot/watch-and-sync.sh --mode symlink`

### Watch Mode
- Bi-directional file sync via fswatch
- `~/.copilot` ↔ `~/FV-Copilot/copilot-cli/`
- Command: `~/FV-Copilot/watch-and-sync.sh --mode watch`

### Both Mode (Default)
- Symlinks for repos + Watch for copilot-cli
- Command: `~/FV-Copilot/watch-and-sync.sh`

---

## **Usage**

### Setup Symlinks
```bash
~/FV-Copilot/watch-and-sync.sh --mode symlink
```
Creates symlinks, exits.

### Start Watcher
```bash
~/FV-Copilot/watch-and-sync.sh --mode watch
```
Runs continuously (Ctrl+C to stop).

### Both (Recommended)
```bash
~/FV-Copilot/watch-and-sync.sh
```

### Specific Repo
```bash
~/FV-Copilot/watch-and-sync.sh --mode symlink --repo FV-Platform-Main
```

---

## **Installation**

### Option A: Manual (Terminal)
```bash
~/FV-Copilot/watch-and-sync.sh --mode both
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
launchctl list | grep fv-copilot

# View logs
tail -f /tmp/fv-copilot-watcher.log

# Stop
launchctl unload ~/Library/LaunchAgents/com.fv-copilot.watch-and-sync.plist

# Start
launchctl load ~/Library/LaunchAgents/com.fv-copilot.watch-and-sync.plist
```

---

## **What Syncs**

### Repos (Symlink Mode)
```
~/FV-Copilot/repos/FV-Platform-Main/
  ├── copilot-instructions.md
  └── platform/partview_core/partview_service/
      └── copilot-instructions.md
```
Creates symlinks at:
```
~/git/FV-Platform-Main/.github → vault/repos/FV-Platform-Main/
~/git/FV-Platform-Main/platform/.../service/.github → vault/.../service/
```

### Copilot-CLI (Watch Mode)
```
~/.copilot/ ↔ ~/FV-Copilot/copilot-cli/
├── agent.json
├── config.json
├── mcp-config.json
├── logs/
├── session-state/
└── ... (all files)
```

---

## **System Cost**

**Idle:** 30MB RAM, 0% CPU
**Active:** Minimal I/O, ~100ms CPU spike during copy
**Disk:** copilot-cli/ mirrors ~/.copilot

---

## **Quick Reference**

| Goal | Command |
|------|---------|
| Full setup | `~/FV-Copilot/watch-and-sync.sh` |
| Watch only | `--mode watch` |
| Symlinks only | `--mode symlink` |
| Background service | `install-launchd-service.sh` |
| One repo | `--mode symlink --repo RepoName` |
