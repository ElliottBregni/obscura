# Registered AI Agents

Active agents for context management. Each agent can have universal and agent-specific skills/instructions.

## Active Agents

- copilot
- claude

## Directory Structure

For each agent, you can create:
- `skills.{agent}/` - Agent-specific skills (overrides universal `skills/`)
- `instructions.{agent}/` - Agent-specific instructions (overrides universal `instructions/`)
- `{agent}-cli/` - Synced CLI state from `~/.{agent}`

Universal directories (no agent suffix) are shared across all agents:
- `skills/` - Universal skills (all agents)
- `instructions/` - Universal instructions (all agents)
- `docs/` - Universal documentation (all agents)

## Override Behavior

When an agent-specific file has the same name as a universal file, the agent-specific version wins.

Example:
- `skills/python.md` (universal)
- `skills.copilot/python.md` (Copilot-specific, overrides universal)
- Copilot sees: `skills.copilot/python.md`
- Claude sees: `skills/python.md` (universal)

## Adding a New Agent

1. Add agent name to the list above
2. Create `skills.{agent}/` directory (optional)
3. Create `instructions.{agent}/` directory (optional)
4. Run sync script with `--agent {agent}` flag
5. Install git hooks in target repos
