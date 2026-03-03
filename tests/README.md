# Testing Guide — Obscura

> Complete testing strategy: unit, integration, and end-to-end tests.

---

## Test Structure

```
tests/
├── conftest.py              # Shared fixtures
├── test_*.py               # Unit tests (existing)
├── test_memory.py          # Memory system tests
├── test_agents.py          # Agent runtime tests
├── test_vector_memory.py   # Vector memory tests
├── test_websockets.py      # WebSocket protocol tests
├── test_cli.py             # CLI tests
└── e2e/                    # End-to-end tests
    ├── conftest.py         # E2E fixtures and config
    └── test_agent_workflows.py  # Full workflow tests
```

---

## Quick Start

### Run Unit Tests (Fast)

```bash
cd ~/dev/obscura
uv run pytest tests/ -v -m "not e2e"
```

### Run All Tests

```bash
# Requires server running on localhost:8080
export OBSCURA_URL=http://localhost:8080
export OBSCURA_TOKEN=local-dev-token
uv run pytest tests/ -v --run-e2e
```

### Run with Auto-Started Server

```bash
# Script starts server, runs tests, stops server
./scripts/run-e2e-tests.sh
```

---

## Test Categories

### 1. Unit Tests (`test_*.py`)

Fast, isolated tests with mocked dependencies.

```bash
uv run pytest tests/test_memory.py -v
uv run pytest tests/test_agents.py -v
uv run pytest tests/test_cli.py -v
```

**Coverage targets:**
- Memory: >90%
- Agents: >85%
- CLI: >80%

### 2. Integration Tests

Tests with real dependencies but mocked external services.

```bash
# These run with unit tests by default
uv run pytest tests/test_server.py -v
uv run pytest tests/test_telemetry_*.py -v
```

### 3. End-to-End Tests (`tests/e2e/`)

Full system tests requiring a running server.

```bash
# Start server first
uv run python -m uvicorn sdk.server:create_app --factory --port 8080 &

# Run e2e tests
uv run pytest tests/e2e/ -v --run-e2e

# Or use the helper script
./scripts/run-e2e-tests.sh
```

---

## E2E Test Coverage

### Agent Lifecycle

| Test | Description |
|------|-------------|
| `test_health_check` | Server health endpoint |
| `test_spawn_agent` | Agent creation |
| `test_run_agent_task` | Task execution |
| `test_list_agents` | Agent listing |
| `test_agent_status_after_spawn` | Status tracking |
| `test_stop_agent` | Agent cleanup |

### Memory Operations

| Test | Description |
|------|-------------|
| `test_set_and_get_memory` | Basic CRUD |
| `test_memory_not_found` | 404 handling |
| `test_delete_memory` | Deletion |
| `test_list_memory_keys` | Key enumeration |
| `test_search_memory` | Text search |

### Vector Memory

| Test | Description |
|------|-------------|
| `test_remember_and_recall` | Semantic search |

### Error Handling

| Test | Description |
|------|-------------|
| `test_404_agent_not_found` | Missing resource |
| `test_invalid_agent_id_format` | Input validation |
| `test_unauthorized_request` | Auth failure |

### Workflows

| Test | Description |
|------|-------------|
| `test_full_agent_workflow` | Complete lifecycle |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSCURA_URL` | `http://localhost:8080` | Server URL for e2e tests |
| `OBSCURA_TOKEN` | `local-dev-token` | Auth token |

---

## Markers

Use markers to run specific test types:

```bash
# Only unit tests
pytest -m "not e2e"

# Only e2e tests
pytest -m e2e --run-e2e

# Skip slow tests
pytest -m "not slow"
```

---

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Tests

on: [push, pull_request]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: astral-sh/setup-uv@v2
      - run: uv run pytest tests/ -v -m "not e2e"

  e2e-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: astral-sh/setup-uv@v2
      - run: ./scripts/run-e2e-tests.sh
```

---

## Writing New Tests

### Unit Test Template

```python
import pytest
from unittest.mock import MagicMock

def test_feature():
    """Test description."""
    # Arrange
    mock_client = MagicMock()
    
    # Act
    result = some_function(mock_client)
    
    # Assert
    assert result == expected
```

### E2E Test Template

```python
import pytest
import httpx
import os

BASE_URL = os.environ.get("OBSCURA_URL", "http://localhost:8080")
TOKEN = os.environ.get("OBSCURA_TOKEN", "local-dev-token")

@pytest.mark.e2e
def test_feature(client):
    """E2E test description."""
    resp = client.get("/api/v1/something")
    assert resp.status_code == 200
```

---

## Troubleshooting

### Server Not Found

```bash
# Error: Connection refused
# Solution: Start server first
uv run python -m uvicorn sdk.server:create_app --factory --port 8080
```

### Tests Skipped

```bash
# E2E tests skipped without --run-e2e
pytest tests/e2e/ --run-e2e

# Or set environment
export OBSCURA_URL=http://localhost:8080
pytest tests/e2e/
```

### Import Errors

```bash
# Install all dependencies
uv sync --extra dev --extra server --extra telemetry
```

---

## Coverage Report

```bash
# Generate coverage
uv run pytest tests/ --cov=sdk --cov-report=html --cov-report=term

# View HTML report
open htmlcov/index.html
```

---

## Test Data Cleanup

E2E tests use `e2e` namespace for memory to avoid conflicts:

```python
# Tests clean up after themselves
client.delete("/api/v1/memory/e2e/test-key")
```

---

## Performance Testing

```bash
# Load test (requires locust)
pip install locust
locust -f tests/performance/locustfile.py
```

---

## Questions?

See `docs/TESTING.md` for more details.
