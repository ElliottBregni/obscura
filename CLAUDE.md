# Obscura — Developer Guide

This file provides guidance to AI agents and humans working with code in this repository.

> **Sync notice:** This file and `CLAUDE.md` share identical content. Edit either one, then copy to the other.

## Build & Development

Requirements: Python 3.13+, uv, Node.js (for some tooling), optional Homebrew on macOS.

```bash
# Install (uses uv for dependency resolution)
uv sync

# Install with extras
uv sync --extra dev --extra server --extra providers

# Additional extras
uv sync --extra voice              # Voice input/STT
uv sync --extra server --extra telemetry  # API mode + observability

# Run the CLI
obscura                          # interactive REPL, default backend: copilot
obscura -b claude                # use Claude backend
obscura -b claude -m claude-sonnet-4-5-20250929 "one-shot prompt"
```

Server (FastAPI factory at `obscura/server/__init__.py:create_app`):
```bash
uv run python -m uvicorn obscura.server:create_app --factory --host 0.0.0.0 --port 8080
```

## Testing

```bash
# All unit tests (excludes e2e by default)
pytest tests/ -v -m "not e2e"

# Single test file
pytest tests/unit/obscura/core/test_preflight.py -v

# Single test by name
pytest tests/unit/obscura/core/test_lifecycle.py -v -k "test_policy_gate_denies"

# With coverage (fail_under=85)
pytest tests/ --cov=obscura --cov-report=term-missing

# End-to-end tests (requires running server)
export OBSCURA_URL=http://localhost:8080
export OBSCURA_TOKEN=local-dev-token
pytest tests/e2e/ -v --run-e2e
```

**Test conventions**: `asyncio_mode = "auto"` in pyproject.toml — all async tests run automatically without `@pytest.mark.asyncio`. Conftest provides autouse fixtures that reset singletons, disable OTEL, and route memory to tmp dirs.

## Linting & Type Checking

```bash
ruff check .                     # lint (E, F rules; E501 ignored)
ruff format --check .            # format check
pyright                          # strict mode, Python 3.13
```

Pyright is configured in `pyrightconfig.json` with `strict` type checking — `reportUnknownParameterType`, `reportPrivateUsage`, `reportImplicitOverride` all enabled.

## Architecture

Obscura is a multi-backend AI agent runtime. The key data flow:

```
YAML specs → Compiler → Frozen CompiledWorkspace → AgentLoop → Events → EventStore
                                                       ↑
                                                  HookRegistry
                                                  ToolBroker
```

### Compiler Pipeline (`obscura/core/compiler/`)

Declarative YAML specs follow a Kubernetes-like envelope (`apiVersion`, `kind`, `metadata`, `spec`). The compile pipeline:

1. **specs.py** — Pydantic models for raw YAML: `TemplateSpec`, `WorkspaceSpec`, `PolicySpec`
2. **loader.py** — Discovers and parses spec files from `~/.obscura/specs/`
3. **resolver.py** — Resolves template chains, expands workspace packs
4. **merger.py** — Merges template inheritance, compiles agents/policies/memory
5. **validator.py** — Validates the merged workspace
6. **compiled.py** — Frozen `@dataclass(frozen=True)` output: `CompiledAgent`, `CompiledWorkspace`, `CompiledPolicy`, `EnvironmentManifest`

All compiled models are **frozen dataclasses** — immutable after creation, safe across threads.

### Agent Loop (`obscura/core/agent_loop.py`)

Drives the model in an iterative loop: prompt → stream → detect tool calls → execute → feed back → repeat. Yields `AgentEvent` objects with `AgentEventKind` discriminator. Works with all backends.

### Providers (`obscura/providers/`)

Each LLM backend implements `BackendProtocol` from `core/types.py`: `copilot.py`, `claude.py`, `openai.py`, `codex.py`, `localllm.py`, `moonshot.py`. Provider selection via `-b` flag or `Backend` enum.

### Plugin System (`obscura/plugins/`)

- **loader.py** — Discovery → validate → resolve config → bootstrap → register. Lifecycle: `discovered → installed → enabled → active → unhealthy → disabled → failed`
- **broker.py** — `ToolBroker` is the single choke-point for all tool execution: schema validation → policy check → approval gate → execute → audit
- **policy.py** — `PluginPolicyEngine` with `allow/deny/approve` rules loaded from `~/.obscura/policies/`
- **bootstrapper.py** — Auto-installs plugin dependencies (pip, uv, npm, cargo, brew, pipx) into `~/.obscura/venv/`
- **builtins/** — Built-in plugin manifests (YAML files declaring tools, deps, config)

### Hooks (`obscura/core/hooks.py`)

Event-driven before/after hooks keyed by `AgentEventKind`. Before hooks can modify or suppress events; after hooks are side-effect only. Registered via `@hooks.before(kind)` / `@hooks.after(kind)` decorators.

### Lifecycle & Preflight (`obscura/core/lifecycle.py`, `preflight.py`)

- **lifecycle.py** — Five hook factories: `make_policy_gate_hook`, `make_audit_hook`, `make_redact_hook`, `make_preflight_hook`, `make_memory_inject_hook`
- **preflight.py** — `PreflightValidator` checks binaries, env vars, Python version, packages, paths before agent start

### Supervisor (`obscura/core/supervisor/`)

Single-writer coordinator: `acquire_lock → build_context → run_model ⇄ run_tools → commit_memory → finalize → release_lock`. Event-sourced with SQLite persistence.

### Event Store (`obscura/core/event_store.py`)

`SQLiteEventStore` at `~/.obscura/events.db`. Immutable append-only event log. Sessions recovered by replaying events. Session states: `RUNNING → WAITING_FOR_TOOL/USER → PAUSED → COMPLETED/FAILED`.

### Tools (`obscura/core/tools.py`, `obscura/tools/`)

`ToolRegistry` holds `ToolSpec` objects. ~100 aliases map LLM-generated names to canonical tools. System tools in `tools/system/`, provider tools in `tools/providers/`.

### Integrations (`obscura/integrations/`)

- **mcp/** — MCP server discovery, lifecycle, and tool bridging
- **a2a/** — Agent-to-Agent protocol with gRPC/Redis transports
- **msgraph/** — Microsoft Graph integration
- **imessage/** — iMessage bridge

### Memory

- **memory/** — Key-value per-user memory store
- **vector_memory/** — Semantic vector store (Qdrant or SQLite fallback), RAG-integrated into the CLI lifecycle

## Key Patterns

- **Frozen dataclasses everywhere** for compiled models — use `@dataclass(frozen=True)`
- **Pydantic `BaseModel`** with `model_config = {"extra": "forbid"}` for spec/input models
- **`from __future__ import annotations`** at top of every module
- **Python 3.13+** required (`requires-python = ">=3.13"`)
- **async throughout** — agent loop, hooks, broker, event store all use `async`/`await`
- **Type aliases** for hook signatures: `BeforeHook = Callable[[AgentEvent], Awaitable[AgentEvent | None] | AgentEvent | None]`
- Tools: Define via `@tool` in `obscura/core/tools.py`; keep parameters JSON-schema friendly. Aliases are heavily normalized, so prefer canonical names when calling from agents.
- Telemetry: Tool calls wrapped with OTel spans and metrics; server can enable OTel via env (`OTEL_ENABLED`, etc.).
- Config via env: See `obscura/core/config.py` for supported vars (e.g., `OBSCURA_AUTH_ENABLED`, rate limits, A2A settings, Kairos toggles, undercover mode).

## Environment

Plugin Python dependencies install into `~/.obscura/venv/` (managed by `uv`). The bootstrapper at `obscura/plugins/bootstrapper.py` targets this venv, not the global Python. Session-init hooks prepend `~/.obscura/venv/bin` to PATH.

## Docker

```bash
docker build -t obscura:dev .
docker run --rm -p 8080:8080 obscura:dev

make dev-up          # docker-compose dev environment
make dev-down
make dev-logs
make dev-restart
```

Compose files: `docker-compose.{base,dev,staging,prod}.yml`. Environment-specific via `scripts/compose-env.sh`. Docker image exposes port 8080; health check probes `/health`.

## Notes

- Package entry points (pyproject): `obscura` (CLI) and `obscura-mcp` (MCP server).
- Node tool `@mermaid-js/mermaid-cli` is present in `package.json` (for docs/diagrams).
- Makefile targets: `make dist` (build), `make lint`, `make typecheck`, `make test`.
- GitNexus (for contributors/agents editing code): AGENTS.md documents GitNexus workflows (impact analysis, detect changes, safe renames). Use those when modifying symbols or refactoring.
