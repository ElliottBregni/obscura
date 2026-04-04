# Obscura — Developer Guide (Concise)

This file orients AI agents and humans to build, run, and extend this repo quickly. It reflects what’s present in the codebase as of April 3, 2026.

## 1) Build & Development

- Requirements: Python 3.13+, uv, Node.js (for some tooling), optional Homebrew on macOS.
- Install (base):
```bash
uv sync
```
- Install (extras):
```bash
# Voice input/STT
uv sync --extra voice
# Server + telemetry (for API mode and observability)
uv sync --extra server --extra telemetry
# Dev toolchain (pytest, ruff, pyright, etc.)
uv sync --extra dev
```
- CLI (REPL):
```bash
obscura                 # default backend: copilot
obscura -b claude       # choose backend
obscura -b claude -m claude-sonnet-4-5-20250929
obscura -p "explain this code"   # single-shot
```
- Server (FastAPI factory at `obscura/server/__init__.py:create_app`):
```bash
uv run python -m uvicorn obscura.server:create_app --factory --host 0.0.0.0 --port 8080
```
- Docker (multi-stage; uses uv to build a venv):
```bash
docker build -t obscura:dev .
docker run --rm -p 8080:8080 obscura:dev
```
- Makefile (docker-compose envs via `scripts/compose-env.sh`):
```bash
make dev-up     # or dev-down / dev-restart / dev-logs / dev-watch
make staging-up # similarly: staging-*, prod-*
make dist       # build sdist + wheel with uv
make lint       # ruff lint + format check
make typecheck  # pyright
make test       # pytest -m "not e2e"
```

## 2) Architecture

- CLI (Click-based REPL): Entry point `obscura.cli:main` (`obscura/cli/__init__.py`). Handles backends, model selection, sessions, tool toggles, and workspace subcommands. Rendering, prompts, and commands live under `obscura/cli/` (e.g., `commands.py`, `repl.py`, `render.py`).
- Tooling Core: Unified tool system in `obscura/core/tools.py`:
  - `@tool(...)` decorator produces `ToolSpec` objects.
  - `ToolRegistry` maintains canonical names and a large alias map (maps common/hallucinated names to real tools).
  - Tool calls are traced/metric’d via OpenTelemetry helpers.
- Providers (LLM backends): Implementations in `obscura/providers/` (e.g., `copilot.py`, `claude.py`, `openai.py`, `localllm.py`). Backends expose a common protocol, tool routing, and streaming.
- Server (HTTP API): FastAPI app factory in `obscura/server/__init__.py` wires middleware (CORS, auth, rate limiting, telemetry), mounts MCP + A2A routers when enabled, and aggregates route modules from `obscura/routes/`.
- Config: Central config model `ObscuraConfig` in `obscura/core/config.py` resolves environment variables (auth, rate limits, cache, telemetry, A2A, Kairos flags, etc.).
- Memory & Storage:
  - Vector memory and local key-value memory under `obscura/vector_memory/` and `obscura/memory/`.
  - The CLI persists event-sourced sessions to `~/.obscura/events.db` (see README Session Storage).
- Integrations: `obscura/integrations/` contains MCP server, A2A transport+store, and other provider bridges. Telemetry lives in `obscura/telemetry/` (metrics, traces, middleware).

Data Flow (typical REPL turn): CLI → Backend (provider) → ToolRouter/ToolRegistry → Tool handler → (optional) web/file/git/etc. Tools → streamed tokens/events recorded to session store.

## 3) Key Patterns

- Language & Style: Python 3.13, type hints throughout. Pydantic models for config and schemas. Prefer absolute imports (`obscura.*`).
- Tools: Define via `@tool` in `obscura/core/tools.py`; keep parameters JSON‑schema friendly. Aliases are heavily normalized, so prefer canonical names when calling from agents.
- Telemetry: Tool calls wrapped with OTel spans and metrics; server can enable OTel via env (`OTEL_ENABLED`, etc.).
- Config via env: See `obscura/core/config.py` for supported vars (e.g., `OBSCURA_AUTH_ENABLED`, rate limits, A2A settings, Kairos toggles, undercover mode).
- CLI Conventions: Click commands/options in `obscura/cli/__init__.py` (e.g., `--backend`, `--model`, `--tools on|off`, `--confirm`, workspace subcommands).
- GitNexus (for contributors/agents editing code): AGENTS.md documents GitNexus workflows (impact analysis, detect changes, safe renames). Use those when modifying symbols or refactoring.

## 4) Testing

- Unit tests (default):
```bash
pytest tests/ -v -m "not e2e"
```
- Coverage and type/lint:
```bash
pytest tests/ --cov=obscura --cov-report=term-missing
pyright
ruff check .
ruff format --check .
```
- E2E tests (require server):
```bash
export OBSCURA_URL=http://localhost:8080
export OBSCURA_TOKEN=local-dev-token
pytest tests/e2e/ -v --run-e2e
# or
./scripts/run-e2e-tests.sh
```
- Pytest config & markers: See `pyproject.toml` (`testpaths`, `markers`), and `tests/README.md` for structure and examples. Coverage target (report) is configured; `fail_under` is set to 85.

## Notes

- Docker image exposes port 8080; health check probes `/health`.
- Package entry points (pyproject): `obscura` (CLI) and `obscura-mcp` (MCP server).
- Node tool `@mermaid-js/mermaid-cli` is present in `package.json` (for docs/diagrams).
- This document avoids speculation and reflects files in this repo: README.md, pyproject.toml, Makefile, Dockerfile, tests/README.md, and key modules under `obscura/`.
