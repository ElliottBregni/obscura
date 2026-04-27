# Agents/

Agent output and conversation surfaces.

Each agent gets its own subdirectory. Output files are named by date.

## Convention

```
Agents/
├── README.md              # this file
├── <agent-name>/
│   ├── YYYY-MM-DD.md      # daily session log
│   └── ...
└── inbox/
    └── *.md               # trigger notes for agents (human → agent)
```

## inbox/

Drop a note in `Agents/inbox/` to trigger an agent. The note filename or
frontmatter signals which agent should pick it up, depending on Obscura config.

## Reading agent output

```python
from _access import RepoAccess
vault = RepoAccess(".")
logs = vault.list_files("Agents/**/*.md")
latest = vault.read(logs[-1])
```
