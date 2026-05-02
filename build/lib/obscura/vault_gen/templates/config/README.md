# ${name}

Configuration-as-code repo for an Obscura fleet. Obsidian opens this as a vault
for human-readable browsing and editing; git is the source of truth.

## Structure

```
${name}/
├── .obsidian/          # Obsidian config (git sync plugin pre-configured)
├── CLAUDE.md           # AI agent context
├── OBSCURA.md          # Obscura runtime context
├── _access/            # Programmatic access layer (stdlib Python)
├── agents/             # Agent manifests — YAML frontmatter + markdown description
├── env/                # Environment configurations
├── plugins/            # Plugin registry
├── policies/           # Operational policies (TOML)
├── tools/              # Tool configurations
└── workspaces/         # Workspace specifications (TOML)
```

## Agent manifests

Files in `agents/` follow this convention:

```markdown
---
name: agent-name
version: "1.0"
model: claude-opus-4-6
tools: [read, write, search]
---

Human-readable description of what this agent does.
```

## Workspaces

Files in `workspaces/` are TOML. See `workspaces/default.toml` for the schema.

## Policies

Files in `policies/` define operational constraints. See `policies/default.toml`.

## Usage

```bash
# Open in Obsidian
open -a Obsidian /path/to/${name}

# Programmatic access
python3 -c "
from _access import RepoAccess
repo = RepoAccess('.')
print(repo.list_files('agents/**/*.md'))
"
```
