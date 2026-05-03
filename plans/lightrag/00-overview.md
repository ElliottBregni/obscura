# LightRAG-on-Obscura: Phased Implementation Plan

> **Status:** plan agreed in principle, phase docs in progress.
> **Owner:** Elliott Bregni (`bregnie34@gmail.com`)
> **Drafted:** 2026-04-26

This is the canonical overview for layering LightRAG-style graph-aware retrieval on top of Obscura's existing Qdrant + decay-based memory. The user's existing decay/lifecycle logic stays load-bearing — LightRAG owns vector + graph **retrieval**, Obscura still owns memory **lifecycle**.

The hybrid scoring model:

```
score = vector_similarity + graph_relevance + recency_decay + usage_frequency
```

---

## Existing-architecture findings (load-bearing facts)

The phase plans below depend on these specific shapes:

1. **`VectorMemoryStore`** (`obscura/vector_memory/vector_memory.py:163-613`) is a **per-user singleton** keyed on `user.user_id` (`for_user()` at line 306). Method-set:
   - **Writes:** `set(key, text, metadata, namespace, ttl, memory_type)` (line 324), `delete()`, `clear_namespace()`, `touch()`, `run_maintenance()`.
   - **Reads:** `search_similar()` (line 387), `search_reranked()` (line 450 — two-stage with `RecencyReranker`), `get()`, `list_keys()`.
   - **Backend:** delegates to a `VectorBackend` Protocol (`obscura/vector_memory/backends/base.py:42-108`) — this is the natural seam to slot LightRAG behind.

2. **Decay** is centralized in `obscura/vector_memory/decay.py`:
   - `DecayConfig` (line 68) with per-`memory_type` `DecayProfile` (line 32), loadable from `[vector_memory.decay]` in `~/.obscura/config.toml`.
   - `compute_decay()` (line 86) returns a multiplier in [0, 1] from `(memory_type, created_at, accessed_at, config)`.
   - **Decay is applied in two places**: inside `QdrantBackend.search_vectors` (multiplied into `final_score` server-side, lines 329-345) AND inside `search_reranked` (`vector_memory.py:506-512`). Anything that bypasses Qdrant's search must re-apply decay itself.

3. **Usage frequency is mostly aspirational.** `_touch_results_async` exists (`vector_memory.py:557`) but is **never called** from `search_similar` or `search_reranked`. Only `obscura/profile/store.py:118` and the explicit `.touch()` API bump `accessed_at`. So `usage_frequency` in the scoring formula has no real signal today other than the access_boost in `compute_decay`. Worth fixing as part of this work.

4. **Memory tools** (`obscura/tools/memory_tools.py:82-320`):
   - `store_memory` / `recall_memory` → KV (`MemoryStore`)
   - `store_searchable` / `semantic_search` → vector (`VectorMemoryStore`)
   - All call `VectorMemoryStore.for_user(user)` directly.

5. **Backend wiring into the agent loop**:
   - The supervisor has a hook scaffold at `obscura/core/supervisor/vector_memory_hook.py` (`register_vector_memory_hooks`) that injects results into `_vector_memory_context`. **But that function has no callers anywhere in the repo.** The hook is dormant.
   - Active wiring is via `SemanticMemoryMixin` on the legacy `Agent` class (`agents.py:1129-1167`, `_load_relevant_memory`) and the CLI bridge (`obscura/cli/vector_memory_bridge.py`).
   - Auth provisions a store on first login (`obscura/auth/middleware.py:58`).

6. **Config surface**:
   - Qdrant: `OBSCURA_VECTOR_BACKEND`, `OBSCURA_QDRANT_MODE` (`local`/`memory`/`cloud`), `OBSCURA_QDRANT_PATH`, `OBSCURA_QDRANT_URL`, `OBSCURA_QDRANT_API_KEY` (read in `vector_memory.py:225-273`).
   - Decay: `[vector_memory.decay]` in `~/.obscura/config.toml`.
   - Local Qdrant default: `~/.obscura/qdrant/`.

7. **Empty-vector bug**: `qdrant_backend.py:337` and `:450` construct `VectorEntry(..., embedding=[], ...)` after search. The full vector isn't fetched back (`with_vectors=False`). Anything downstream that needs the embedding gets an empty list. Not a crash, but a footgun. *Tracked separately as a spawn task.*

8. **`pyproject.toml`** already has `qdrant-client>=1.17.0`, `numpy>=2.1.0`, `httpx`, `openai`, `tiktoken`. LightRAG's hard deps are tiktoken, networkx, nano-vectordb, tenacity, pipmaster. **No conflicts**; `lightrag-hku` is the canonical PyPI name.

---

## Phase 0 — Confirm fit (done)

**Verdict: nothing structurally blocks layering LightRAG behind the existing memory interface.** Three caveats:

1. **The hybrid-score formula needs a re-applied decay step.** Currently the Qdrant backend bakes decay into `final_score` server-side. If LightRAG is doing the vector search instead of `QdrantBackend.search_vectors`, we lose that path and have to re-apply `compute_decay()` on whatever LightRAG returns. The right move is to make LightRAG's results flow through `search_reranked()`'s candidate stage.

2. **Embedding dimensionality lock-in.** LightRAG stores entity/relationship embeddings in its own vector store. Mixing dimensions with the existing `all-MiniLM-L6-v2` (384) means we either share the embedding model end-to-end or maintain two collections.

3. **The empty-vector quirk** (`qdrant_backend.py:337`): if we ever need to re-score retrieved chunks against the graph, we won't have the original embedding. Either change scroll calls to `with_vectors=True` (read cost) or recompute embeddings as needed. *Not a blocker; tracked separately.*

---

## Phase 1 — Dependency + scaffold

**Choice:** `lightrag-hku` from PyPI, with **NetworkX** as the graph backend and **reuse Qdrant** for LightRAG's vector storage.

- **NetworkX over Postgres+AGE / Neo4j**: Obscura runs locally, single-user-per-machine. NetworkX serializes to a pickled file in `working_dir` — zero ops cost. Personal-memory graph sizes stay well under the 100k-edge mark where NetworkX latency matters. If Obscura ever runs in shared/multi-tenant mode, swap to AGE without changing the integration layer.
- **Reuse Qdrant for LightRAG vector storage** rather than letting it default to nano-vectordb. The user already has Qdrant set up. LightRAG ships `QdrantVectorDBStorage` (use `vector_storage="QdrantVectorDBStorage"`).
- **Share the embedding function** with `_make_default_embedding_fn()` (`vector_memory.py:86`) — one model load, no dimension mismatches.

### Dependency add — optional extra in `pyproject.toml`

```toml
lightrag = [
    "lightrag-hku>=1.4",
    "networkx>=3.0",
]
```

### Module layout — new sibling module

```
obscura/lightrag_memory/
├── __init__.py
├── adapter.py        # LightRAGAdapter — owns the LightRAG instance per user
├── hybrid_store.py   # HybridVectorMemoryStore — wraps VectorMemoryStore
├── scoring.py        # hybrid_score() — combines vector + graph + decay + usage
├── ingest.py         # async fan-out helpers for write path
└── backfill.py       # CLI/maintenance entrypoint for migrating existing chunks
```

Sibling, not nested — keeps the existing 1700-line `vector_memory` package as the source of truth for decay/lifecycle. The new module imports from it but never the other way. Trivial to delete if abandoned.

---

## Phase 2 — Ingest path

### Write call sites (~10)

Direct callers of `VectorMemoryStore.set()`:

- `obscura/tools/memory_tools.py:179` (`store_searchable_impl`)
- `obscura/core/supervisor/vector_memory_hook.py:122` (dormant)
- `obscura/cli/vector_memory_bridge.py`
- `obscura/routes/vector_memory.py:41`
- `obscura/routes/session_ingest.py:188`, `obscura/routes/session_sync.py`
- `obscura/eval/memory.py:66`, `obscura/profile/store.py`, `obscura/kairos/vault_sync.py:471`
- `obscura/agent/agents.py` via `SemanticMemoryMixin.remember()`

**Don't rewire them.** Have the singleton `VectorMemoryStore.for_user()` return a `HybridVectorMemoryStore` when LightRAG is enabled — every existing caller transparently gains graph indexing.

### `HybridVectorMemoryStore` proposal

```python
# obscura/lightrag_memory/hybrid_store.py

class HybridVectorMemoryStore(VectorMemoryStore):
    """Drop-in subclass that fans writes out to LightRAG.
    Inherits the entire existing API; overrides only set/delete/search_*.
    Decay/consolidation/touch behavior unchanged.
    """

    def __init__(self, user, *, lightrag_adapter: LightRAGAdapter, **kw):
        super().__init__(user, **kw)
        self._lr = lightrag_adapter
        self._ingest_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix=f"lr-ingest-{user.user_id[:8]}")

    def set(self, key, text, metadata=None, namespace="default",
            ttl=None, memory_type="general") -> None:
        super().set(key, text, metadata, namespace, ttl, memory_type)
        if memory_type in self._lr.indexable_types:
            self._ingest_executor.submit(
                self._lr.insert_safe,
                doc_id=str(MemoryKey(namespace, key if isinstance(key, str) else key.key)),
                text=text,
                metadata={**(metadata or {}), "memory_type": memory_type,
                          "obscura_key": str(key), "obscura_namespace": namespace},
            )
```

### Critical decisions baked into the signature

1. **Async fan-out is the default.** LightRAG's `ainsert()` runs an LLM-based entity-extraction pipeline (seconds-per-doc). Blocking `vector_memory.set()` on that would stall every tool call.
2. **Selective indexing.** Default whitelist: `fact`, `summary`, `general`. Skip `episode` (turn chatter, consolidates away) and `preference` (already structured). Configurable.
3. **`obscura_key` / `obscura_namespace` carried as metadata** is the join key that lets the query path line LightRAG's hits up against the original `VectorEntry`.
4. **`delete()` override** also calls `lr_adapter.delete_safe(doc_id)` to keep the graph consistent.

### `LightRAGAdapter` shape

```python
class LightRAGAdapter:
    """Per-user LightRAG instance + cached event loop for async calls."""
    _instances: dict[str, "LightRAGAdapter"] = {}

    @classmethod
    def for_user(cls, user, embedding_fn) -> "LightRAGAdapter": ...

    def insert_safe(self, doc_id: str, text: str, metadata: dict) -> None: ...
    def delete_safe(self, doc_id: str) -> None: ...
    async def aquery(self, query: str, mode: str = "hybrid",
                     top_k: int = 20) -> list[GraphHit]: ...
```

`working_dir` lives at `~/.obscura/lightrag/<user_hash>/`.

---

## Phase 3 — Query path (hybrid score)

### Scoring function

```python
@dataclass(frozen=True)
class HybridWeights:
    vector: float = 0.5     # similarity
    graph:  float = 0.3     # graph_relevance from LightRAG
    decay:  float = 0.15    # recency_decay [0,1]
    usage:  float = 0.05    # log(1 + access_count) normalized

def hybrid_score(*, vector_sim, graph_relevance, decay_multiplier,
                 usage_count, weights):
    usage_norm = math.log1p(usage_count) / math.log1p(100)  # saturate at ~100
    return (
        weights.vector * vector_sim
        + weights.graph * graph_relevance
        + weights.decay * decay_multiplier
        + weights.usage * min(usage_norm, 1.0)
    )
```

**Default weights:** `vector=0.5, graph=0.3, decay=0.15, usage=0.05`. Tunable via `[vector_memory.lightrag.weights]` in `~/.obscura/config.toml`.

### `search_hybrid()` on `HybridVectorMemoryStore`

```python
def search_hybrid(self, query, namespace=None, top_k=5,
                  mode="hybrid", first_stage_k=50, weights=None):
    weights = weights or load_hybrid_weights_from_disk()

    # 1. LightRAG retrieval (async)
    lr_hits = run_async(self._lr.aquery(query, mode=mode, top_k=first_stage_k))

    # 2. Hydrate to VectorEntry by obscura_key/namespace
    candidates = []
    for hit in lr_hits:
        entry = self.backend.get_vector(MemoryKey(hit.namespace, hit.key))
        if entry is None:
            continue
        # 3. Re-apply decay
        decay_mult = compute_decay(entry.memory_type, entry.created_at,
                                    entry.accessed_at, self.decay_config)
        entry.score = hit.vector_sim
        entry.rerank_score = hit.graph_relevance
        usage = entry.metadata.get("access_count", 0)
        entry.final_score = hybrid_score(
            vector_sim=hit.vector_sim,
            graph_relevance=hit.graph_relevance,
            decay_multiplier=decay_mult,
            usage_count=usage,
            weights=weights,
        )
        candidates.append(entry)

    # 4. Bump access_count + accessed_at
    self._touch_and_count_async(candidates[:top_k])

    candidates.sort(key=lambda e: e.final_score, reverse=True)
    return candidates[:top_k]
```

### Key design choices

1. **LightRAG sees raw hits; decay/usage re-applied in Obscura.** Decay logic stays owned by existing code.
2. **Hydration via existing backend.** Don't trust LightRAG's payload for canonical text — look up by `(namespace, key)`.
3. **Usage counting finally wired.** `_touch_and_count_async` updates `accessed_at` AND increments `metadata.access_count` on returned hits. Add `access_count` to `VectorEntry` payload in Qdrant + SQLite backends; default 0 for legacy entries.
4. **Fallback path:** if `lr_hits` empty, fall back to `super().search_reranked()`. Same return type; degrades gracefully during backfill.

### Wiring into existing API

`SemanticMemoryMixin.recall()` (`vector_memory.py:665`) gets a new `use_graph: bool = True` kwarg. A/B controlled by `OBSCURA_LIGHTRAG=on|off` (off by default in v1).

---

## Phase 4 — Tool / backend integration

### Modifications to existing tools

- **`semantic_search_impl`** (`memory_tools.py:123`): no signature change. Internally route to `search_hybrid()` when `HybridVectorMemoryStore` is active. Bump response payload to include `graph_relevance`.
- **`store_searchable_impl`** (`memory_tools.py:170`): no change. Async fan-out happens in `HybridVectorMemoryStore.set()`.

### New tools (two only)

1. **`memory_graph_query`** — explicit hybrid retrieval with mode control:
   ```
   memory_graph_query(query, mode="hybrid"|"local"|"global"|"mix", top_k=5)
   ```
   `local` = single-entity neighborhood. `global` = community-summary. `hybrid` = combined.

2. **`memory_graph_explain`** — given a memory key, return entities + immediate graph neighbors. Cheap (no LLM call). Lets the model debug "what is this connected to?"

Skip `memory_extract_entities` — internal pipeline operation, not model-facing.

### System prompt addition

In `build_channels_prompt_section` (`memory_tools.py:36`), append three short paragraphs:

```
## Graph-aware memory

`semantic_search` and `memory_graph_query` use a knowledge graph built from
your memories. Results combine vector similarity, graph relevance (entities
and relations the query overlaps with), recency decay, and access frequency.

Use `memory_graph_query` with mode="local" for focused entity lookup,
mode="global" for community-level summarization, or mode="hybrid" (default)
for multi-hop reasoning across entities.

`memory_graph_explain(key)` shows what entities and relations a memory
participates in.
```

### Backend wiring — single integration point

```python
# obscura/vector_memory/vector_memory.py:306

@classmethod
def for_user(cls, user, embedding_fn=None):
    with cls._lock:
        if user.user_id not in cls._instances:
            if _lightrag_enabled():
                from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
                from obscura.lightrag_memory.adapter import LightRAGAdapter
                adapter = LightRAGAdapter.for_user(user, embedding_fn or _make_default_embedding_fn())
                cls._instances[user.user_id] = HybridVectorMemoryStore(
                    user, lightrag_adapter=adapter, embedding_fn=embedding_fn)
            else:
                cls._instances[user.user_id] = cls(user, embedding_fn=embedding_fn)
        return cls._instances[user.user_id]
```

Auth middleware (`obscura/auth/middleware.py:58`) already calls `for_user()` on first login.

---

## Phase 5 — Migration / backfill

### Track A: lazy on-touch (default, zero ops)

When `HybridVectorMemoryStore.set()` is called for an existing key (upsert), the post-write fan-out runs. Any chunk re-saved during normal operation gets graph-indexed automatically.

Piggy-back on `vector_memory.touch()` (`vector_memory.py:551`): when `OBSCURA_LIGHTRAG=on` and a chunk is touched but has no `obscura_metadata.lr_indexed_at` payload field, schedule async ingest. Indexes hot chunks first.

### Track B: explicit batch backfill (CLI)

```
obscura memory backfill-graph [--user <id>] [--namespace <ns>] [--batch-size 50]
                              [--dry-run] [--max-chunks N]
```

`obscura/lightrag_memory/backfill.py`:

1. Iterate `backend.list_keys(namespace=...)`.
2. For each key: `backend.get_vector(key)` → check `metadata.lr_indexed_at`.
3. If unset: `lr_adapter.insert(...)` with rate limiting (default 1 chunk/sec).
4. Write `lr_indexed_at` back via new `backend.update_metadata(key, partial)` method.

**Cost telemetry:** log `total_llm_calls`, `estimated_cost_usd`, `chunks_indexed`. Print estimated cost before running; require `--confirm` past 1000 chunks.

**Don't wire into `run_maintenance()`.** Backfill is a heavy operation; maintenance runs on startup. Keep separate.

---

## Phase 6 — Tests

### Test layout

```
tests/unit/obscura/lightrag_memory/
├── conftest.py                # MockLightRAG fixture
├── test_hybrid_score.py
├── test_hybrid_store.py
├── test_adapter.py
└── test_backfill.py
```

### Fixtures — `MockLightRAG`

- Records every `insert_safe(doc_id, text, metadata)` call.
- Returns canned `GraphHit` lists from `aquery()`.
- `set_canned_response(query_substring, hits)` knob.
- Never imports `lightrag` itself — test runs don't pull the heavy dep.

Reuse the test pattern from `tests/unit/obscura/vector_memory/test_vector_memory.py:36-40` (BackendConfig + SQLiteBackend in `tmp_path`).

### Unit coverage

1. `hybrid_score()` weighting math — 6-8 cases.
2. `set()` fan-out — `super().set()` synchronous, `insert_safe` async.
3. `set()` skips fan-out for non-indexable types.
4. `search_hybrid()` fallback when adapter empty → calls `super().search_reranked()`.
5. `search_hybrid()` decay re-application correctness.
6. `search_hybrid()` access_count increment.
7. `delete()` propagates to adapter.

### Integration (opt-in via `RUN_LR_INTEGRATION=1`)

1. End-to-end with real `lightrag-hku` against tiny corpus + VCR-recorded LLM cassette.
2. Backfill CLI: 100 SQLite chunks, mock adapter, verify rate limiting + idempotency.

### Don't test

- LightRAG's retrieval quality.
- Real LLM calls in unit suite.

---

## Risks / open questions

### Latency

LightRAG `aquery(mode="hybrid")` does graph traversal + vector search + LLM-based answer synthesis. Disable answer synthesis with `only_need_context=True` in `QueryParam`. Even then, ~200-500ms vs. ~20-50ms plain Qdrant. Mitigation: `OBSCURA_LIGHTRAG_TIMEOUT_MS=400` budget, fall back to vector-only when exceeded.

### LLM cost during ingest

A single `ainsert` of a 1k-token chunk runs ~3-5 LLM calls. At gpt-4o-mini rates: <$0.001/chunk. ~10k chunks ≈ $5-10 backfill. Safeguards:

1. `indexable_types` whitelist excludes `episode`.
2. Backfill CLI prints estimated cost; requires `--confirm` past 1000 chunks.

### A/B testing

1. **Env flag** (`OBSCURA_LIGHTRAG=on|off`).
2. **Per-call override**: `recall(query, use_graph=False)` for cheap vector-only path. Critical for `obscura/vector_memory/consolidator.py` background work — graph queries during consolidation would multiply LLM costs.
3. **Telemetry**: log both paths' top-5 results when `OBSCURA_LIGHTRAG_SHADOW=1`.

### Open questions for the user

1. Should `episode` memories ever go into the graph? Default no. Opt-in via `graph_index=true` metadata flag if needed for temporal reasoning.
2. Multi-user isolation in shared deployment — NetworkX pickle becomes a contention point. Switch to AGE if/when relevant. Not blocking local use.
3. Consolidation interaction: when `MemoryConsolidator` deletes consolidated episodes and creates summaries, graph references go dangling. Add a hook in `consolidator.consolidate()` (~line 130) to call `lr_adapter.delete_safe(...)` per deleted episode. Easy to forget.

---

## Effort summary

| Phase | Effort | Notes |
|-------|--------|-------|
| 0 — confirm fit | done | three caveats noted above |
| 1 — scaffold + uv extra | 1 day | |
| 2 — `HybridVectorMemoryStore.set()` + adapter | 2-3 days | |
| 3 — `search_hybrid()` + scoring + usage count wiring | 3-4 days | usage_count is new; touches Qdrant + SQLite payloads |
| 4 — two new tools + system prompt | 1 day | |
| 5 — backfill CLI + lazy on-touch | 2 days | needs `update_metadata` on backend protocol |
| 6 — tests | 2-3 days | |

**Net: ~2 weeks of focused work** with existing decay logic completely untouched.

---

## Critical files for implementation

- `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/vector_memory.py`
- `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/decay.py`
- `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/backends/base.py`
- `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/backends/qdrant_backend.py`
- `/Users/elliottbregni/dev/obscura-main/obscura/tools/memory_tools.py`
