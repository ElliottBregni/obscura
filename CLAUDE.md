# Obscura — Agent Reference

Multi-backend AI agent runtime (CLI, REST API, Web UI, MCP server). Supports GitHub Copilot, Claude, OpenAI, and local LLMs with 100+ tools, vector memory, multi-agent orchestration, and 49 plugin integrations.

---

## Build & Development

**Prerequisites:** Python 3.13+, `uv`, Node.js ≥18 (Web UI only), Qdrant (optional, falls back to SQLite)

```bash
# Install
git clone <repo-url> && cd obscura-main
uv sync                  # production deps
uv sync --group dev      # + pytest, pyright, ruff

# Credentials — auto-loaded from .env, no sourcing needed
cp .env.example .env     # add GITHUB_TOKEN / ANTHROPIC_API_KEY / OPENAI_API_KEY

# Run
uv run obscura           # interactive REPL (default: copilot backend)
uv run obscura -b claude # Claude backend
uv run obscura "prompt"  # single-shot
```

**.env load order** (earlier wins): shell env → `~/.obscura/.env` → `.obscura/.env` → `./.env`

**Docker / infra:**
```bash
make local-up            # keychain → shell → docker compose (no .env on disk)
make dev-up              # dev env via compose
make dev-logs            # tail logs
```

**Build & release:**
```bash
uv build                 # sdist + wheel → dist/
make dist                # same via make
make brew-install        # build + install Homebrew formula
```

---

## Architecture

```
obscura/
├── core/               # Agent loop, tool registry, sessions, hooks, config
│   ├── agent_loop.py   # Main prompt→stream→tool→repeat loop (all backends)
│   ├── tool_context.py # Per-call session state for tools (ContextVar)
│   ├── tools.py        # ToolRegistry + alias map + @tool decorator
│   ├── supervisor/     # Multi-agent supervisor, state machine, vault hooks
│   ├── kairos/         # Autonomous background daemon (goal-driven)
│   ├── compaction.py   # Context window compaction
│   ├── event_store.py  # SQLite event log
│   ├── tool_router.py  # Tool dispatch + policy
│   └── types.py        # AgentEvent, BackendProtocol, ContentBlock, etc.
├── agent/              # Agent definitions, daemon, loop, coordinator, peers
├── arbiter/            # Eval engine, watchdog, criteria checks
├── auth/               # Capability grants, RBAC, secrets, middleware
├── cli/                # Click-based REPL, renderer, session mgmt, commands
├── integrations/
│   ├── mcp/            # MCP server + client (tool bridge)
│   │   └── discovery.py # Probe external MCP servers, register shadow specs
│   ├── a2a/            # Agent-to-agent gRPC/proto transport
│   ├── messaging/      # iMessage, WhatsApp, Slack, Telegram, Signal routing
│   └── msgraph/        # Microsoft Graph OAuth
├── memory/             # Key-value memory store
├── vector_memory/      # Qdrant-backed semantic memory
├── kairos/             # KAIROS mode: proactive, goal-driven, dream cycles
├── providers/          # LLM backends — share BackendToolHostMixin
│   └── _tool_host.py   # Mixin: register_tool / _tool_registry boilerplate
├── skills/             # Loadable skill modules (~/.obscura/.codex/skills/)
├── server/             # FastAPI REST API + SSE
├── routes/             # API route handlers
├── eval/               # Eval harness, scoring, regression
├── heartbeat/          # Agent health monitoring
└── mcp_server/         # Obscura-as-MCP-server entry point
```

**Data flow:**
```
CLI prompt → AgentLoop.run() → BackendProtocol.stream()
  → tool calls → ToolRegistry.execute() → tool result
  → back to model → AgentEventKind.AGENT_DONE
```

**Key storage paths:**
- `~/.obscura/` — all runtime state (sessions, memory, logs, config)
- `~/.obscura/config.toml` — capability grants, plugin settings
- `~/.obscura/mcp/core.json` — active MCP server configs
- `~/.obscura/events.db` — SQLite event log
- `~/.obscura/logs/deep.jsonl` — per-tool-call audit log

---

## Key Patterns

**Async-first:** All agent execution is `async`. The core loop is `async for event in loop.run(...)`.

**Backend protocol:** All LLM providers implement `BackendProtocol` — swap backends with `-b claude/copilot/codex/localllm`.

**Tool registration:** Tools are registered via `ToolRegistry`. Plugin tools are loaded lazily from `~/.obscura/plugins/builtins/<id>.toml` manifests and capability grants in `config.toml`.

Every backend inherits `BackendToolHostMixin` (`obscura/providers/_tool_host.py`), which owns `_tools` + `_tool_registry` and provides `register_tool(spec)` with duplicate-skip semantics. Adding a 6th backend is a matter of inheriting the mixin and calling `_init_tool_host()` in `__init__` — no copy-paste of the registration boilerplate.

**Tool context — how tools access session state:** Tools that need the active registry, conversation history, authenticated user, or host-supplied callbacks read them from a `ToolContext` bound by the agent loop around each invocation. `ContextVar` isolates the binding per asyncio task, so concurrent agents in the same process don't fight over shared state.

```python
from obscura.core.tool_context import current_tool_context

@tool("my_tool", "...")
async def my_tool() -> str:
    ctx = current_tool_context()
    if ctx is None or ctx.registry is None:
        return _json_error("no_context")
    # ctx.registry, ctx.history, ctx.user, ctx.session_id are available
    # ctx.ask_user_callback / .permission_mode_callback for host UI
    ...
```

Legacy `set_*_callback` setters (`set_ask_user_callback`, `set_permission_mode_callback`, etc.) keep working — the agent loop reads them into `ToolContext` before each tool call, so REPL wiring needs no changes when migrating an old tool to the new pattern.

**External MCP tool discovery:** When `mcp_servers` is configured for a backend, `register_external_mcp_tools(self, self._mcp_servers)` runs in `start()` and registers shadow `ToolSpec`s named `mcp__<server>__<tool>`. Claude SDK still routes the actual calls via `mcp_servers` passthrough — the shadows exist for system-prompt visibility and `tool_search` lookup. Discovery is best-effort (per-server timeout, never raises).

**Capability grants:** Tools are gated by capability strings. Grant/deny in `~/.obscura/config.toml`:
```toml
[defaults.capabilities]
grant = ["shell.exec", "file.read", "file.write", "git.ops"]
```

**Imports:**
```python
from obscura.core.agent_loop import AgentLoop
from obscura.core.types import AgentEvent, AgentEventKind, BackendProtocol
from obscura.core.tools import ToolRegistry, tool
from obscura.core.tool_context import ToolContext, current_tool_context, bind_tool_context
from obscura.core.event_store import SQLiteEventStore
from obscura.providers._tool_host import BackendToolHostMixin
from obscura.integrations.mcp.discovery import register_external_mcp_tools
from obscura.integrations.browser.client import (
    BrowserBridgeClient,
    attach_if_running,
    register_browser_tools,
)
```

**Browser bridge:** when the Obscura Chrome side panel is open, terminal
REPL boot calls `attach_if_running(client.register_tool)` and registers
~27 `browser_*` tools that proxy through the running native host's Unix
socket. There are two tool families — **always start with the cheap one
and escalate only on failure**:

- **Event dispatch** (`browser_fill`, `browser_click`, `browser_press_key`,
  `browser_clipboard_*`, `browser_eval_js`, etc.) — no banner, no debugger,
  but `isTrusted=false` so Tab won't move focus and chars don't auto-appear
  in inputs.
- **CDP** (`browser_type_text`, `browser_native_click`, `browser_native_press_key`,
  `browser_upload_file`, `browser_console_logs`, `browser_network_log`) —
  attaches `chrome.debugger`, Chrome shows a yellow banner. `isTrusted=true`,
  file uploads work, console/network observable. Call `browser_cdp_detach`
  when done to dismiss the banner.

Architecture and decision-tree details:
[`packages/browser-extension/ARCHITECTURE.md`](packages/browser-extension/ARCHITECTURE.md).

**Linting:** `ruff` — E/F rules, E501 ignored. Format: `ruff format`.
**Type checking:** `pyright` (configured via `pyrightconfig.json`).
**Naming:** `snake_case` modules/functions, `PascalCase` classes, dataclasses for events/types.

---

## Testing

```bash
# Run all unit tests (excludes e2e)
pytest tests/ -v -m "not e2e"

# Run specific markers
pytest tests/ -m unit
pytest tests/ -m integration

# With coverage (fails below 85%)
pytest tests/ --cov=obscura --cov-report=term-missing

# Lint + typecheck
make lint         # ruff check + format --check
make typecheck    # pyright
```

**Test layout:**
```
tests/
├── unit/obscura/     # Unit tests mirroring package structure
├── integration/      # Integration tests
├── e2e/              # End-to-end (require running server, slow)
├── cli/              # CLI-specific tests
└── conftest.py       # Shared fixtures
```

**Markers:** `unit`, `integration`, `e2e` — always exclude `e2e` in local dev.
**Async tests:** `asyncio_mode = "auto"` — all `async def test_*` run automatically.
