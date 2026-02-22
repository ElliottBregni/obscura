# Memory System

Per-user key-value storage for AI agents. Each user gets an isolated SQLite database identified by JWT `user_id`.

## How It Works

- Each authenticated user maps to a unique SQLite file: `~/.obscura/memory/<sha256(user_id)[:16]>.db`
- Data is organized into **namespaces** (e.g., `session`, `agent:runtime`, `default:tasks`)
- Optional **TTL** for ephemeral data (auto-expires on read)
- Thread-safe singleton per user via `MemoryStore.for_user(user)`

## Schema

```sql
CREATE TABLE memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,          -- JSON-serialized
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    UNIQUE(namespace, key)
);
```

Indexes on `(namespace, key)` and `(expires_at)`.

## Python SDK

```python
from obscura.memory import MemoryStore
from obscura.auth.models import AuthenticatedUser

store = MemoryStore.for_user(user)

# Store
store.set("last_session", {"repo": "obscura", "file": "server.py"}, namespace="session")

# Retrieve
value = store.get("last_session", namespace="session")

# With TTL (auto-expires)
from datetime import timedelta
store.set("temp", "value", namespace="cache", ttl=timedelta(minutes=5))

# Delete
store.delete("last_session", namespace="session")

# List keys
keys = store.list_keys(namespace="session")

# Text search
results = store.search("database")

# Stats
stats = store.get_stats()
# {"total_keys": 42, "namespaces": {"session": 10, "agent:runtime": 5}, "db_path": "..."}

# Cleanup
store.clear_namespace("cache")
store.clear_expired()
```

## HTTP API

All endpoints require `Authorization: Bearer <token>`.

```bash
# Store a value
curl -X POST http://localhost:8080/api/v1/memory/session/context \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"value": {"repo": "obscura"}}'

# Store with TTL (seconds)
curl -X POST "http://localhost:8080/api/v1/memory/cache/temp?ttl=300" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"value": "expires in 5 minutes"}'

# Retrieve
curl http://localhost:8080/api/v1/memory/session/context \
  -H "Authorization: Bearer $TOKEN"

# Delete
curl -X DELETE http://localhost:8080/api/v1/memory/session/context \
  -H "Authorization: Bearer $TOKEN"

# List keys
curl "http://localhost:8080/api/v1/memory?namespace=session" \
  -H "Authorization: Bearer $TOKEN"

# Search
curl "http://localhost:8080/api/v1/memory/search?q=repo" \
  -H "Authorization: Bearer $TOKEN"

# Stats
curl http://localhost:8080/api/v1/memory/stats \
  -H "Authorization: Bearer $TOKEN"

# Namespaces
curl http://localhost:8080/api/v1/memory/namespaces \
  -H "Authorization: Bearer $TOKEN"

# Atomic transaction (multiple ops)
curl -X POST http://localhost:8080/api/v1/memory/transaction \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"operations": [
    {"op": "set", "namespace": "session", "key": "a", "value": 1},
    {"op": "delete", "namespace": "cache", "key": "old"}
  ]}'

# Export / Import
curl http://localhost:8080/api/v1/memory/export -H "Authorization: Bearer $TOKEN"
curl -X POST http://localhost:8080/api/v1/memory/import \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d @memory_backup.json
```

## Namespace Conventions

| Namespace | Purpose | Example Data |
|-----------|---------|--------------|
| `agent:runtime` | Agent state (status, iterations, errors) | `{"agent_id": "agent-abc", "status": "RUNNING"}` |
| `default:tasks` | Task prompts and results | `{"prompt": "Review this PR", "result": "..."}` |
| `session` | Conversation transcripts | `[{"role": "user", "content": "hello"}, ...]` |
| `project:{name}` | Project-specific context | `{"tech_stack": "python+fastapi"}` |
| `cache` | Ephemeral data (use with TTL) | API responses, computed values |
| `passthrough` | CLI passthrough results | `{"vendor": "claude", "transcript": "..."}` |

These are conventions, not enforced. Any string is a valid namespace.

## How Agents Use Memory

Agents automatically persist state to the `agent:runtime` namespace:

```json
{
  "agent_id": "agent-9294711e",
  "name": "repo-task-6d79444c3cf3",
  "status": "STOPPED",
  "created_at": "2026-02-21T22:25:50.234788+00:00",
  "updated_at": "2026-02-21T22:26:24.699468+00:00",
  "iteration_count": 1,
  "error_message": null
}
```

Task prompts and results go to `default:tasks`:

```json
{
  "prompt": "Workflow step: Classify ticket\nTask: Investigate 502 errors",
  "context": {},
  "started_at": "2026-02-21T22:23:42.806175+00:00",
  "mode": "agent_loop"
}
```

This state survives server restarts. Agent instances must be re-spawned, but their history is preserved.

## Multi-Tenancy

Users never see each other's data:

```python
store_a = MemoryStore.for_user(user_a)
store_b = MemoryStore.for_user(user_b)

store_a.set("key", "A's value")
store_b.set("key", "B's value")

assert store_a.get("key") == "A's value"  # Isolated
assert store_b.get("key") == "B's value"  # Isolated
```

## Global Memory

For org-wide shared data:

```python
from obscura.memory import GlobalMemoryStore

global_store = GlobalMemoryStore.get_instance()
global_store.set("standards", {"python": "black"}, namespace="org")
```

Stored at `~/.obscura/memory/global.db`. Accessible to all users.

## Vector Memory

For semantic search beyond keyword matching, use the vector memory extension:

```python
# CLI
obscura vector remember "Auth uses JWT with RS256 via Zitadel"
obscura vector recall "how does authentication work?"
```

Uses embeddings and cosine similarity to find related memories even when keywords don't match.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OBSCURA_MEMORY_DIR` | `~/.obscura/memory` | Storage directory |
