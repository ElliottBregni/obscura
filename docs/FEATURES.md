# Obscura Features — All 4 Complete

## ✅ 1. OpenClaw Client

**File:** `openclaw_client.py`

Drop this into your OpenClaw workspace:

```python
from obscura_client import get_obscura, quick, remember, recall

# Quick one-off agent
result = await quick("reviewer", "Review this code: ...")

# Store memory
await remember("Python async uses event loops")

# Recall semantically
memories = await recall("how to do concurrency?")

# Full client
obscura = await get_obscura()
agent = await obscura.spawn_agent("analyzer", "claude")
```

## ✅ 2. WebSocket Streaming

**Added to:** `sdk/server.py`

```python
# WebSocket endpoints
ws://localhost:8080/ws/agents/{agent_id}  # Real-time agent I/O
ws://localhost:8080/ws/monitor            # Live agent status updates

# Protocol:
# Client sends: {"type": "run", "prompt": "..."}
# Server streams: {"type": "chunk", "text": "..."}
# Server sends: {"type": "done"}
```

## ✅ 3. Web UI

**File:** `web-ui/index.html`

```bash
# Serve the UI
cd web-ui
python -m http.server 3000

# Open http://localhost:3000
```

Features:
- Real-time agent monitoring (WebSocket)
- Spawn/stop agents from browser
- Live status updates with animated indicators
- System logs
- Dark mode UI

## ✅ 4. CLI Tool

**File:** `obscura_cli.py`

```bash
# Install dependencies
pip install click rich httpx

# Make executable
chmod +x obscura_cli.py

# Use it
./obscura_cli.py agent spawn --name reviewer --model claude
./obscura_cli.py agent list
./obscura_cli.py agent run <id> --prompt "Review this"
./obscura_cli.py agent quick --prompt "Hello world"

# Memory
./obscura_cli.py memory set mykey "my value"
./obscura_cli.py memory get mykey
./obscura_cli.py memory search "query"

# Vector/Semantic memory
./obscura_cli.py vector remember "Python async uses event loops"
./obscura_cli.py vector recall "how to do concurrency"

# Server
./obscura_cli.py serve --port 8080
```

## Quick Start

```bash
# 1. Start server
cd ~/dev/obscura
uv run python -m uvicorn sdk.server:create_app --factory --port 8080

# 2. In another terminal, use CLI
python obscura_cli.py agent quick --prompt "Say hello"

# 3. Or open Web UI
open web-ui/index.html

# 4. From OpenClaw
from obscura_client import quick
result = await quick("helper", "What time is it?")
```

## All Commands

| Tool | Command | Description |
|------|---------|-------------|
| **CLI** | `agent spawn` | Create new agent |
| **CLI** | `agent list` | Show all agents |
| **CLI** | `agent run` | Execute task |
| **CLI** | `agent quick` | One-off agent |
| **CLI** | `memory set/get` | Key-value storage |
| **CLI** | `vector remember` | Semantic storage |
| **CLI** | `vector recall` | Semantic search |
| **CLI** | `serve` | Start server |
| **Web** | Spawn button | Create agents |
| **Web** | Status indicators | Live updates |
| **WebSocket** | `/ws/agents/{id}` | Real-time I/O |
| **WebSocket** | `/ws/monitor` | Status stream |
