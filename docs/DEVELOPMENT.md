# Development Guide

## Setup

```bash
git clone <repo-url>
cd obscura-main

# Install all dependencies
pip install -e ".[dev,server,telemetry,tui]"
# or
uv pip install -e ".[dev,server,telemetry,tui]"
```

Requires Python 3.13+.

## Running the Server

```bash
# Development (auth + telemetry disabled)
export OBSCURA_AUTH_ENABLED=false
export OTEL_ENABLED=false
obscura serve --port 8080

# With reload
obscura serve --port 8080 --reload
```

## Tests

### Organization

Tests mirror source structure exactly:

```
tests/
  unit/
    obscura/
      core/           # types, client, config, auth, stream, sessions, tools, agent_loop
      providers/      # claude, copilot, openai, localllm
      agents/         # agent runtime, lifecycle, communication
      tools/          # policy evaluation
      integrations/
        mcp/          # MCP client, server
        a2a/          # A2A client, transports, tool adapter
      auth/           # middleware, RBAC, capabilities
      routes/         # API endpoint tests
      memory/         # MemoryStore tests
      vector_memory/  # Semantic search
      telemetry/      # OpenTelemetry integration
      tui/            # Terminal UI
      cli/            # CLI tests
  integration/        # Cross-module integration tests
  e2e/                # Full system tests (requires server)
```

### Running Tests

```bash
# Unit tests (fast, no server needed)
pytest tests/ -v -m "not e2e"

# E2E tests
./scripts/run-e2e-tests.sh

# All tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=obscura --cov-report=term-missing --cov-fail-under=85

# Specific module
pytest tests/unit/obscura/core/ -v
pytest tests/unit/obscura/tools/test_policy.py -v
```

### Test Markers

| Marker | Description |
|--------|-------------|
| `@pytest.mark.unit` | Fast unit tests (default) |
| `@pytest.mark.integration` | Cross-module integration |
| `@pytest.mark.e2e` | Full system tests (slow, require server) |
| `@pytest.mark.asyncio` | Async test support (auto mode) |

### Coverage

Minimum 85% coverage enforced in CI. Omits: `obscura/tui/*`.

### Test Patterns

**MockBackend** for testing agent loops:

```python
class MockBackend(BackendProtocol):
    async def stream(self, prompt, **kwargs):
        for chunk in self._turns[self._call_count]:
            yield chunk
```

**Autouse fixtures** prevent test pollution:

```python
@pytest.fixture(autouse=True)
def reset_shared_state():
    """Reset singletons between tests."""
    MemoryStore.reset_instances()
    yield
```

## Quality Checks

```bash
# Type checking (strict mode)
pyright

# Linting
ruff check .

# Formatting
ruff format --check .
ruff format .  # auto-fix
```

All three must pass for CI to be green.

## CI/CD

Three GitHub Actions workflows:

| Workflow | Trigger | Steps |
|----------|---------|-------|
| `test.yml` | push/PR to main | pytest (unit + e2e), import verification |
| `ruff.yml` | push/PR to main | `ruff check .`, `ruff format --check .` |
| `pyright.yml` | push/PR to main | `pyright` (strict mode) |

## Docker

### Build

```bash
docker build -t obscura .
```

Multi-stage build:
1. **Builder** -- Compiles venv with `uv` (frozen deps, no dev extras)
2. **Runtime** -- Minimal Python 3.13 slim, non-root user, health check

### Full Stack

```bash
./scripts/compose-env.sh dev up --watch

# or include host OAuth passthrough for dev
./scripts/dev-compose-oauth-up.sh

# environment overlays
./scripts/compose-env.sh staging up -d --build
./scripts/compose-env.sh prod up -d --build
```

### SDLC Targets

```bash
make dev-up
make dev-check
make dev-auth-fix

make staging-up
make staging-check

make prod-up
make prod-check
```

Services:

| Service | Port | Description |
|---------|------|-------------|
| `obscura-sdk` | 8080 | Obscura API server |
| `web-ui` | 5173 | React admin portal (includes `/approvals`) |
| `redis` | 6379 | A2A pub/sub |
| `zitadel` | 8081 | OIDC auth provider |
| `cockroachdb` | 26257 | Identity store (for Zitadel) |
| `otel-collector` | 4317 | OpenTelemetry collector |
| `jaeger` | 16686 | Trace visualization |
| `prometheus` | 9090 | Metrics |
| `grafana` | 3000 | Dashboards |

## PR Requirements

1. `pyright` -- 0 errors, 0 warnings
2. `ruff check .` -- clean
3. `pytest tests/unit/` -- all pass
4. Module-specific tests for changed modules

### Module Ownership

| Module | Owner | Required Tests |
|--------|-------|----------------|
| `obscura.core` | core-team | `tests/unit/obscura/core/` |
| `obscura.providers` | core-team | `tests/unit/obscura/providers/` |
| `obscura.auth` | security-team | `tests/unit/obscura/auth/` |
| `obscura.memory` | core-team | `tests/unit/obscura/memory/` |
| `obscura.tools` | tools-team | `tests/unit/obscura/tools/` |
| `obscura.tools.policy` | security-team | `tests/unit/obscura/tools/test_policy.py` |
| `obscura.integrations.mcp` | integrations-team | `tests/unit/obscura/integrations/mcp/` |
| `obscura.integrations.a2a` | integrations-team | `tests/unit/obscura/integrations/a2a/` |
| `obscura.agent` | core-team | `tests/unit/obscura/agents/` |
| `obscura.telemetry` | observability-team | `tests/unit/obscura/telemetry/` |

### Change Policies

| Tier | Policy |
|------|--------|
| **Stable** (`core`, `providers`, `auth`, `memory`) | Breaking changes require RFC + migration guide. Semver major bump. |
| **Beta** (`tools`, `mcp`, `agent`, `server`, `cli`, `tui`) | Breaking changes require changelog. Semver minor bump. |
| **Experimental** (`a2a`, `openclaw_bridge`, `parity`) | Breaking changes allowed without notice. |

## Project Configuration

Key files:

| File | Description |
|------|-------------|
| `pyproject.toml` | Package metadata, dependencies, tool config |
| `feature_tiers.yaml` | Module stability declarations |
| `parity_matrix.md` | Backend feature support matrix |
| `ownership.md` | Module owners and required tests |
| `config/mcp-config.json` | MCP server configuration template |
| `Dockerfile` | Multi-stage production build |
| `docker-compose.base.yml` | Shared stack definition for all environments |
| `docker-compose.dev.yml` | Local development overlay |
| `docker-compose.staging.yml` | Staging overlay |
| `docker-compose.prod.yml` | Production overlay |
| `config/env/*.env` | Environment-specific runtime defaults |
