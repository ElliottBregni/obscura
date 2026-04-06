# Obscura

Multi-backend AI agent runtime — CLI, REST API, Web UI, and MCP server. Supports GitHub Copilot, Claude, OpenAI, and local LLMs with 100+ tools, vector memory, multi-agent orchestration, and 49 plugin integrations.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Node.js ≥18 (for the Web UI only)
- Qdrant (optional — falls back to SQLite for vector memory)

## Install

```bash
git clone <repo-url>
cd obscura

# Install Python dependencies
uv sync

# Copy and configure environment
cp .env.example .env
# Edit .env — add your LLM backend credentials (see Configuration below)

# Run the CLI
obscura
```

### Development install

```bash
uv sync --group dev
```

### Voice mode

```bash
uv sync --extra voice

# macOS: also install SoX and FFmpeg
brew install sox ffmpeg
```

## Quick Start

```bash
# Interactive REPL (default backend: copilot)
obscura

# Choose a backend
obscura -b claude
obscura -b copilot
obscura -b codex

# Specify a model
obscura -b claude -m claude-sonnet-4-5-20250929

# Single-shot prompt (no REPL)
obscura -p "summarize this file"
obscura "explain this code"
```

## Configuration

### Backend credentials

| Backend | Environment Variable(s) |
|---------|------------------------|
| Copilot | `GITHUB_TOKEN`, `GH_TOKEN`, or `gh auth login` |
| Claude | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Moonshot | `MOONSHOT_API_KEY` |
| LocalLLM | `OBSCURA_LOCALLLM_BASE_URL` (default: `http://localhost:1234/v1`) |

### Key environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSCURA_PORT` | `8080` | API server port |
| `OBSCURA_AUTH_ENABLED` | `false` | Enforce auth on `/api/` routes |
| `OBSCURA_API_KEYS` | — | `token:user:scope1,scope2` (`;` separates multiple) |
| `OBSCURA_VECTOR_BACKEND` | `qdrant` | `qdrant` or `none` |
| `OBSCURA_QDRANT_URL` | — | Qdrant server URL |
| `OBSCURA_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `OBSCURA_OUTPUT_MODE` | `plain` | `plain` or `json` |
| `OBSCURA_UNDERCOVER` | `1` | Strip AI attribution from commits/PRs |

See [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) for the full reference.

## Architecture

| Subsystem | Description |
|-----------|-------------|
| **Supervisor** | Single-writer session coordinator. Freezes tools, prompt, and memory during context assembly. SQLite-backed, 7-state FSM, 16 modules. |
| **Plugin System** | 49 built-in plugins loaded from TOML manifests (`obscura/plugins/builtins/`). Plugins expose tools, capabilities, and credentials. |
| **Kairos** | Autonomous background goal runtime. Runs long-horizon goals asynchronously. |
| **A2A** | Agent-to-agent protocol bridge. JSON-RPC and gRPC transports. |
| **Channels** | Messaging integrations (Telegram, WhatsApp, Slack). Routes inbound messages to the agent runtime. |
| **MCP Server** | Exposes all Obscura tools to Claude Code / Claude Desktop via stdio MCP. |
| **Web UI** | React 18 admin dashboard at `localhost:5173`. Sessions, memory, agents, workflows, approvals. |

### Package layout

```
obscura/
├── agent/            # Agent loop, spawn, delegation
├── cli/              # Click CLI + 85 slash commands
├── core/             # Supervisor, Kairos, config, types
├── integrations/
│   ├── a2a/          # Agent-to-Agent protocol
│   ├── mcp/          # Model Context Protocol
│   └── messaging/    # Telegram, WhatsApp, Slack
├── memory/           # Key-value memory
├── vector_memory/    # Qdrant + SQLite vector backends
├── plugins/          # Plugin system + 49 built-in plugins
├── providers/        # LLM backends (Copilot, Claude, OpenAI, LocalLLM)
├── routes/           # FastAPI routers (20 routers)
├── server/           # FastAPI app factory
├── skills/           # Skill manifest loading
├── tools/            # Tool registry, system tools, policy
└── tui/              # Rich-based TUI
```

## Web UI

```bash
cd web-ui
npm install
npm run dev     # http://localhost:5173
```

The Web UI requires the Obscura API to be running on port 8080. See [SETUP.md](SETUP.md) for full dev setup including auth and Qdrant.

## API Server

```bash
# Start the API server
python -m obscura.server

# Or with make
make dev-up
```

The API is available at `http://localhost:8080`. All routes are prefixed `/api/v1/`.

## MCP Server (Claude Code / Claude Desktop)

The `obscura-mcp` command exposes all Obscura tools via stdio MCP:

```bash
obscura-mcp --transport stdio
```

Add to `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "obscura": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "obscura.mcp_server", "--transport", "stdio"],
      "env": {
        "OBSCURA_API_KEYS": "your-api-key:admin:agent:read,agent:copilot",
        "OBSCURA_PORT": "8080"
      }
    }
  }
}
```

Restart Claude Code after changing this config.

## Plugins (49)

Plugins are TOML manifests in `obscura/plugins/builtins/`. Each plugin declares the tools it exposes and the credentials it needs. Active on startup; authentication is per-plugin.

Notable plugins: `alphavantage`, `browserless`, `censys`, `coingecko`, `datadog`, `docker-engine`, `duckdb`, `flightaware`, `github-graphql`, `grafana`, `huggingface`, `jira`, `kubernetes-api`, `m365`, `nats`, `notion`, `playwright`, `polygon`, `prometheus`, `ripgrep`, `sec-edgar`, `shodan`, `websearch`, `x-twitter`, and more.

Skills plugins: `skill-pytight`, `skill-red-team`, `skill-persuasion`, `skill-authority`, `skill-defense`, `skill-rapport`, `skill-steering`.

## Agent Manifests

Agents are defined as `*.agent.md` files with YAML frontmatter:

```markdown
---
name: reviewer
provider: claude
model_id: claude-sonnet-4-5-20250929
tools:
  - read_text_file
  - grep_files
  - git_diff
can_delegate: true
delegate_allowlist:
  - writer
max_turns: 25
agent_type: loop
tags:
  - code-review
permissions:
  allow:
    - "read_*"
    - "grep_*"
  deny:
    - "remove_path"
    - "run_shell"
---

You are a code reviewer. Analyze code changes for bugs,
style issues, and security concerns.
```

Place agent manifests in `~/.obscura/agents/` or the project `agents/` directory.

### Key manifest fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Agent name |
| `provider` | string | `copilot` | `copilot`, `claude`, `openai`, `localllm`, `moonshot` |
| `model_id` | string | null | Specific model to use |
| `tools` | list | `[]` | Tools the agent can use |
| `permissions.allow` | list | `[]` | Glob patterns for allowed tools |
| `permissions.deny` | list | `[]` | Glob patterns for denied tools |
| `can_delegate` | bool | `false` | Allow agent to delegate tasks |
| `delegate_allowlist` | list | `[]` | Agents this agent can delegate to |
| `agent_type` | string | `loop` | `loop`, `daemon`, or `aper` |
| `max_turns` | int | `25` | Max conversation turns |

## Slash Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/backend <copilot\|claude\|codex>` | Switch LLM backend |
| `/model <model-id>` | Switch model |
| `/mode <ask\|plan\|code>` | Switch interaction mode |
| `/tools <on\|off>` | Toggle tool execution |
| `/confirm <on\|off>` | Toggle tool approval prompts |
| `/agent <spawn\|list\|stop\|run>` | Manage agents |
| `/fleet <spawn\|status\|run\|delegate\|stop>` | Multi-agent fleet |
| `/memory <stats\|search\|clear>` | Vector memory operations |
| `/session <list\|new>` | Session management |
| `/mcp <discover\|list\|select\|install>` | MCP server management |
| `/compact [n]` | Summarize history to free tokens |
| `/context` | Show context window usage |
| `/undercover <on\|off\|auto>` | Toggle undercover mode |
| `/quit` | Exit the REPL |

## Vector Memory

Vector memory runs automatically in every session:

1. **Session start** — Loads recent memories into the system prompt
2. **Pre-message** — Searches for relevant memories before each user message (RAG)
3. **Post-message** — Auto-saves conversation turns in background threads
4. **Tools** — LLM can call `store_memory`, `recall_memory`, `semantic_search`, `store_searchable`

Memory is stored in `~/.obscura/`:

```
~/.obscura/
  events.db          # Session event store (SQLite)
  memory/            # Per-user key-value memory
  vector_memory/     # Semantic vector store
  mcp/               # MCP server configs
  logs/              # Trace logs (JSONL)
```

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSCURA_VECTOR_MEMORY` | `on` | Enable/disable vector memory |
| `QDRANT_URL` | — | Qdrant URL (falls back to SQLite if unset) |
| `QDRANT_API_KEY` | — | Qdrant API key |

## Development

```bash
# Unit tests (fast, no server required)
pytest tests/ -v -m "not e2e"

# With coverage
pytest tests/ --cov=obscura --cov-report=term-missing

# Type checking
pyright

# Linting
ruff check .
ruff format --check .

# Or via make
make lint
make typecheck
make test
```

## Docker

```bash
# Dev stack (API + Qdrant)
make dev-up

# Staging
make staging-up

# Production
make prod-up

# Tear down
make dev-down
```

See [SETUP.md](SETUP.md) for the full local and production setup guide including Nginx config, env templates, and MCP configuration.

## Docs

| Document | Description |
|----------|-------------|
| [SETUP.md](SETUP.md) | Full local dev and production setup guide |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture deep-dive |
| [docs/kairos.md](docs/kairos.md) | Kairos autonomous goal runtime |
| [docs/web-ui.md](docs/web-ui.md) | Web UI reference |
| [docs/PLUGIN_ARCHITECTURE.md](docs/PLUGIN_ARCHITECTURE.md) | Plugin system internals |
| [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) | All environment variables |
| [AGENTS.md](AGENTS.md) | GitNexus code intelligence guide |
| [OBSCURA.md](OBSCURA.md) | Operating manual (synced to CLAUDE.md on startup) |

## License

MIT
