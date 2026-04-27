# ${name}

Interactive Obsidian vault with bidirectional Obscura sync.

Notes written here feed into Obscura's memory system. Obscura agent output
surfaces here as notes. Git is the transport layer.

## Structure

```
${name}/
├── .obsidian/       # Obsidian config (daily notes, templates, git sync)
├── CLAUDE.md        # AI agent context
├── OBSCURA.md       # Obscura runtime context
├── _access/         # Programmatic access layer
├── Agents/          # Agent output and conversation surfaces
├── Logs/            # KAIROS dream logs, fleet summaries, daily notes
├── Memory/          # Vector memory snapshots surfaced as notes
├── Projects/        # Project-scoped notes and working documents
└── Templates/       # Note templates (Obsidian core templates)
```

## Daily workflow

1. Open this folder in Obsidian as your vault.
2. Use `Ctrl/Cmd+P → Open today's daily note` — it lands in `Logs/`.
3. Agent output appears in `Agents/` — review, edit, link.
4. Memory snapshots appear in `Memory/` — treat as read-mostly.

## Sync

Obsidian Git plugin handles pull/push on a timer. For manual sync:

```bash
cd /path/to/${name}
git pull --rebase && git push
```

Or via the access layer:

```python
from _access import RepoAccess
RepoAccess(".").sync()
```

## Templates

| Template | Use for |
|----------|---------|
| `Templates/Daily Note.md` | Daily notes (auto-used by Obsidian daily notes plugin) |
| `Templates/Agent Log.md` | Agent session logs |
| `Templates/Meeting Note.md` | Meeting notes |
| `Templates/Decision Record.md` | Architecture / decision records |
