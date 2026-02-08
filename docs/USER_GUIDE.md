# Obscura User Guide

## Table of Contents
1. [Installation](#installation)
2. [Quick Start](#quick-start)
3. [Core Concepts](#core-concepts)
4. [API Usage](#api-usage)
5. [TUI Guide](#tui-guide)
6. [Examples](#examples)
7. [Troubleshooting](#troubleshooting)

## Installation

### Requirements
- Python 3.12+
- pip or uv

### Install

```bash
# Clone repository
git clone <your-repo-url>
cd obscura

# Install with pip
pip install -e ".[dev,server,telemetry,tui]"

# Or with uv (faster)
uv pip install -e ".[dev,server,telemetry,tui]"
```

### Verify Installation

```bash
python -c "from sdk import ObscuraClient; print('✓ SDK installed')"
obscura --help
```

## Quick Start

### 1. Start the Server

```bash
# For development (auth disabled)
export OBSCURA_AUTH_ENABLED=false
export OTEL_ENABLED=false
obscura serve

# For production (auth enabled)
obscura serve --host 0.0.0.0 --port 8080
```

### 2. Create Your First Agent

```bash
curl -X POST http://localhost:8080/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-first-agent",
    "model": "claude",
    "system_prompt": "You are a helpful coding assistant."
  }'
```

Response:
```json
{
  "agent_id": "agent-abc123",
  "name": "my-first-agent",
  "status": "WAITING",
  "created_at": "2026-02-08T00:00:00"
}
```

### 3. Run a Task

```bash
curl -X POST http://localhost:8080/api/v1/agents/agent-abc123/run \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain Python decorators",
    "context": {"language": "python"}
  }'
```

### 4. Use Memory

```bash
# Store
curl -X POST http://localhost:8080/api/v1/memory/project/context \
  -H "Content-Type: application/json" \
  -d '{"value": {"framework": "fastapi"}}'

# Retrieve
curl http://localhost:8080/api/v1/memory/project/context
```

### 5. Launch TUI

```bash
obscura tui
```

Press `F1` for help, `F2` for dashboard, `F3` for chat.

## Core Concepts

### Agents
Agents are AI workers that can run tasks. Each agent has:
- **Name**: Identifier
- **Model**: AI backend (claude, copilot)
- **Memory namespace**: Isolated storage
- **Status**: PENDING → RUNNING → COMPLETED/FAILED

### Memory
Key-value storage scoped by namespace:
- **session**: Current conversation
- **project**: Project-wide data
- **user**: User preferences
- **semantic**: Vector embeddings for semantic search

### Backends
Supported AI providers:
- **claude**: Anthropic Claude
- **copilot**: GitHub Copilot
- **lmstudio**: Local models (coming)
- **byok**: Bring your own key (coming)

## API Usage

### Health Check
```bash
curl http://localhost:8080/health
```

### Agents

#### Create Agent
```bash
curl -X POST http://localhost:8080/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "code-reviewer",
    "model": "claude",
    "system_prompt": "Review code for security issues"
  }'
```

#### List Agents
```bash
curl http://localhost:8080/api/v1/agents
```

#### Get Agent Status
```bash
curl http://localhost:8080/api/v1/agents/{agent_id}
```

#### Run Task
```bash
curl -X POST http://localhost:8080/api/v1/agents/{agent_id}/run \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Review this code: ...",
    "context": {"file": "main.py"}
  }'
```

#### Stop Agent
```bash
curl -X DELETE http://localhost:8080/api/v1/agents/{agent_id}
```

### Memory

#### Store Value
```bash
curl -X POST http://localhost:8080/api/v1/memory/{namespace}/{key} \
  -H "Content-Type: application/json" \
  -d '{"value": {"data": "here"}}'
```

#### Get Value
```bash
curl http://localhost:8080/api/v1/memory/{namespace}/{key}
```

#### Delete Value
```bash
curl -X DELETE http://localhost:8080/api/v1/memory/{namespace}/{key}
```

#### Search Memory
```bash
curl "http://localhost:8080/api/v1/memory/search?q=python"
```

#### Semantic Search
```bash
curl -X POST http://localhost:8080/api/v1/vector-memory/docs/python \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Python async uses event loops",
    "metadata": {"topic": "async"}
  }'

curl "http://localhost:8080/api/v1/vector-memory/search?q=concurrency"
```

## TUI Guide

### Launch
```bash
obscura tui
```

### Navigation
- `F1`: Help
- `F2`: Dashboard (agent overview)
- `F3`: Chat (interactive agent chat)
- `F4`: Plan (task planning)
- `F5`: Code (file browser)
- `F6`: Diff (side-by-side diff)
- `Tab`: Next widget
- `Ctrl+C`: Quit

### Dashboard (F2)
- View all agents and their status
- Spawn new agents
- Stop agents
- View statistics

### Chat (F3)
- Select an agent from dropdown
- Type messages and press Enter
- View conversation history
- Real-time streaming responses

### Plan (F4)
- Create task plans with steps
- Track progress
- Spawn agents for each step
- Mark steps complete

## Examples

### Example 1: Code Review Workflow

```bash
#!/bin/bash

# 1. Create code reviewer agent
AGENT=$(curl -s -X POST http://localhost:8080/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "code-reviewer",
    "model": "claude",
    "system_prompt": "You are a senior engineer reviewing code for security and performance."
  }' | jq -r '.agent_id')

# 2. Review some code
curl -s -X POST "http://localhost:8080/api/v1/agents/${AGENT}/run" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Review this Python function for security issues:\n\ndef login(username, password):\n    query = f\"SELECT * FROM users WHERE username = '{username}'\"\n    return db.execute(query)"
  }'

# 3. Clean up
curl -s -X DELETE "http://localhost:8080/api/v1/agents/${AGENT}"
```

### Example 2: Multi-Agent Task

```python
import asyncio
import httpx

async def multi_agent_workflow():
    async with httpx.AsyncClient() as client:
        base_url = "http://localhost:8080"
        
        # Spawn agents
        agents = []
        for name, model in [("analyzer", "claude"), ("tester", "claude")]:
            resp = await client.post(f"{base_url}/api/v1/agents", json={
                "name": name,
                "model": model
            })
            agents.append(resp.json()["agent_id"])
        
        # Run tasks in parallel
        tasks = [
            client.post(f"{base_url}/api/v1/agents/{aid}/run", 
                       json={"prompt": "Analyze code"})
            for aid in agents
        ]
        results = await asyncio.gather(*tasks)
        
        # Clean up
        for aid in agents:
            await client.delete(f"{base_url}/api/v1/agents/{aid}")
        
        return results

asyncio.run(multi_agent_workflow())
```

### Example 3: Memory Persistence

```python
import httpx

async def persistent_memory():
    async with httpx.AsyncClient() as client:
        base_url = "http://localhost:8080"
        
        # Store user preferences
        await client.post(f"{base_url}/api/v1/memory/user/preferences", json={
            "value": {"theme": "dark", "language": "python"}
        })
        
        # Store session context
        await client.post(f"{base_url}/api/v1/memory/session/context", json={
            "value": {"current_file": "main.py", "line": 42}
        })
        
        # Store semantic memory
        await client.post(f"{base_url}/api/v1/vector-memory/knowledge/python", json={
            "text": "Python decorators are functions that modify other functions",
            "metadata": {"topic": "decorators"}
        })
        
        # Retrieve later
        prefs = await client.get(f"{base_url}/api/v1/memory/user/preferences")
        print(f"Preferences: {prefs.json()['value']}")

import asyncio
asyncio.run(persistent_memory())
```

## Troubleshooting

### Import Errors
```bash
# Problem: ModuleNotFoundError

# Solution 1: Reinstall in editable mode
pip install -e .

# Solution 2: Use uv run
uv run python your_script.py
```

### Server Won't Start
```bash
# Problem: Port already in use

# Find and kill process
lsof -ti:8080 | xargs kill -9

# Or use different port
obscura serve --port 9000
```

### 401 Unauthorized Errors
```bash
# Problem: Auth enabled but no valid token

# Solution 1: Disable auth (development only)
export OBSCURA_AUTH_ENABLED=false
obscura serve

# Solution 2: Use valid JWT token
export OBSCURA_TOKEN="your-valid-token"
```

### Tests Failing
```bash
# Run with verbose output
pytest tests/ -v --tb=short

# Run specific test
pytest tests/test_memory.py -v

# Run E2E tests with temp server
./scripts/run-e2e-tests.sh
```

### TUI Not Working
```bash
# Problem: textual not installed

# Install TUI dependencies
pip install textual rich

# Or with extras
pip install -e ".[tui]"
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSCURA_AUTH_ENABLED` | `true` | Enable JWT authentication |
| `OBSCURA_PORT` | `8080` | Server port |
| `OBSCURA_HOST` | `0.0.0.0` | Server bind host |
| `OTEL_ENABLED` | `true` | Enable OpenTelemetry |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4317` | OTel collector URL |
| `OBSCURA_LOG_LEVEL` | `INFO` | Logging level |
| `OBSCURA_LOG_FORMAT` | `json` | Log format (json/text) |

## Getting Help

- **Documentation**: See `docs/` directory
- **Examples**: See `examples/` directory
- **Issues**: Check GitHub issues
- **Tests**: Run `pytest tests/ -v`

## Next Steps

1. ✅ Server running
2. ✅ Created first agent
3. ✅ Used memory
4. 🔄 Try the TUI: `obscura tui`
5.  Read full docs in `docs/`
6. 🚀 Build your own workflows!
