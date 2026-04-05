# Obscura Architecture Reference

## Overview

Obscura is a multi-agent AI orchestration platform. The Python package root is `obscura/`. The server exposes a REST API on port 8080, a gRPC A2A interface on port 50051, and an MCP server via a separate entry point. A React web UI communicates with the REST API.

---

## Package Layout

```
obscura/
├── core/
│   ├── supervisor/       # Session coordinator (single-writer, SQLite WAL)
│   ├── kairos/           # Autonomous goal runtime
│   ├── config.py         # Pydantic config, from_env()
│   ├── types.py
│   ├── tools.py
│   ├── auth.py
│   ├── client.py
│   ├── handlers.py
│   ├── sessions.py
│   ├── stream.py
│   ├── paths.py
│   ├── context.py
│   └── agent_loop.py
├── providers/            # LLM backends (BackendProtocol)
├── tools/
│   ├── system/
│   ├── policy/
│   └── providers/
├── integrations/
│   ├── a2a/              # Agent-to-Agent protocol
│   ├── mcp/              # Model Context Protocol
│   └── messaging/        # Telegram, WhatsApp, Slack, Signal, Webhook
├── routes/               # 20 FastAPI routers
├── auth/                 # API key RBAC + capability tokens
├── vector_memory/        # Qdrant + SQLite vector backends
├── server/               # FastAPI app factory
└── cli/                  # Click CLI, 85+ slash commands
```

---

## Entry Points

Defined in `pyproject.toml`:

| Command | Target |
|---|---|
| `obscura` | `obscura.cli:main` |
| `obscura-mcp` | `obscura.mcp_server.__main__:main` |

---

## Core Subsystems

### Supervisor (`core/supervisor/`)

Single-writer session coordinator. Uses a SQLite advisory lock per session to guarantee at-most-one writer. All mutable context (tools, prompt, memory) is frozen during the `BUILDING_CONTEXT` phase to prevent flicker.

**16 modules**, **130 tests**, **14 SQLite tables** (WAL mode).

**State machine** — 7 states:

```
IDLE → BUILDING_CONTEXT → CONTEXT_READY → RUNNING → PAUSED → DONE
                                                    ↘            ↗
                                                     FAILED
```

| Component | Responsibility |
|---|---|
| `Supervisor` | Top-level coordinator, owns the session lifecycle |
| `SessionStateMachine` | Enforces valid state transitions |
| `SessionLock` | SQLite advisory lock, prevents concurrent writes |
| `FrozenToolRegistry` | Immutable snapshot of tools taken at BUILDING_CONTEXT |
| `PromptAssembler` | Assembles the final prompt from frozen context |
| `MemoryCommitGate` | Controls when memory writes are allowed |
| `SessionHeartbeatManager` | Emits periodic heartbeats, detects stale sessions |
| `SessionHookManager` | Lifecycle hooks (pre/post state transitions) |
| `RunObserver` | Observability callbacks per run |
| `AgentTemplateStore` | Stores and retrieves agent prompt templates |
| `PolicyStore` | Per-session tool policy snapshots |

Import:

```python
from obscura.core.supervisor import Supervisor, SupervisorConfig
```

---

### Kairos (`core/kairos/`)

Autonomous goal runtime. Operates independently of the Supervisor; manages long-horizon planning and execution.

**Data model:**

```
Goal → Plan → Task → Checkpoint → Intervention
```

| Component | Responsibility |
|---|---|
| `GoalStore` | Persists goals, plans, tasks, checkpoints (8 SQLite tables) |
| `PlanEngine` | Decomposes goals into plans and tasks |
| `TaskRunner` | Executes tasks, tracks budget consumption |

Budget tracking is first-class: token and time budgets are attached to goals and enforced by the TaskRunner.

Enabled via config: `OBSCURA_KAIROS=true`.

---

### Providers (`providers/`)

All LLM backends implement `BackendProtocol`.

**Key protocol constraint**: `stream()` is a synchronous `def` that returns an async generator. It is not an `async def`.

```python
class BackendProtocol(Protocol):
    def stream(self, messages: list[Message], **kwargs) -> AsyncGenerator[str, None]: ...
```

| Backend | Module |
|---|---|
| Claude (Anthropic) | `providers/claude.py` |
| GitHub Copilot | `providers/copilot.py` |
| OpenAI | `providers/openai.py` |
| Local LLM | `providers/localllm.py` |
| Codex | `providers/codex.py` |
| Moonshot | `providers/moonshot.py` |
| MCP backend | `providers/mcp_backend.py` |

Import:

```python
from obscura.providers import ClaudeBackend, CopilotBackend
```

---

### Tools (`tools/`)

| Subpackage | Contents |
|---|---|
| `tools/system/` | Built-in system tools (file ops, shell, web, etc.) |
| `tools/policy/` | `ToolPolicy`, `PolicyResult`, `evaluate_policy()` |
| `tools/providers/` | Tool provider adapters |

Policy import:

```python
from obscura.tools.policy import ToolPolicy, evaluate_policy
```

---

### Auth (`auth/`)

API key-based RBAC. Keys are passed as `Authorization: Bearer <api-key>`.

**Roles:**

| Role | Description |
|---|---|
| `admin` | Full access |
| `operator` | Manage sessions and agents |
| `agent:*` | Agent-scoped permissions |
| `sync:write` | Memory sync writes |
| `sessions:manage` | Session lifecycle |
| `a2a:invoke` | Invoke remote agents |
| `a2a:manage` | Manage A2A configuration |

Key format in `OBSCURA_API_KEYS`: `key:user:role1,role2`

**Capability tokens**: HMAC-signed tokens scoped to specific capabilities, issued by the auth layer.

---

### Vector Memory (`vector_memory/`)

Two storage backends, selectable by config:

| Backend | Notes |
|---|---|
| Qdrant | Primary vector store for semantic search |
| SQLite | Fallback / lightweight deployments |

**Semantic decay schedule:**

| Memory type | Half-life |
|---|---|
| `episode` | 7 days |
| `learned` | 30 days |
| `preference` | Permanent (no decay) |

Import:

```python
from obscura.core import MemoryStore
```

---

## Integrations

### A2A (`integrations/a2a/`)

Agent-to-Agent protocol. Supports multiple transports:

| Transport | Details |
|---|---|
| JSON-RPC 2.0 over HTTP/REST | Primary API surface |
| SSE | Server-Sent Events for streaming responses |
| gRPC | Port 50051 (`OBSCURA_A2A_GRPC_PORT`) |

| Component | Role |
|---|---|
| `A2AService` | Core request handler |
| `ObscuraA2AServer` | Server-side host |
| `A2AClient` | Client for invoking remote agents |
| `AgentCard` | Agent capability descriptor |

Enabled via config: `OBSCURA_A2A_ENABLED=true`.

---

### MCP (`integrations/mcp/`)

Model Context Protocol client and server. The MCP server runs as a separate process via the `obscura-mcp` entry point.

---

### Messaging (`integrations/messaging/`)

Inbound/outbound messaging channels:

| Channel | Notes |
|---|---|
| Telegram | Bot API |
| WhatsApp | WhatsApp Business API |
| Slack | Events API + Web API |
| Signal | Signal messenger |
| Webhook | Generic inbound HTTP webhooks |

---

## HTTP API (`routes/`)

**20 FastAPI routers**. Base prefix: `/api/v1` (exceptions: `/channels`, `/health`, `/webhooks`). Approximately 50+ endpoints total.

**Router categories:**

| Category | Prefix |
|---|---|
| Sessions | `/api/v1/sessions` |
| Agents | `/api/v1/agents` |
| Memory | `/api/v1/memory` |
| Goals (Kairos) | `/api/v1/goals` |
| Workflows | `/api/v1/workflows` |
| Approvals | `/api/v1/approvals` |
| Audit | `/api/v1/audit` |
| A2A | `/api/v1/a2a` |
| MCP | `/api/v1/mcp` |
| Admin | `/api/v1/admin` |
| Health | `/health` |
| Channels | `/channels` |
| Webhooks | `/webhooks` |

---

## Server (`server/`)

FastAPI application factory:

```python
from obscura.server import create_app
from obscura.core.config import ObscuraConfig

config = ObscuraConfig.from_env()
app = create_app(config)
```

Served by uvicorn on port 8080 (default).

---

## Configuration (`core/config.py`)

`ObscuraConfig` is a Pydantic `BaseModel` loaded from environment variables via `from_env()`.

| Variable | Default | Description |
|---|---|---|
| `OBSCURA_HOST` | `0.0.0.0` | Bind host |
| `OBSCURA_PORT` | `8080` | Bind port |
| `OBSCURA_AUTH_ENABLED` | `true` | Enable API key auth |
| `OBSCURA_DEFAULT_BACKEND` | `claude` | Default LLM provider |
| `OBSCURA_API_KEYS` | — | `key:user:role1,role2` pairs (comma-separated entries) |
| `OBSCURA_KAIROS` | `false` | Enable Kairos goal runtime |
| `OBSCURA_A2A_ENABLED` | `false` | Enable A2A integration |
| `OBSCURA_A2A_GRPC_PORT` | `50051` | gRPC port for A2A |

---

## CLI (`cli/`)

Click command group `obscura`. 85+ slash commands. Rendering via `StreamRenderer`.

```bash
obscura chat             # Interactive chat session
obscura sessions list    # List active sessions
obscura goals create     # Create a Kairos goal
obscura memory search    # Semantic memory search
obscura a2a invoke       # Invoke a remote agent
```

---

## Web UI (`web-ui/`)

| Property | Value |
|---|---|
| Framework | React 18 + TypeScript + Vite |
| Styling | Tailwind CSS + Radix UI |
| State | Zustand + React Query |
| Dev server | `cd web-ui && npm run dev` → localhost:5173 |
| Auth | `Authorization: Bearer <api-key>` header |

**Pages:**

| Page | Path |
|---|---|
| Dashboard | `/` |
| Agents | `/agents` |
| Sessions | `/sessions` |
| Memory | `/memory` |
| Workflows | `/workflows` |
| Goals | `/goals` |
| Approvals | `/approvals` |
| Webhooks | `/webhooks` |
| Audit | `/audit` |
| Health | `/health` |
| MCP | `/mcp` |
| A2A | `/a2a` |
| Admin | `/admin` |

---

## Deployment

### Docker Compose

```bash
docker compose up -d
```

Starts: server (port 8080) + telemetry stack.

Docker image: Python 3.13, uvicorn.

### Helm

```bash
helm install obscura helm/obscura/
```

Chart located at `helm/obscura/`.

---

## Testing

```bash
pytest                          # All tests
pytest -m unit                  # Unit tests only
pytest -m integration           # Integration tests
pytest -m e2e                   # End-to-end tests
pytest tests/unit/obscura/core/supervisor/  # Supervisor tests (130)
```

**Structure:**

```
tests/
└── unit/
    └── obscura/
        ├── core/
        │   ├── supervisor/     # 12 test files, 130 tests
        │   └── kairos/
        ├── providers/
        ├── tools/
        └── integrations/
```

---

## Key Patterns

### Import style

All modules use `from __future__ import annotations` for forward reference support.

### BackendProtocol.stream()

`stream()` must be a synchronous `def` returning an async generator — not an `async def`:

```python
# Correct
def stream(self, messages: list[Message], **kwargs) -> AsyncGenerator[str, None]:
    async def _gen():
        ...
    return _gen()

# Incorrect — do not use
async def stream(self, ...):
    ...
```

### Supervisor single-writer guarantee

The `SessionLock` acquires a SQLite advisory lock before any state mutation. Only one writer per session is permitted at a time. This is enforced at the lock level, not by application convention.

### FrozenToolRegistry

During the `BUILDING_CONTEXT` phase, the tool registry is snapshotted into a `FrozenToolRegistry`. Subsequent tool lookups during `RUNNING` use the frozen snapshot. This prevents mid-run tool changes from causing inconsistent behavior.

### frozenset defaults (pyright strict)

Default `frozenset` values require a typed factory function under pyright strict mode:

```python
def _empty_frozenset() -> frozenset[str]:
    return frozenset()

class MyModel(BaseModel):
    tags: frozenset[str] = Field(default_factory=_empty_frozenset)
```
