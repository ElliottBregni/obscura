# Vector Memory — Semantic Search for Agents

> Store memories with embeddings and retrieve them by meaning, not just keywords.

---

## Overview

Vector Memory extends Obscura's memory system with **semantic search**. Instead of exact keyword matching, agents can find related memories even when the wording is different.

- **Automatic embedding** — Text is embedded on storage
- **Cosine similarity search** — Find semantically similar memories
- **Metadata storage** — Attach context to each memory
- **Namespace isolation** — Separate semantic spaces
- **SemanticMemoryMixin** — Drop-in integration for agents

---

## Quick Start

### Python SDK

```python
from sdk.vector_memory import VectorMemoryStore

store = VectorMemoryStore.for_user(user)

# Store with automatic embedding
store.set("python_async", "Async/await is Python's way to handle concurrency...")
store.set("threading_guide", "Python threading is best for I/O-bound tasks...")
store.set("multiprocessing", "Use multiprocessing for CPU-bound parallelism...")

# Semantic search — finds related memories even without keyword match
results = store.search_similar("how do I run multiple things at once?", top_k=3)
for r in results:
    print(f"  {r.key}: score={r.score:.2f} — {r.text[:60]}")
```

### CLI

```bash
# Store a memory with semantic embedding
obscura vector remember "Python async/await handles concurrency with an event loop"

# Recall by meaning
obscura vector recall "how to handle parallel tasks" --top-k 3
```

### HTTP API

```bash
# Store with embedding
curl -X POST http://localhost:8080/api/v1/vector-memory/docs/python-async \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "Async/await is Python'\''s concurrency model", "metadata": {"topic": "python"}}'

# Semantic search
curl "http://localhost:8080/api/v1/vector-memory/search?q=run+things+in+parallel&top_k=3" \
  -H "Authorization: Bearer $TOKEN"
```

Response:
```json
{
  "query": "run things in parallel",
  "results": [
    {
      "namespace": "docs",
      "key": "python-async",
      "text": "Async/await is Python's concurrency model",
      "score": 0.87,
      "metadata": {"topic": "python"}
    }
  ],
  "count": 1
}
```

---

## Agent Integration — SemanticMemoryMixin

Add semantic memory to any agent with the mixin:

```python
from sdk.vector_memory import SemanticMemoryMixin

# The mixin adds remember() and recall() to agents
agent.remember("The auth module uses JWT with RS256 signing")
agent.remember("Rate limiting is handled by slowapi middleware")

# Later, the agent can recall relevant context
results = agent.recall("how is authentication implemented?")
# Returns the JWT memory even though "JWT" wasn't in the query
```

---

## Embedding Configuration

By default, Obscura uses a simple hash-based embedding for demo/testing. For production, swap in a real embedding model:

```python
# Option 1: OpenAI embeddings
import openai
def openai_embedding(text: str) -> list[float]:
    response = openai.embeddings.create(model="text-embedding-3-small", input=text)
    return response.data[0].embedding

store = VectorMemoryStore.for_user(user, embedding_fn=openai_embedding)

# Option 2: sentence-transformers (local)
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("all-MiniLM-L6-v2")
def local_embedding(text: str) -> list[float]:
    return model.encode(text).tolist()

store = VectorMemoryStore.for_user(user, embedding_fn=local_embedding)
```

---

## API Reference

### VectorMemoryStore

| Method | Description |
|--------|-------------|
| `for_user(user, embedding_fn=None)` | Get/create store for user |
| `set(key, text, metadata=None, namespace="default", ttl=None)` | Store with embedding |
| `get(key, namespace="default")` | Get by exact key |
| `search_similar(query, namespace=None, top_k=5, threshold=-1.0)` | Semantic search |
| `delete(key, namespace="default")` | Delete entry |
| `list_keys(namespace=None)` | List all keys |
| `clear_namespace(namespace)` | Clear namespace |
| `get_stats()` | Usage statistics |

### HTTP Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/vector-memory/{namespace}/{key}` | Store with embedding |
| GET | `/api/v1/vector-memory/search?q={query}&top_k=N` | Semantic search |

---

## Architecture

```
User Query: "how to handle parallel tasks"
       │
       ▼
┌──────────────┐
│  Embedding   │  text → [0.12, -0.45, 0.78, ...]
│  Function    │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Cosine     │  Compare against all stored embeddings
│  Similarity  │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Top-K       │  Return most similar memories
│  Results     │
└──────────────┘
```

Storage: `~/.obscura/vector_memory/<user_hash>.db`

---

## Testing

```bash
# Run vector memory tests
uv run pytest tests/test_vector_memory.py -v

# With coverage
uv run pytest tests/test_vector_memory.py --cov=sdk.vector_memory --cov-report=html
```
