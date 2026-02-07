# Shared Memory — Multi-Tenant Agent Memory

> Auth-scoped key-value storage for AI agents. Like a "git for agent memory."

---

## Overview

Obscura's memory system provides **isolated, per-user storage** that agents can read from and write to. Think of it as a shared database where:

- Each user gets their own isolated SQLite database (identified by JWT `user_id`)
- Agents can store context, preferences, conversation history
- Data is organized into **namespaces** (e.g., `session`, `project`, `user`)
- Optional **TTL** for ephemeral data
- Full **audit logging** for compliance

---

## Use Cases

### 1. **Conversation Memory**
```python
# Agent remembers what you were working on
store.set("last_session", {
    "repo": "obscura",
    "file": "sdk/server.py",
    "task": "adding memory endpoints"
}, namespace="session")
```

### 2. **User Preferences**
```python
# Store user preferences across sessions
store.set("preferences", {
    "default_backend": "claude",
    "code_style": "black",
    "test_framework": "pytest"
}, namespace="user")
```

### 3. **Project Context**
```python
# Shared context for a specific project
store.set("tech_stack", {
    "language": "python",
    "framework": "fastapi",
    "database": "sqlite"
}, namespace="project:obscura")
```

### 4. **Agent Coordination**
```python
# Agent A writes, Agent B reads
store.set("task_status", {
    "agent": "code-review",
    "status": "complete",
    "result": "..."
}, namespace="coordination", ttl=timedelta(hours=1))
```

---

## API Reference

### Python SDK

```python
from sdk.memory import MemoryStore
from sdk.auth.models import AuthenticatedUser

# Get store for current user
store = MemoryStore.for_user(user)

# Store a value
store.set("mykey", {"foo": "bar"}, namespace="session")

# Retrieve a value
value = store.get("mykey", namespace="session")

# With TTL (auto-expires)
from datetime import timedelta
store.set("temp", "value", namespace="cache", ttl=timedelta(minutes=5))

# Delete
store.delete("mykey", namespace="session")

# List all keys in namespace
keys = store.list_keys(namespace="session")

# Search
results = store.search("database")

# Get stats
stats = store.get_stats()
```

### HTTP API

All endpoints require authentication (JWT token in `Authorization` header).

#### Get Value
```bash
curl http://localhost:8080/api/v1/memory/session/last_context \
  -H "Authorization: Bearer $TOKEN"
```
Response:
```json
{
  "namespace": "session",
  "key": "last_context",
  "value": {"repo": "obscura", "file": "server.py"}
}
```

#### Set Value
```bash
curl -X POST http://localhost:8080/api/v1/memory/session/last_context \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"value": {"repo": "obscura", "file": "server.py"}}'
```

#### Set with TTL (seconds)
```bash
curl -X POST "http://localhost:8080/api/v1/memory/cache/temp?ttl=300" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"value": "expires in 5 minutes"}'
```

#### Delete Value
```bash
curl -X DELETE http://localhost:8080/api/v1/memory/session/last_context \
  -H "Authorization: Bearer $TOKEN"
```

#### List Keys
```bash
curl http://localhost:8080/api/v1/memory?namespace=session \
  -H "Authorization: Bearer $TOKEN"
```

#### Search
```bash
curl "http://localhost:8080/api/v1/memory/search?q=database" \
  -H "Authorization: Bearer $TOKEN"
```

#### Stats
```bash
curl http://localhost:8080/api/v1/memory/stats \
  -H "Authorization: Bearer $TOKEN"
```
Response:
```json
{
  "total_keys": 42,
  "expired_keys": 3,
  "namespaces": {
    "session": 10,
    "user": 5,
    "project:obscura": 27
  },
  "db_path": "/Users/elliott/.obscura/memory/<hash>.db"
}
```

---

## Namespaces

Namespaces organize memory. Common patterns:

| Namespace | Purpose | Example |
|-----------|---------|---------|
| `session` | Current conversation/session | Last file edited, current task |
| `user` | User preferences | Default model, code style |
| `project:{name}` | Project-specific context | Tech stack, architecture decisions |
| `cache` | Ephemeral data | API responses, computed values |
| `coordination` | Agent-to-agent communication | Task assignments, status |

---

## Multi-Tenancy

Each user gets **isolated storage**:

```python
# User A's data
user_a = AuthenticatedUser(user_id="u-1", ...)
store_a = MemoryStore.for_user(user_a)
store_a.set("key", "A's value", namespace="test")

# User B's data  
user_b = AuthenticatedUser(user_id="u-2", ...)
store_b = MemoryStore.for_user(user_b)
store_b.set("key", "B's value", namespace="test")

# They don't see each other's data!
assert store_a.get("key", namespace="test") == "A's value"
assert store_b.get("key", namespace="test") == "B's value"
```

Storage location: `~/.obscura/memory/<user_hash>.db`

---

## Global Memory (Shared)

For organization-wide knowledge:

```python
from sdk.memory import GlobalMemoryStore

global_store = GlobalMemoryStore.get()
global_store.set("standards", {"python": "black", "js": "prettier"}, namespace="org")
```

All users can read, but writes are audited.

---

## Architecture

```
User Authenticated
       │
       ▼
┌───────────────┐
│  MemoryStore  │  Singleton per user_id
│   for_user()  │
└───────┬───────┘
        │
   ┌────┴────┐
   ▼         ▼
SQLite    SQLite     (isolated per user)
┌─────┐   ┌─────┐
│u-1  │   │u-2  │
│.db  │   │.db  │
└─────┘   └─────┘
```

---

## Testing

```bash
# Run memory tests
uv run pytest tests/test_memory.py -v

# Test with coverage
uv run pytest tests/test_memory.py --cov=sdk.memory --cov-report=html
```

---

## Vector Memory (Semantic Search)

For semantic retrieval beyond keyword matching, see [Vector Memory](VECTOR_MEMORY.md). Vector memory extends this store with embeddings and cosine similarity search — agents can find related memories even when keywords don't match.

---

## Future Enhancements

- **Memory pruning** — Auto-delete old/expired entries
- **Cross-user sharing** — Shared namespaces with permissions
- **Replication** — Sync memory across devices
- **Memory import/export** — Backup/restore user memory
