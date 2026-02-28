# CLI Reference

Obscura has two CLI entry points:

- `obscura` (or `obscura-sdk`) -- Unified CLI for direct backend chat, sessions, and server management
- `obscura` (Click CLI via `chat_cli.py`) -- Agent, memory, and server management

## Direct Backend Chat

```bash
# Chat with a backend
obscura-sdk copilot -p "explain this code"
obscura-sdk claude -p "summarize this file" --model claude-sonnet-4-5-20250929
obscura-sdk openai -p "refactor this" --model gpt-4o
obscura-sdk moonshot -p "translate to Chinese" --model kimi-2.5
obscura-sdk localllm -p "hello from localhost"

# Pipe input
cat file.py | obscura-sdk copilot -p "review this"

# Streaming (default) vs blocking
obscura-sdk claude -p "write a poem" --stream
obscura-sdk claude -p "write a poem" --no-stream

# Unified vs native mode
obscura-sdk claude -p "hello" --mode unified   # Normalized types (default)
obscura-sdk claude -p "hello" --mode native     # Raw SDK access
```

### Sessions

```bash
# Start a session
obscura-sdk claude -p "Let's discuss auth" --session my-session

# Resume
obscura-sdk claude -p "continue" --session my-session

# List sessions
obscura-sdk claude --list-sessions
```

### Options

| Flag | Description |
|------|-------------|
| `-p`, `--prompt` | Prompt text (reads stdin if omitted) |
| `--model` | Raw model ID (e.g. `gpt-5-mini`, `claude-sonnet-4-5-20250929`) |
| `--model-alias` | Copilot model alias (e.g. `copilot_automation_safe`) |
| `--automation-safe` | Require automation-safe model (Copilot only) |
| `--system-prompt` | System prompt for the conversation |
| `--mode` | `unified` (default) or `native` |
| `--stream` / `--no-stream` | Stream output (default: stream) |
| `--session` | Session ID to resume |
| `--list-sessions` | List available sessions and exit |
| `--permission-mode` | Claude permission mode |

## Agent Management

```bash
# Spawn an agent
obscura agent spawn --name reviewer --model claude

# List agents (all or by status)
obscura agent list
obscura agent list --status RUNNING

# Get agent status
obscura agent status <agent-id>

# Run a task
obscura agent run <agent-id> --prompt "Review this code"

# Stop an agent
obscura agent stop <agent-id>
```

## Memory Operations

```bash
# Set a value
obscura memory set <key> <value>
obscura memory set <key> <value> --namespace session

# Get a value
obscura memory get <key>
obscura memory get <key> --namespace session

# List keys
obscura memory list
obscura memory list --namespace session

# Search
obscura memory search <query>
```

## Vector Memory

```bash
# Store with semantic embedding
obscura vector remember "Auth uses JWT with RS256 via Zitadel"

# Semantic recall
obscura vector recall "how does authentication work?" --top-k 3
```

## Server

```bash
# Start the API server
obscura serve --port 8080
obscura serve --host 0.0.0.0 --port 8080 --reload
```

## Other Commands

```bash
# Launch TUI
obscura tui

# Health check
obscura health

# Passthrough (run vendor CLI directly)
obscura-sdk passthrough claude "hello"
obscura-sdk passthrough copilot "hello"

# Observe agent runtime state
obscura-sdk observe
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSCURA_URL` | `http://localhost:8080` | API base URL (for Click CLI) |
| `OBSCURA_TOKEN` | `local-dev-token` | Auth token (for Click CLI) |
| `GITHUB_TOKEN` | -- | Copilot auth |
| `OBSCURA_GITHUB_TOKEN_CMD` | -- | Copilot token command fallback |
| `ANTHROPIC_API_KEY` | -- | Claude auth |
| `OBSCURA_CLAUDE_TOKEN_CMD` | -- | Claude token command fallback |
| `OPENAI_API_KEY` | -- | OpenAI auth |
| `MOONSHOT_API_KEY` | -- | Moonshot auth |
| `OBSCURA_LOCALLLM_BASE_URL` | `http://localhost:1234/v1` | Local LLM server |
