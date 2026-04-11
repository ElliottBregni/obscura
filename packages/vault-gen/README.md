# vault-gen

Scaffolds Obsidian-compatible, git-backed repos for the [Obscura](../obscura) AI agent runtime.

## Two repo types

**`config`** — Configuration-as-code. Agent manifests, workspace specs, policies, tool configs, plugin registries. Obsidian opens it as a vault for human-readable config browsing.

**`vault`** — Interactive Obsidian vault. Bidirectional sync between Obscura and Obsidian: notes feed into agent memory, agent output surfaces as notes. Daily notes, templates, agent logs, decision records.

## Install

```bash
uv tool install .
```

Or run directly:

```bash
uv run vault-gen --help
```

## Usage

```bash
# Scaffold a new vault
vault-gen init my-workspace --type vault --path ~/vaults
vault-gen init fleet-config --type config --path ~/config-repos

# List generated repos
vault-gen list

# Link a repo into an Obscura instance
vault-gen link ~/vaults/my-workspace --obscura-path ~/dev/obscura

# Show status
vault-gen status my-workspace
```

## Generated repo structure

### config type
```
fleet-config/
├── .gitignore
├── .obsidian/          # Pre-configured for git sync
├── CLAUDE.md
├── OBSCURA.md
├── README.md
├── _access/            # Programmatic access layer
├── agents/             # Agent manifests (YAML frontmatter .md)
├── workspaces/         # Workspace specs (.toml)
├── policies/           # Operational policies (.toml)
├── tools/              # Tool configurations
├── plugins/            # Plugin registry
└── env/                # Environment configs
```

### vault type
```
my-workspace/
├── .gitignore
├── .obsidian/          # Full Obsidian config (daily notes, templates, git)
├── CLAUDE.md
├── OBSCURA.md
├── README.md
├── _access/            # Programmatic access layer
├── Agents/             # Agent output / conversation surfaces
├── Logs/               # KAIROS dream logs, fleet summaries
├── Memory/             # Vector memory snapshots as notes
├── Projects/           # Project-scoped notes
└── Templates/          # Note templates (daily, agent log, meeting, ADR)
```

## Access layer

Each generated repo includes `_access/` — a stdlib-only Python module for programmatic access:

```python
from _access import RepoAccess

repo = RepoAccess(".")
repo.read("Agents/fleet-summary.md")
repo.write("Memory/snapshot-2026.md", content, commit_msg="mem: update snapshot")
repo.search("KAIROS")
repo.history(n=10)
repo.list_files("**/*.md")
repo.sync()
```

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check src/
uv run pyright
```
