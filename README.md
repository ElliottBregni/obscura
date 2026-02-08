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

## Usage Examples

### Python SDK

```python
from sdk import ObscuraClient

async with ObscuraClient("claude") as client:
    response = await client.send("Hello!")
    print(response.text)
```

### CLI

```bash
# Quick agent command
obscura claude -p "Explain Python async"

# Spawn persistent agent
obscura agent spawn --name reviewer --model claude

# List agents
obscura agent list

# Memory operations
obscura memory set mykey "my value"
obscura memory get mykey
```

### Full Demo

```bash
python examples/working_demo.py
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

### Running Tests

```bash
# Unit tests
pytest tests/ -v -m "not e2e"

# E2E tests (starts temp server)
./scripts/run-e2e-tests.sh

# All tests
pytest tests/ -v
```

### Project Structure

```
obscura/
├── sdk/              # Core SDK
├── obscura/tui/      # Terminal UI
├── tests/            # Test suite
├── examples/         # Usage examples
└── docs/             # Documentation
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
