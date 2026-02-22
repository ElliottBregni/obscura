# Architecture

Obscura is a unified agent runtime supporting multiple LLM providers with shared memory, tool execution, and multi-agent orchestration.

## Design Principles

1. **Two Modes, Never Mixed** -- Unified mode provides cross-provider portability. Native mode gives full SDK access. They don't overlap.
2. **Adapters, Not Wrappers** -- Each backend translates between unified types and provider-specific formats. No behavior is hidden or suppressed.
3. **Capabilities Over Pretending** -- Backends declare what they support. The runtime adapts accordingly rather than silently ignoring unsupported features.
4. **Stable Contract** -- The agent runtime depends only on unified types. Providers evolve independently.

## System Layers

```
Layer 3: Interfaces (FastAPI server, Click CLI, Textual TUI, React web-ui)
Layer 2: Agent Runtime (orchestration, tool execution, memory, hooks, telemetry)
Layer 1: Backend Adapters (implement BackendProtocol, normalize streaming)
Layer 0: Provider SDKs (Claude, OpenAI, Copilot, local servers)
```

## Package Structure

```
obscura/
  core/                 # Stable API
    types.py            # Data models: Message, StreamChunk, BackendProtocol, ToolSpec, etc.
    config.py           # ObscuraConfig (Pydantic BaseModel, env var loading)
    client/             # ObscuraClient: unified dispatcher to backends
    auth.py             # Credential resolution (env vars, CLI tools, OAuth)
    tools.py            # @tool decorator, schema inference, telemetry
    stream.py           # Streaming adapters (EventToIteratorBridge, ClaudeIteratorAdapter)
    sessions.py         # SessionStore (in-memory + persistent)
    handlers.py         # RequestHandler protocol
    agent_loop.py       # Tool-calling loop (accumulate deltas, execute, inject results)
    context.py          # ContextLoader (vault-based prompt loading)
    paths.py            # Path resolution utilities

  providers/            # Backend adapters (each implements BackendProtocol)
    copilot.py          # GitHub Copilot (event-based streaming via EventToIteratorBridge)
    claude.py           # Anthropic Claude (ClaudeIteratorAdapter, session fork)
    openai.py           # OpenAI (Responses API + Chat Completions)
    localllm.py         # Local OpenAI-compatible servers (localhost:1234)
    moonshot.py         # Moonshot/Kimi (extends OpenAI backend)
    mcp_backend.py      # MCP as a backend

  auth/                 # Authentication & authorization
    middleware.py       # JWTAuthMiddleware, JWKSCache
    models.py           # AuthenticatedUser dataclass
    rbac.py             # Role-based access control
    capabilities.py     # CapabilityTier, token generation

  memory/               # Per-user SQLite storage
    __init__.py         # MemoryStore, GlobalMemoryStore

  tools/
    system/             # Built-in tools (shell, Python execution)
    policy/             # ToolPolicy engine (allow/deny lists, base_dir sandboxing)
    providers/          # ToolProvider protocol (System, MCP, A2A sources)

  integrations/
    mcp/                # Model Context Protocol (client + server, stdio/SSE transport)
    a2a/                # Agent-to-Agent protocol (JSON-RPC, REST, SSE, gRPC)

  agent/                # Agent orchestration
    agent.py            # BaseAgent with APER lifecycle
    agents.py           # AgentRuntime, MCPConfig, system tools, approval workflow

  server/               # FastAPI app factory, middleware, lifespan
  routes/               # API endpoints (agents, memory, sessions, health, etc.)
  cli/                  # Click CLI (chat_cli.py) + unified CLI (__init__.py)
  tui/                  # Terminal UI (Textual)
  telemetry/            # OpenTelemetry traces, metrics, structured logging
  vector_memory/        # Semantic search with embeddings
  heartbeat/            # Health monitoring
```

## BackendProtocol

Every provider implements this contract:

```python
@runtime_checkable
class BackendProtocol(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, prompt: str, **kwargs: Any) -> Message: ...
    def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]: ...
    async def create_session(self, **kwargs: Any) -> SessionRef: ...
    async def resume_session(self, session_id: str) -> SessionRef: ...
    async def list_sessions(self) -> list[SessionRef]: ...
    async def delete_session(self, session_id: str) -> bool: ...
    def register_tool(self, spec: ToolSpec) -> None: ...
    def register_hook(self, hook: HookPoint, callback: Callable) -> None: ...
    def capabilities(self) -> BackendCapabilities: ...
    @property
    def native(self) -> Any: ...  # Raw SDK access
```

Note: `stream()` is a sync method that returns an async iterator. This accommodates backends with different concurrency models.

## Unified Stream Contract

All backends emit normalized `StreamChunk` events:

| Kind | Description |
|------|-------------|
| `MESSAGE_START` | Conversation turn begins |
| `TEXT_DELTA` | Incremental text output |
| `THINKING_DELTA` | Extended thinking / chain-of-thought (optional) |
| `TOOL_USE_START` | Tool call initiated |
| `TOOL_USE_DELTA` | Tool input being streamed |
| `TOOL_USE_END` | Tool call arguments complete |
| `TOOL_RESULT` | Tool execution result |
| `ERROR` | Error during streaming |
| `DONE` | Turn complete (includes finish_reason, usage, model, session_id) |

## Agent Loop

The agent loop (`core/agent_loop.py`) handles multi-turn tool execution:

1. Stream response from backend
2. Accumulate `TOOL_USE_DELTA` chunks into complete tool call
3. Validate tool name against registry
4. Execute tool handler (sync or async)
5. Inject `TOOL_RESULT` back into conversation
6. Continue streaming until `DONE`

Features: capability token enforcement, confirmation gate for human-in-the-loop, audit hooks for denied calls, telemetry spans.

## APER Agent Lifecycle

`BaseAgent` implements the Analyze-Plan-Execute-Respond pattern:

```
PRE_ANALYZE -> analyze() -> POST_ANALYZE ->
PRE_PLAN    -> plan()    -> POST_PLAN    ->
PRE_EXECUTE -> execute() -> POST_EXECUTE ->
PRE_RESPOND -> respond() -> POST_RESPOND
```

8 hook points for validation, persistence, audit, or short-circuit logic. Each phase can be deterministic Python or LLM-driven via `self._client.send()`.

## Memory Architecture

Per-user SQLite databases at `~/.obscura/memory/<user_hash>.db`:

```
User (JWT) -> MemoryStore.for_user(user) -> SQLite DB (isolated)
```

- Namespace-organized key-value storage
- TTL support for ephemeral data
- Text search across keys and values
- Agent state persistence (survives restarts)
- GlobalMemoryStore for org-wide shared data

## Stability Tiers

| Tier | Modules | Change Policy |
|------|---------|---------------|
| **Stable** | `core`, `providers`, `auth`, `memory` | RFC + migration guide required |
| **Beta** | `tools`, `integrations.mcp`, `agent`, `server`, `cli`, `tui`, `telemetry` | Changelog entry required |
| **Experimental** | `integrations.a2a`, `openclaw_bridge`, `parity`, `skills` | Breaking changes allowed |

## Backend Parity

| Feature | Copilot | Claude | OpenAI | LocalLLM | Moonshot |
|---------|---------|--------|--------|----------|---------|
| send/stream | Y | Y | Y | Y | Y |
| Tool use | Y | Y | Y | Partial | N |
| System prompt | Y | Y | Y | Y | Y |
| Multi-turn | Y | Y | Y | Y | Y |
| Sessions | Y | Y | Y | N | N |
| Thinking/CoT | N | Y | Y | N | N |
| Agent loop | Y | Y | Y | Y | Y |
| Native SDK | Y | Y | Y | N | N |
