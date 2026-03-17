# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development

```bash
# Install (uses uv for dependency resolution)
uv sync

# Install with extras
uv sync --extra dev --extra server --extra providers

# Run the CLI
obscura                          # interactive REPL, default backend: copilot
obscura -b claude                # use Claude backend
obscura -b claude -m claude-sonnet-4-5-20250929 "one-shot prompt"
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
pytest tests/ --run-e2e
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

## Environment

Plugin Python dependencies install into `~/.obscura/venv/` (managed by `uv`). The bootstrapper at `obscura/plugins/bootstrapper.py` targets this venv, not the global Python. Session-init hooks prepend `~/.obscura/venv/bin` to PATH.

## Docker

```bash
make dev-up          # docker-compose dev environment
make dev-down
make dev-logs
make dev-restart
```

Compose files: `docker-compose.{base,dev,staging,prod}.yml`. Environment-specific via `scripts/compose-env.sh`.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **obscura-main** (11423 symbols, 33657 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## When Debugging

1. `gitnexus_query({query: "<error or symptom>"})` — find execution flows related to the issue
2. `gitnexus_context({name: "<suspect function>"})` — see all callers, callees, and process participation
3. `READ gitnexus://repo/obscura-main/process/{processName}` — trace the full execution flow step by step
4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` — see what your branch changed

## When Refactoring

- **Renaming**: MUST use `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` first. Review the preview — graph edits are safe, text_search edits need manual review. Then run with `dry_run: false`.
- **Extracting/Splitting**: MUST run `gitnexus_context({name: "target"})` to see all incoming/outgoing refs, then `gitnexus_impact({target: "target", direction: "upstream"})` to find all external callers before moving code.
- After any refactor: run `gitnexus_detect_changes({scope: "all"})` to verify only expected files changed.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Tools Quick Reference

| Tool | When to use | Command |
|------|-------------|---------|
| `query` | Find code by concept | `gitnexus_query({query: "auth validation"})` |
| `context` | 360-degree view of one symbol | `gitnexus_context({name: "validateUser"})` |
| `impact` | Blast radius before editing | `gitnexus_impact({target: "X", direction: "upstream"})` |
| `detect_changes` | Pre-commit scope check | `gitnexus_detect_changes({scope: "staged"})` |
| `rename` | Safe multi-file rename | `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` |
| `cypher` | Custom graph queries | `gitnexus_cypher({query: "MATCH ..."})` |

## Impact Risk Levels

| Depth | Meaning | Action |
|-------|---------|--------|
| d=1 | WILL BREAK — direct callers/importers | MUST update these |
| d=2 | LIKELY AFFECTED — indirect deps | Should test |
| d=3 | MAY NEED TESTING — transitive | Test if critical path |

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/obscura-main/context` | Codebase overview, check index freshness |
| `gitnexus://repo/obscura-main/clusters` | All functional areas |
| `gitnexus://repo/obscura-main/processes` | All execution flows |
| `gitnexus://repo/obscura-main/process/{name}` | Step-by-step execution trace |

## Self-Check Before Finishing

Before completing any code modification task, verify:
1. `gitnexus_impact` was run for all modified symbols
2. No HIGH/CRITICAL risk warnings were ignored
3. `gitnexus_detect_changes()` confirms changes match expected scope
4. All d=1 (WILL BREAK) dependents were updated

## CLI

- Re-index: `npx gitnexus analyze`
- Check freshness: `npx gitnexus status`
- Generate docs: `npx gitnexus wiki`

<!-- gitnexus:end -->
