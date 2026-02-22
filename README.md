# Obscura

> Multi-agent context management system with API, CLI, and TUI

Obscura is a unified agent runtime that supports multiple LLM providers (Claude, Copilot, OpenAI, Moonshot, LocalLLM) with shared memory, tool execution, and multi-agent orchestration.

## Quick Start

### Install

```bash
git clone <repo-url>
cd obscura-main

# Install with all dependencies
pip install -e ".[dev,server,telemetry,tui]"

# Or with uv
uv pip install -e ".[dev,server,telemetry,tui]"
```

### Start the Server

```bash
# Development mode (auth + telemetry disabled)
export OBSCURA_AUTH_ENABLED=false
export OTEL_ENABLED=false
obscura serve --port 8080
```

### Verify

```bash
# Health check
curl http://localhost:8080/health

# Spawn an agent
curl -X POST http://localhost:8080/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "my-agent", "model": "claude"}'

# Store a memory
curl -X POST http://localhost:8080/api/v1/memory/session/context \
  -H "Content-Type: application/json" \
  -d '{"value": {"repo": "obscura", "task": "getting started"}}'
```

### CLI

```bash
# Direct backend chat
obscura claude -p "Explain Python async"
obscura copilot -p "Write a test for auth.py"

# Agent management
obscura agent spawn --name reviewer --model claude
obscura agent list
obscura agent run <agent-id> --prompt "Review this module"

# Memory
obscura memory set mykey '{"project": "obscura"}'
obscura memory get mykey
obscura memory search "project"

# Vector memory (semantic)
obscura vector remember "Auth uses JWT with RS256 via Zitadel"
obscura vector recall "how does authentication work?"
```

### Python SDK

```python
from obscura.core import ObscuraClient

async with ObscuraClient("claude") as client:
    resp = await client.send("Hello!")
    print(resp.text)
```

### TUI

```bash
obscura tui
```

## Architecture

Obscura operates in two modes:

- **Unified Mode** -- Normalized interface for multi-agent orchestration, tools, sessions, streaming, and memory. Cross-provider portable.
- **Native Mode** -- Direct SDK access (`backend.native`) with zero abstraction. Provider-specific features, no portability guarantees.

### Layers

```
Layer 0: Provider SDKs (Claude, OpenAI, Copilot, local servers)
Layer 1: Backend Adapters (implement BackendProtocol, normalize streaming)
Layer 2: Agent Runtime (tool execution, memory, hooks, telemetry)
Layer 3: Server / CLI / TUI (FastAPI, Click, Textual)
```

### Package Structure

```
obscura/
  core/           # Stable API: types, client, config, auth, stream, sessions, tools
  providers/      # Backend adapters: claude, copilot, openai, localllm, moonshot
  auth/           # Zitadel JWT, RBAC, middleware
  memory/         # Per-user SQLite memory (MemoryStore, GlobalMemoryStore)
  tools/
    system/       # Shell, Python execution (sandboxed)
    policy/       # ToolPolicy engine (allow/deny lists, base_dir)
    providers/    # Tool provider protocol (System, MCP, A2A)
  integrations/
    mcp/          # Model Context Protocol client + server
    a2a/          # Agent-to-Agent protocol (JSON-RPC, REST, SSE, gRPC)
  agent/          # BaseAgent (APER lifecycle), AgentRuntime
  server/         # FastAPI app factory, middleware, lifespan
  routes/         # API endpoints (agents, memory, sessions, health, etc.)
  cli/            # Click CLI + unified chat CLI
  tui/            # Terminal UI (Textual)
  telemetry/      # OpenTelemetry traces, metrics, structured logging
  vector_memory/  # Semantic search with embeddings
  heartbeat/      # Health monitoring
```

### Stability Tiers

| Tier | Modules | Policy |
|------|---------|--------|
| **Stable** | `core`, `providers`, `auth`, `memory` | Breaking changes require RFC + migration guide |
| **Beta** | `tools`, `integrations.mcp`, `agent`, `server`, `cli`, `tui`, `telemetry` | Breaking changes require changelog |
| **Experimental** | `integrations.a2a`, `openclaw_bridge`, `parity`, `skills` | Breaking changes allowed |

### Backend Parity

| Feature | Copilot | Claude | OpenAI | LocalLLM | Moonshot |
|---------|---------|--------|--------|----------|---------|
| send/stream | Y | Y | Y | Y | Y |
| Tool use | Y | Y | Y | Partial | N |
| Sessions | Y | Y | Y | N | N |
| Thinking/CoT | N | Y | Y | N | N |
| Agent loop | Y | Y | Y | Y | Y |
| Native SDK | Y | Y | Y | N | N |

## API Reference

### Agents
- `POST /api/v1/agents` -- Spawn agent
- `GET /api/v1/agents` -- List agents (filter: `?status=RUNNING`)
- `GET /api/v1/agents/{id}` -- Get status
- `POST /api/v1/agents/{id}/run` -- Run task
- `GET /api/v1/agents/{id}/stream` -- SSE streaming
- `DELETE /api/v1/agents/{id}` -- Stop agent

### Memory
- `POST /api/v1/memory/{ns}/{key}` -- Store value (TTL: `?ttl=300`)
- `GET /api/v1/memory/{ns}/{key}` -- Get value
- `DELETE /api/v1/memory/{ns}/{key}` -- Delete value
- `GET /api/v1/memory` -- List keys (filter: `?namespace=session`)
- `GET /api/v1/memory/search?q=<query>` -- Text search
- `GET /api/v1/memory/stats` -- Usage statistics
- `POST /api/v1/memory/transaction` -- Atomic multi-op
- `GET /api/v1/memory/export` -- Export as JSON
- `POST /api/v1/memory/import` -- Import from JSON

### Sessions
- `POST /api/v1/sessions` -- Create session
- `GET /api/v1/sessions` -- List sessions
- `GET /api/v1/sessions/{id}` -- Get session
- `DELETE /api/v1/sessions/{id}` -- Delete session

### Health
- `GET /health` -- Server health check

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSCURA_AUTH_ENABLED` | `true` | Enable JWT authentication |
| `OBSCURA_AUTH_ISSUER` | -- | Zitadel OIDC issuer URL |
| `OBSCURA_AUTH_AUDIENCE` | -- | JWT audience |
| `OBSCURA_PORT` | `8080` | Server port |
| `OTEL_ENABLED` | `true` | Enable OpenTelemetry |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | -- | OTLP collector endpoint |
| `OBSCURA_CORS_ORIGINS` | `localhost` | Allowed CORS origins |
| `OBSCURA_A2A_ENABLED` | `false` | Enable Agent-to-Agent protocol |
| `OBSCURA_A2A_REDIS_URL` | -- | Redis URL for A2A pub/sub |
| `OBSCURA_MEMORY_DIR` | `~/.obscura/memory` | Memory storage path |
| `OBSCURA_LOG_LEVEL` | `INFO` | Logging level |
| `OBSCURA_LOG_FORMAT` | `json` | Log format (`json` or `text`) |

### Auth Credentials (per backend)

| Backend | Environment Variables |
|---------|----------------------|
| Copilot | `GITHUB_TOKEN`, `GH_TOKEN`, or `gh auth token` |
| Claude | `ANTHROPIC_API_KEY` or `claude auth status` |
| OpenAI | `OPENAI_API_KEY` |
| Moonshot | `MOONSHOT_API_KEY` or `OPENAI_API_KEY` |
| LocalLLM | `OBSCURA_LOCALLLM_BASE_URL` (default: `http://localhost:1234/v1`) |

## Development

### Tests

```bash
# Unit tests (fast, no server needed)
pytest tests/ -v -m "not e2e"

# E2E tests (starts temp server)
./scripts/run-e2e-tests.sh

# All tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=obscura --cov-report=term-missing --cov-fail-under=85
```

### Quality Checks

```bash
# Type checking (strict)
pyright

# Linting + formatting
ruff check .
ruff format --check .
```

### Docker

```bash
# Build
docker build -t obscura .

# Full stack (app + Redis + Zitadel + OTEL + Jaeger + Prometheus + Grafana)
docker compose up
```

### PR Requirements

1. `pyright` -- 0 errors
2. `ruff check .` -- clean
3. `pytest tests/unit/` -- all pass
4. Module-specific tests for changed modules (see `ownership.md`)

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/02-25-25-arch.md) | Design principles, layers, modes |
| [Memory](docs/MEMORY.md) | Memory system: namespaces, TTL, multi-tenancy |
| [Agents](docs/AGENTS.md) | Agent runtime: lifecycle, coordination, streaming |
| [Auth Guide](docs/AUTH_GUIDE.md) | Zitadel setup, RBAC, JWT |
| [MCP](docs/MCP-README.md) | Model Context Protocol integration |
| [Vector Memory](docs/VECTOR_MEMORY.md) | Semantic search with embeddings |
| [Testing](docs/TESTING.md) | Test suite organization and strategy |

## Troubleshooting

```bash
# Import errors
pip install -e .

# Port in use
lsof -ti:8080 | xargs kill

# Debug logging
export OBSCURA_LOG_LEVEL=DEBUG
obscura serve

# Auth issues (dev)
export OBSCURA_AUTH_ENABLED=false
```

## License

MIT
