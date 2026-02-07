# Testing Guide — Obscura SDK

> How to run tests, common issues, and debugging tips.

---

## Quick Start

```bash
cd ~/dev/obscura

# Install all dependencies (dev + server + telemetry)
uv sync --extra dev --extra server --extra telemetry

# Run all tests
uv run pytest tests/ -v

# Run with coverage report
uv run pytest tests/ --cov=sdk --cov-report=html

# Run specific test file
uv run pytest tests/test_server.py -v

# Run with specific markers
uv run pytest tests/ -v -k "telemetry"
```

---

## Test Suite Overview

| Test File | Coverage | Purpose |
|-----------|----------|---------|
| `test_server.py` | FastAPI API endpoints | Health, send, stream, sessions, sync endpoints |
| `test_auth_middleware.py` | JWT auth | JWKS cache, token validation, role extraction |
| `test_rbac.py` | Role-based access | `require_role()`, `require_any_role()`, 401/403 responses |
| `test_memory.py` | Shared memory | Multi-tenant storage, namespaces, TTL, search |
| `test_telemetry_traces.py` | OpenTelemetry traces | `@traced` decorator, span export, NoOp fallback |
| `test_telemetry_metrics.py` | OTel metrics | Counters, histograms, lazy initialization |
| `test_telemetry_hooks.py` | Hook integration | 6 HookPoints, context token lifecycle |
| `test_telemetry_audit.py` | Audit logging | JSONL events, trace correlation, immutability |
| `test_sdk_client.py` | SDK client | Backend selection, model resolution |
| `test_sdk_tools.py` | Tool registry | `@tool` decorator, schema inference |
| `test_sdk_stream.py` | Streaming | SSE format, chunk handling, metrics |

---

## Common Test Patterns

### Bypassing Auth in Tests

When testing validation logic (e.g., empty prompt rejection), you need to bypass the auth middleware:

```python
from sdk.auth.rbac import get_current_user
from sdk.auth.models import AuthenticatedUser

_TEST_USER = AuthenticatedUser(
    user_id="u-test-123",
    email="test@obscura.dev",
    roles=("admin",),
    org_id="org-1",
    token_type="user",
    raw_token="fake-token",
)

def test_something_with_validation():
    app = create_app(config)
    # Override auth dependency
    app.dependency_overrides[get_current_user] = lambda: _TEST_USER
    client = TestClient(app)
    # Now your test runs without JWT middleware interfering
```

### Working with ContentBlock

The `ContentBlock` dataclass uses `kind` (not `type`) to avoid shadowing Python's built-in:

```python
from sdk._types import ContentBlock

# ✅ Correct
block = ContentBlock(kind="text", text="Hello!")

# ❌ Wrong (will raise TypeError)
block = ContentBlock(type="text", text="Hello!")
```

### OpenTelemetry in Tests

OTel uses global singletons. Tests must handle "already initialized" gracefully:

```python
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

reader = InMemoryMetricReader()
provider = MeterProvider(metric_readers=[reader])

# Try to set, but don't fail if already set
try:
    metrics.set_meter_provider(provider)
except Exception:
    pass  # Already initialized by another test

# Use provider directly, not the global
meter = provider.get_meter("test")
```

### Correct OTel Imports

| Class | Import Path |
|-------|-------------|
| `InMemorySpanExporter` | `opentelemetry.sdk.trace.export.in_memory_span_exporter` |
| `InMemoryMetricReader` | `opentelemetry.sdk.metrics.export` |
| `SimpleSpanProcessor` | `opentelemetry.sdk.trace.export` |

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'jose'`

```bash
# Install server extras (includes python-jose)
uv add --dev python-jose
```

### `ModuleNotFoundError: No module named 'fastapi'`

```bash
# Install server extras
uv add --dev fastapi uvicorn sse-starlette
```

### `TypeError: ContentBlock.__init__() got an unexpected keyword argument 'type'`

Use `kind=` instead of `type=` when creating `ContentBlock` instances.

### Tests fail with 401 Unauthorized

Add auth dependency override to bypass JWT middleware (see pattern above).

### OTel tests fail with "already initialized"

Use try/except around `set_meter_provider()` and access meter via `provider.get_meter()` instead of `metrics.get_meter()`.

---

## CI/CD Test Matrix

Tests should pass on:

| Python | OS | Auth | OTel |
|--------|-----|------|------|
| 3.12 | macOS | enabled | enabled |
| 3.12 | Linux | enabled | enabled |
| 3.12 | Linux | disabled | disabled |
| 3.13 | macOS | enabled | enabled |

---

## Phase 1 Test Checklist

Before submitting MR:

- [ ] `uv run pytest tests/ -v` passes (364 tests)
- [ ] `test_auth_middleware.py` - JWT validation, JWKS cache
- [ ] `test_rbac.py` - Role enforcement
- [ ] `test_server.py` - All 8 routes, health/ready 200
- [ ] `test_telemetry_*.py` - Traces, metrics, hooks, audit
- [ ] `test_sdk_*.py` - Client, tools, stream

---

## See Also

- [Platform Architecture](PLATFORM-ARCHITECTURE.md) — Full stack overview
- [UAT Guide](../UAT-GUIDE.md) — End-to-end testing with Docker/K8s
