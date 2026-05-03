# Obscura

Multi-backend AI agent runtime — CLI, REST API, Web UI, and MCP server. Supports GitHub Copilot, Claude, OpenAI, and local LLMs with 100+ tools, vector memory, multi-agent orchestration, and 49 plugin integrations.

## Getting Started

### 1. Prerequisites

- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **Node.js ≥18** — only needed if you want the Web UI
- **Qdrant** — optional, only needed for vector memory (falls back to SQLite automatically)

### 2. Clone and install

```bash
git clone <repo-url>
cd obscura
uv sync                 # installs deps into .venv automatically
```

For development (adds pytest, pyright, ruff):

```bash
uv sync --group dev
```

### 3. Set up credentials

The CLI auto-loads `.env` from the project root on startup — no sourcing required.

```bash
cp .env.example .env
# Open .env and add credentials for whichever backend(s) you want to use
```

At minimum you need one of:

```bash
# GitHub Copilot (default backend — also works via `gh auth login`)
GITHUB_TOKEN=ghp_...

# OR Anthropic Claude
ANTHROPIC_API_KEY=sk-ant-...

# OR OpenAI
OPENAI_API_KEY=sk-...
```

### 4. Run it

```bash
uv run obscura           # starts the interactive REPL
```

Or if the venv is already activated (`source .venv/bin/activate`):

```bash
obscura                  # default backend: copilot
obscura -b claude        # use Claude
obscura -b codex         # use OpenAI Codex
obscura -b claude -m claude-sonnet-4-5-20250929  # specific model
obscura "explain this code"                       # single-shot, no REPL
```

`.env` load order (later entries do **not** override earlier ones):

```
shell environment  >  ~/.obscura/.env  >  .obscura/.env  >  ./.env
```

### Voice mode

```bash
uv sync --extra voice
brew install sox ffmpeg   # macOS only
```

## Configuration

### Backend credentials

| Backend | Environment Variable |
|---------|---------------------|
| Copilot | `GITHUB_TOKEN` or `GH_TOKEN` (or `gh auth login`) |
| Claude | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Moonshot | `MOONSHOT_API_KEY` |
| LocalLLM | `OBSCURA_LOCALLLM_BASE_URL` (default: `http://localhost:1234/v1`) |

### Key environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSCURA_PORT` | `8080` | API server port |
| `OBSCURA_API_KEYS` | — | `token:user:scope1,scope2` (`;` separates multiple) |
| `OBSCURA_VECTOR_BACKEND` | `qdrant` | `qdrant` or `none` |
| `OBSCURA_QDRANT_URL` | — | Qdrant server URL |
| `OBSCURA_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `OBSCURA_UNDERCOVER` | `1` | Strip AI attribution from commits/PRs |

See [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) for the full reference.

## Authentication

Obscura's server accepts two credential types on `/api/*`, `/mcp/*`, and `/a2a/*`:

1. **Supabase bearer tokens** — issued to users who sign in through the Web UI (primary path)
2. **API keys** — long-lived tokens for scripts, CI, and MCP clients (`OBSCURA_API_KEYS`)

### Sign in with GitHub (end users)

If the operator has configured GitHub OAuth (see below), signing in is one click:

1. Open the Web UI (`http://localhost:5173` for dev, or your deployed URL)
2. Click **Sign in with GitHub**
3. Authorize the OAuth app on GitHub
4. You're redirected back, authenticated via Supabase

The access token is held in `sessionStorage` under `obscura.supabase.auth` and sent as `Authorization: Bearer <token>` on every API call. Tokens expire after 15 minutes (`jwt_exp=900`) and refresh automatically while the tab is open.

Your role in Obscura comes from `app_metadata.roles` on your Supabase user — set it from the Supabase dashboard (Auth → Users → your user → edit `app_metadata`). Valid roles: `admin`, `operator`, `agent:read`, `agent:claude`, `agent:copilot`, etc. — see `obscura/auth/models.py` for the full list. New GitHub sign-ups default to `agent:read`.

### Configure GitHub OAuth (operators)

One-time setup when standing up a new Supabase project.

**1. Create a GitHub OAuth App** — https://github.com/settings/developers → **New OAuth App**

| Field | Value |
|-------|-------|
| Application name | Obscura (or your deployment name) |
| Homepage URL | `https://your-domain` (or `http://localhost:5173` for dev) |
| Authorization callback URL | `https://<project-ref>.supabase.co/auth/v1/callback` |

Copy the **Client ID**, generate a **Client Secret**.

**2. Enable GitHub in Supabase** — `https://supabase.com/dashboard/project/<project-ref>/auth/providers`

Toggle **GitHub** on, paste the Client ID and Client Secret, Save.

**3. Point Obscura at your Supabase project** — in `.env`:

```bash
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_ANON_KEY=<public anon key from Supabase API settings>
SUPABASE_JWKS_URL=https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json
```

Auth is always enforced on `/api/*`, `/mcp/*`, and `/a2a/*`; there is no off-switch.

Obscura validates tokens by fetching the JWKS on first use — no shared secret to paste or rotate. If your project is still on legacy HS256, either migrate to asymmetric keys (Dashboard → Project Settings → JWT Signing Keys → Rotate) or set `SUPABASE_JWT_SECRET` instead of `SUPABASE_JWKS_URL`.

**4. Rotating the GitHub secret** — generate a new one in the GitHub OAuth App, paste into the Supabase dashboard (same page as step 2), Save. Then revoke the old secret on GitHub. No Obscura restart or code change needed.

### API key alternative (CI / scripts)

For headless callers that can't do OAuth, use long-lived API keys:

```bash
OBSCURA_API_KEYS="mykey:service-account:agent:read,agent:copilot"
# then:
curl -H "Authorization: Bearer mykey" http://localhost:8080/api/v1/whoami
```

Format is `token:user_id:role1,role2` — separate multiple keys with `;`.

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
# Web UI is now a separate repo
git clone <obscura-web-ui-repo> ~/dev/obscura-web-ui
cd ~/dev/obscura-web-ui
cp .env.example .env  # set VITE_API_URL=http://localhost:8080
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
