# 🧪 Obscura

> Multi-agent context management system with TUI and API

## Quick Start (5 minutes)

### 1. Install

```bash
git clone <repo-url>
cd obscura

# Install with all dependencies
pip install -e ".[dev,server,telemetry,tui]"

# Or with uv
uv pip install -e ".[dev,server,telemetry,tui]"
```

### 2. Start Server

```bash
# Start with auth disabled (for development)
export OBSCURA_AUTH_ENABLED=false
export OTEL_ENABLED=false
obscura serve --port 8080

# Or using uvicorn directly
uv run python -m uvicorn sdk.server:create_app --factory --port 8080
```

### 3. Test It Works

```bash
# Health check
curl http://localhost:8080/health

# Create an agent
curl -X POST http://localhost:8080/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "my-agent", "model": "claude"}'

# Store memory
curl -X POST http://localhost:8080/api/v1/memory/session/context \
  -H "Content-Type: application/json" \
  -d '{"value": {"key": "value"}}'
```

### 4. Launch TUI

```bash
obscura tui
```

## API Reference

### Health
- `GET /health` - Server health check

### Agents
- `POST /api/v1/agents` - Create agent
- `GET /api/v1/agents` - List agents
- `GET /api/v1/agents/{id}` - Get agent status
- `DELETE /api/v1/agents/{id}` - Stop agent
- `POST /api/v1/agents/{id}/run` - Run task

### Memory
- `POST /api/v1/memory/{ns}/{key}` - Store value
- `GET /api/v1/memory/{ns}/{key}` - Get value
- `DELETE /api/v1/memory/{ns}/{key}` - Delete value
- `GET /api/v1/memory` - List keys

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSCURA_AUTH_ENABLED` | `true` | Enable JWT auth |
| `OBSCURA_PORT` | `8080` | Server port |
| `OTEL_ENABLED` | `true` | Enable telemetry |

## Development

### Running & Using

**Install (dev):**
```bash
pip install -e "[dev,server,telemetry,tui]"
# or
uv pip install -e "[dev,server,telemetry,tui]"
```

**Run server (dev, auth off, telemetry off):**
```bash
export OBSCURA_AUTH_ENABLED=false
export OTEL_ENABLED=false
obscura serve --port 8080
# or
uv run python -m uvicorn sdk.server:create_app --factory --port 8080
```

**Use the CLI:**
```bash
# Quick ask
obscura claude -p "Explain Python async"
# Spawn/list agents
obscura agent spawn --name reviewer --model claude
obscura agent list
# Memory ops
obscura memory set mykey "value"
obscura memory get mykey
```

**Python SDK:**
```python
from sdk import ObscuraClient

async with ObscuraClient("claude") as client:
    resp = await client.send("Hello!")
    print(resp.text)
```

**TUI:**
```bash
obscura tui
```

**Tests:**
- Unit: `pytest tests/unit -v`
- E2E: `pytest tests/e2e -v` (starts temp server; ensure ports free)
- All (if needed): `pytest tests -v`

**Config & logs:**
- MCP config: `config/mcp-config.json` (template in same folder)
- Audit log: `logs/audit.jsonl`

**Troubleshooting**


```bash
# Unit tests
pytest tests/ -v -m "not e2e"

# E2E tests (starts temp server)
./scripts/run-e2e-tests.sh

# All tests
pytest tests/ -v
```

### Project Structure (updated)

```
obscura/
├── sdk/                  # Core SDK (agents, backends, telemetry, tui, mcp, etc.)
│   ├── agent/            # Agent runtime, loop, base agent
│   ├── backends/         # Copilot, Claude, OpenAI-compatible, LocalLLM
│   ├── internal/         # Auth, tools, types, stream, sessions (internal APIs)
│   ├── telemetry/        # traces, metrics, audit hooks
│   ├── vector_memory/    # memory store, filters, router, rerank
│   └── ...               # cli, client, config, routes, tui, mcp, etc.
├── config/               # Runtime configs (e.g., mcp-config.json, templates)
├── logs/                 # Runtime logs (e.g., audit.jsonl)
├── scripts/              # Helper scripts (sync, crawlers, etc.)
├── tests/
│   ├── unit/             # Unit tests grouped by sdk sub-areas + scripts
│   └── e2e/              # End-to-end tests
├── docs/                 # Documentation
├── examples/             # Usage examples
└── web-ui/               # Frontend (excluded from pyright)
```

## Troubleshooting

### Import errors
```bash
# Reinstall in editable mode
pip install -e .
```

### Server won't start
```bash
# Check port 8080 isn't in use
lsof -ti:8080 | xargs kill

# Start with debug logging
export OBSCURA_LOG_LEVEL=DEBUG
obscura serve
```

### Auth issues
```bash
# Disable auth for development
export OBSCURA_AUTH_ENABLED=false
```

## License

MIT
