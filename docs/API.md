# API Reference

FastAPI server at `http://localhost:8080`. All endpoints except `/health` require JWT authentication.

## Starting the Server

```bash
# Development (no auth, no telemetry)
export OBSCURA_AUTH_ENABLED=false
export OTEL_ENABLED=false
obscura serve --port 8080

# Production
obscura serve --host 0.0.0.0 --port 8080
```

App factory: `obscura.server:create_app`

## Health

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Server health check |

## Agents

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/agents` | `agent:write` | Spawn agent |
| GET | `/api/v1/agents` | `agent:read` | List agents |
| GET | `/api/v1/agents/{id}` | `agent:read` | Get agent status |
| DELETE | `/api/v1/agents/{id}` | `agent:write` | Stop agent |
| POST | `/api/v1/agents/{id}/run` | `agent:write` | Run task |
| GET | `/api/v1/agents/{id}/stream` | `agent:read` | SSE event stream |

### Spawn Agent

```bash
curl -X POST http://localhost:8080/api/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "code-reviewer",
    "model": "claude",
    "system_prompt": "Expert code reviewer.",
    "memory_namespace": "project:obscura",
    "max_iterations": 10,
    "mcp": {
      "enabled": true,
      "config_path": "~/.obscura/mcp-config.json",
      "server_names": ["filesystem"]
    },
    "system_tools": {"enabled": true},
    "a2a_remote_tools": {"agent-url": "https://remote-agent.example.com"}
  }'
```

Valid models: `copilot`, `claude`, `openai`, `localllm`, `moonshot`

### Run Task

```bash
curl -X POST http://localhost:8080/api/v1/agents/{id}/run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Review this code", "context": {"pr_number": 123}}'
```

### SSE Stream Events

| Event | Data | Description |
|-------|------|-------------|
| `text_delta` | `{"delta": "..."}` | Incremental text |
| `thinking_delta` | `{"delta": "..."}` | Chain-of-thought |
| `tool_use_start` | `{"tool_name": "...", "tool_id": "..."}` | Tool call begins |
| `tool_use_delta` | `{"delta": "..."}` | Tool input streaming |
| `tool_use_end` | `{"tool_id": "..."}` | Tool call complete |
| `tool_result` | `{"tool_id": "...", "result": "..."}` | Tool execution result |
| `done` | `{"finish_reason": "..."}` | Turn complete |
| `error` | `{"message": "..."}` | Error |

## Memory

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/memory` | `agent:read` | List keys |
| GET | `/api/v1/memory/search?q=<query>` | `agent:read` | Text search |
| GET | `/api/v1/memory/stats` | `agent:read` | Usage statistics |
| GET | `/api/v1/memory/namespaces` | `agent:read` | List namespaces |
| POST | `/api/v1/memory/namespaces` | `agent:write` | Create namespace |
| DELETE | `/api/v1/memory/namespaces/{ns}` | `agent:write` | Delete namespace |
| GET | `/api/v1/memory/namespaces/{ns}/stats` | `agent:read` | Namespace stats |
| GET | `/api/v1/memory/{ns}/{key}` | `agent:read` | Get value |
| POST | `/api/v1/memory/{ns}/{key}` | `agent:write` | Set value |
| DELETE | `/api/v1/memory/{ns}/{key}` | `agent:write` | Delete value |
| POST | `/api/v1/memory/transaction` | `agent:write` | Atomic multi-op |
| GET | `/api/v1/memory/export` | `agent:read` | Export as JSON |
| POST | `/api/v1/memory/import` | `agent:write` | Import from JSON |

Query parameters: `?namespace=<ns>` (list), `?ttl=<seconds>` (set).

## Sessions

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/sessions` | `agent:write` | Create session |
| GET | `/api/v1/sessions` | `agent:read` | List sessions |
| GET | `/api/v1/sessions/{id}` | `agent:read` | Get session |
| DELETE | `/api/v1/sessions/{id}` | `agent:write` | Delete session |

## Send (Direct)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/send` | `agent:write` | Send prompt to backend |

## Vector Memory

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/vector/remember` | `agent:write` | Store with embedding |
| POST | `/api/v1/vector/recall` | `agent:read` | Semantic search |

## MCP

| Method | Path | Description |
|--------|------|-------------|
| POST | `/mcp/tools/list` | List available MCP tools |
| POST | `/mcp/tools/call` | Execute an MCP tool |
| POST | `/mcp/resources/list` | List MCP resources |
| POST | `/mcp/prompts/list` | List MCP prompts |

## A2A (when enabled)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/.well-known/agent.json` | Agent card (discovery) |
| POST | `/a2a/jsonrpc` | JSON-RPC 2.0 endpoint |
| POST | `/a2a/tasks` | REST: create task |
| GET | `/a2a/tasks/{id}` | REST: get task |
| GET | `/a2a/tasks/{id}/stream` | SSE: stream task |

## Middleware Stack

Applied in order (innermost first):

1. **ObscuraTelemetryMiddleware** -- OpenTelemetry traces/metrics (if `OTEL_ENABLED=true`)
2. **JWTAuthMiddleware** -- JWKS validation (if `OBSCURA_AUTH_ENABLED=true`)
3. **CORSMiddleware** -- localhost + configured origins

## Authentication

JWT tokens validated against JWKS endpoint. The `AuthenticatedUser` is extracted from the token and injected into route handlers via FastAPI dependency injection.

Disable for development: `OBSCURA_AUTH_ENABLED=false`

## Error Responses

```json
{
  "detail": "Error message here"
}
```

Standard HTTP status codes: 400 (bad request), 401 (unauthorized), 403 (forbidden), 404 (not found), 500 (server error).
