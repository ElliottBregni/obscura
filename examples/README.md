# Examples

## Clean Agent + Instance Template

Use `agent_instance_template.py` as the starter for new Obscura usage.

### Managed agent mode (recommended for workflows)

```bash
uv run python examples/agent_instance_template.py \
  --mode agent \
  --backend claude \
  --prompt "Use tools if needed and summarize the repo in 3 bullets."
```

### Direct Obscura instance mode

```bash
uv run python examples/agent_instance_template.py \
  --mode instance \
  --backend openai \
  --model gpt-4o \
  --prompt "Explain what this project does."
```

## Full Agent Builder Interface (skills + MCP + A2A + tools)

Use `full_agent_builder_template.py` when you need a full configurable builder.

### Example: load skills + MCP servers + A2A remotes

```bash
uv run python examples/full_agent_builder_template.py \
  --backend claude \
  --name orchestrator \
  --mode loop \
  --skill-file .obscura/skills/research.md \
  --skills-dir .obscura/skills \
  --mcp-discover \
  --mcp-config .obscura/mcp/servers.json \
  --mcp-server-names github,filesystem,playwright \
  --mcp-stdio "localfs:npx:-y,@modelcontextprotocol/server-filesystem,." \
  --a2a-urls "http://localhost:9001,http://localhost:9002" \
  --enable-system-tools \
  --prompt "Use skills and tools to inspect the repo and produce an action plan."
```

### Example: custom APER mode on same builder

```bash
uv run python examples/full_agent_builder_template.py \
  --backend claude \
  --mode aper \
  --aper-max-turns 10 \
  --aper-execute-template "Goal: {goal}\nPlan: {plan}\nExecute deeply using tools." \
  --mcp-discover \
  --mcp-server-names github,filesystem \
  --enable-system-tools \
  --prompt "Investigate this repo and propose a production hardening plan."
```

### Example: run two APER profiles (`fast` + `deep`)

```bash
uv run python examples/full_agent_builder_two_profiles.py \
  --backend claude \
  --profile both \
  --mcp-discover \
  --mcp-server-names github,filesystem \
  --prompt "Analyze the codebase and propose an execution plan."
```
