# Phase 4 — Tool / backend integration

> **Status:** ready to implement.
> **Depends on:** Phases 1-3 landed (`obscura.lightrag_memory` package present, `LightRAGAdapter`, `HybridVectorMemoryStore`, `search_hybrid()`, `hybrid_score()` all exist).
> **Owner:** assigned engineer (executes without further questions).
> **Drafted:** 2026-04-25

This phase makes the LightRAG-aware memory layer reachable from a running agent. Three things have to happen: (1) the singleton factory `VectorMemoryStore.for_user()` must hand out a `HybridVectorMemoryStore` when the feature flag is on; (2) the model needs two new tools (`memory_graph_query`, `memory_graph_explain`) plus a system-prompt section telling it what they're for; (3) the existing `semantic_search` tool quietly gains graph awareness without changing its signature, and lifecycle hooks ensure the LightRAG adapter is warmed up at login and torn down cleanly at shutdown.

The work is intentionally narrow — every change either flips the existing behaviour at a single integration point, or adds isolated new code (two tools, one prompt block, two lifecycle hooks). No existing call site is rewritten.

---

## 1. Goal & non-goals

### Goal — what this phase produces

1. **Single integration point** — `obscura/vector_memory/vector_memory.py:306` (the `for_user` classmethod) is the **only** existing call site that changes. It now decides per-user whether to instantiate the plain `VectorMemoryStore` or the Phase-2 `HybridVectorMemoryStore`. Every other caller of `VectorMemoryStore.for_user()` (the ten ingest sites listed in `00-overview.md` line 103-111) inherits graph behaviour transparently.
2. **Two new model-facing tools** registered with the existing `ToolRegistry`:
   - `memory_graph_query(query, mode, top_k, namespace)` — explicit hybrid retrieval with mode control.
   - `memory_graph_explain(key, namespace, depth)` — given a stored memory, return its entities + 1-3 hop graph neighbours. Cheap, no LLM call.
3. **System-prompt extension** — `build_channels_prompt_section` (`obscura/tools/memory_tools.py:36`) gains a conditional `## Graph-aware memory` block, rendered only when the user's store is hybrid.
4. **`semantic_search_impl` upgrade** — internal-only change. When the active store is `HybridVectorMemoryStore`, route through `search_hybrid()` instead of `search_similar()`. Response payload gains an optional `graph_relevance` field. **No tool-spec change**, no signature change.
5. **Lifecycle hooks** — warm up `LightRAGAdapter.for_user(user, ...)` at first-login provisioning (`obscura/auth/middleware.py:58`) so the first query isn't slow; close adapters on process exit via `atexit`.

### Non-goals — explicitly NOT in scope

- **No migration / backfill** (Phase 5) — existing memories don't get retroactively graph-indexed by this phase. Only memories saved while LightRAG is on go into the graph.
- **No new tests in this PR** — Phase 6 owns the test wiring. (Test sketches are listed in section 13 below as a reference for the Phase 6 engineer; do not block this PR on them.)
- **No new memory write tools** — `store_searchable` already covers ingest. Phase 2's `HybridVectorMemoryStore.set()` override wires the LightRAG fan-out automatically.
- **No web-UI changes** — UI doesn't render `graph_relevance` in this phase. Flagged as a follow-up in section 14.
- **No A/B telemetry framework changes** — `OBSCURA_LIGHTRAG_SHADOW=1` shadow logging stays a Phase 3 concern.

---

## 2. Acceptance criteria

A reviewer can check off each of these against a running build:

1. With `OBSCURA_LIGHTRAG=on` and the `lightrag` extra installed, `VectorMemoryStore.for_user(u)` returns a `HybridVectorMemoryStore` instance (`isinstance(store, HybridVectorMemoryStore) is True`).
2. With `OBSCURA_LIGHTRAG=off` (or unset), `VectorMemoryStore.for_user(u)` returns a plain `VectorMemoryStore` (`isinstance(store, HybridVectorMemoryStore) is False`).
3. If `LightRAGAdapter.for_user(...)` raises during construction (Qdrant unreachable, working_dir not writable, lightrag extra missing), the factory logs a `WARNING`, falls back to plain `VectorMemoryStore`, and the user can still use semantic memory.
4. `memory_graph_query` is registered in the active `ToolRegistry` exactly when `_lightrag_enabled() is True` at registry-build time. The Claude and Codex backends both see it via the standard tool-spec list.
5. `memory_graph_query` called against a plain (non-hybrid) store returns `{"error": "graph_unavailable", "hint": "..."}` (defensive — should not occur under standard wiring, but tested).
6. `memory_graph_query` called against a hybrid store with valid args returns JSON with keys `results[]`, `mode`, `top_k`. Each result item has `key`, `namespace`, `text`, `score`, `graph_relevance`, `final_score`, `memory_type`, `created_at`.
7. `memory_graph_explain` clamps `depth` into `[1, 3]` (passing 0 or 5 still returns a result, with depth applied at the boundary).
8. `build_channels_prompt_section(channels, is_graph_enabled=True)` includes the literal substring `"Graph-aware memory"` in its output. With `is_graph_enabled=False` (or omitted, since default is False), it does not.
9. `semantic_search_impl` invoked against a hybrid store returns `results[*].graph_relevance` as a float; against a plain store the field is absent. Existing callers that ignore unknown fields keep working.
10. `LightRAGAdapter` is constructed once per user across the process; subsequent `for_user(u)` calls reuse the cached instance (singleton). At process exit the registered `atexit` callback drains every adapter's executor without leaking threads.

---

## 3. The `for_user` modification — full diff

The current implementation is simple; the change keeps it that way. Lazy import of `obscura.lightrag_memory` matters: it must remain optional, since users without the `lightrag` extra installed must not hit an `ImportError` from a top-level import in `vector_memory.py`.

### Before — `obscura/vector_memory/vector_memory.py:306-322`

```python
@classmethod
def for_user(
    cls,
    user: AuthenticatedUser,
    embedding_fn: Callable[[str], list[float]] | None = None,
) -> VectorMemoryStore:
    """Get or create a vector memory store for the given user."""
    with cls._lock:
        if user.user_id not in cls._instances:
            cls._instances[user.user_id] = cls(user, embedding_fn=embedding_fn)
        return cls._instances[user.user_id]

@classmethod
def reset_instances(cls) -> None:
    """Clear singleton cache. For testing only."""
    with cls._lock:
        cls._instances.clear()
```

### After

```python
@classmethod
def for_user(
    cls,
    user: AuthenticatedUser,
    embedding_fn: Callable[[str], list[float]] | None = None,
) -> VectorMemoryStore:
    """Get or create a vector memory store for the given user.

    When ``OBSCURA_LIGHTRAG=on`` and the ``lightrag`` extra is installed,
    returns a :class:`HybridVectorMemoryStore` that fans writes out to a
    per-user :class:`LightRAGAdapter` and routes searches through the
    hybrid scorer. Otherwise returns the plain :class:`VectorMemoryStore`.

    If hybrid construction fails (Qdrant unreachable for the graph
    collection, working_dir not writable, lightrag-hku not importable),
    falls back to the plain store and logs a warning. The user gets a
    working memory system; the graph layer just stays off this session.
    """
    with cls._lock:
        if user.user_id in cls._instances:
            return cls._instances[user.user_id]

        instance: VectorMemoryStore
        if _lightrag_enabled():
            try:
                # Lazy import — only paid when the feature flag is on,
                # so users without the lightrag extra don't crash on
                # `from obscura.vector_memory import VectorMemoryStore`.
                from obscura.lightrag_memory.adapter import LightRAGAdapter
                from obscura.lightrag_memory.hybrid_store import (
                    HybridVectorMemoryStore,
                )

                resolved_emb_fn = embedding_fn or _make_default_embedding_fn()
                adapter = LightRAGAdapter.for_user(user, resolved_emb_fn)
                instance = HybridVectorMemoryStore(
                    user,
                    lightrag_adapter=adapter,
                    embedding_fn=embedding_fn,
                )
            except Exception as exc:
                # Don't crash the user's session over an optional feature.
                # Drop back to plain vector memory and continue.
                logger.warning(
                    "LightRAG hybrid store unavailable for user %s, "
                    "falling back to plain VectorMemoryStore: %s",
                    user.user_id[:8],
                    exc,
                )
                instance = cls(user, embedding_fn=embedding_fn)
        else:
            instance = cls(user, embedding_fn=embedding_fn)

        cls._instances[user.user_id] = instance
        return instance

@classmethod
def reset_instances(cls) -> None:
    """Clear singleton cache. For testing only.

    NOTE: this clears the in-memory dict but does not clean up
    LightRAG working dirs on disk. Tests that need a clean slate
    should also call ``LightRAGAdapter.close_all()`` and remove
    the per-user ``working_dir`` (typically
    ``~/.obscura/lightrag/<user_hash>/``). The Phase 5 backfill
    helpers expose a ``shutdown_and_clean_for_user()`` that does
    both — reuse it from test conftest.
    """
    with cls._lock:
        cls._instances.clear()
```

### Helpers introduced in the same module

```python
def _lightrag_enabled() -> bool:
    """Read OBSCURA_LIGHTRAG. Default off (v1 ships dark)."""
    import os
    return os.environ.get("OBSCURA_LIGHTRAG", "off").strip().lower() in (
        "on",
        "1",
        "true",
        "yes",
    )
```

Place `_lightrag_enabled` next to the other module-level helpers (around the existing `_make_default_embedding_fn` at line 86). Importing this from `obscura.lightrag_memory.adapter` is also fine — pick whichever colocation you prefer, but be consistent across the codebase. Recommendation: keep it in `obscura.vector_memory.vector_memory` since the `for_user` factory is the **first** caller, and `lightrag_memory` is allowed to import from `vector_memory` but not vice-versa.

### Edge cases the diff handles

**Lazy import.** The `from obscura.lightrag_memory ...` block sits inside the `if _lightrag_enabled()` branch. If a user has not installed the `lightrag` extra, that import path fails — but only on machines where `OBSCURA_LIGHTRAG=on`. Default-off users never pay the import cost.

**Adapter construction failure.** A wide `except Exception` is correct here: the user explicitly opted in to LightRAG, but if the ingest pipeline can't start (Qdrant down, disk full, lightrag-hku not pip-installed despite the env var being set), the right move is to log loudly and degrade. Memory tools must remain available — agents otherwise lose access to their entire memory. The fallback uses `cls(user, ...)` which is the same code path as default-off; existing memories continue to be queryable via the plain backend.

**Singleton consistency.** Once a user's first `for_user(u)` call has resolved (either hybrid or plain), the `_instances` dict locks the choice in for the process lifetime. Flipping `OBSCURA_LIGHTRAG=on` mid-process does not retroactively upgrade the user's store. Document this in the docstring (done above) so operators know they need a process restart to toggle.

**Test injection.** The existing test pattern (`tests/unit/obscura/vector_memory/test_vector_memory.py:36-40`) uses `VectorMemoryStore._instances.clear()` between tests. That still works; this phase only adds the docstring note that hybrid-mode test cleanup additionally needs `LightRAGAdapter.close_all()` and working-dir removal — Phase 5 introduces `shutdown_and_clean_for_user()` for that purpose. For Phase 6 unit tests using the `MockLightRAG` fixture (which never touches disk), the standard `_instances.clear()` is sufficient.

### Logger import

The module already uses `logging.getLogger(__name__)` (defined inside `_create_default_backend` at `vector_memory.py:230`). Hoist it to a module-level `logger = logging.getLogger(__name__)` near the imports if it isn't already; the fallback warning above needs it.

---

## 4. `memory_graph_query` — tool spec + impl

A new model-facing tool exposing the Phase-3 hybrid retrieval API with explicit `mode` control. Lives in `obscura/tools/memory_tools.py` next to the existing memory tools.

### Implementation

```python
def memory_graph_query_impl(
    query: str,
    mode: str = "hybrid",
    top_k: int = 5,
    namespace: str | None = None,
) -> str:
    """Search memory with knowledge-graph awareness.

    Routes through HybridVectorMemoryStore.search_hybrid(). Returns
    chunks ranked by combined vector + graph + recency + usage signals.
    """
    from obscura.core.tool_context import current_tool_context
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore

    ctx = current_tool_context()
    if ctx is None or ctx.user is None:
        return _json_error(
            "no_context",
            hint="Tool invoked without a bound user. Caller bug.",
        )

    valid_modes = {"naive", "local", "global", "hybrid", "mix"}
    if mode not in valid_modes:
        return _json_error(
            "invalid_mode",
            valid=sorted(valid_modes),
            given=mode,
        )

    store = VectorMemoryStore.for_user(ctx.user)
    if not isinstance(store, HybridVectorMemoryStore):
        return _json_error(
            "graph_unavailable",
            hint=(
                "Graph-aware retrieval is not active for this user. "
                "Use semantic_search instead, or enable OBSCURA_LIGHTRAG=on "
                "and ensure the lightrag extra is installed."
            ),
        )

    # Resolve namespace: explicit value wins, else default to project ns.
    resolved_ns = namespace if namespace is not None else _project_namespace()

    try:
        results = store.search_hybrid(
            query, mode=mode, top_k=top_k, namespace=resolved_ns,
        )
    except Exception as exc:
        return _json_error(
            "search_failed",
            hint=str(exc)[:200],
        )

    items = [
        {
            "key": str(r.key),
            "namespace": (
                r.key.namespace if hasattr(r.key, "namespace") else resolved_ns
            ),
            "text": r.text,
            "score": round(r.score, 3) if r.score is not None else None,
            "graph_relevance": (
                round(r.rerank_score, 3) if r.rerank_score is not None else None
            ),
            "final_score": (
                round(r.final_score, 3) if r.final_score is not None else None
            ),
            "memory_type": r.memory_type,
            "created_at": (
                r.created_at.isoformat() if r.created_at else None
            ),
            "metadata": r.metadata,
        }
        for r in results
    ]
    return json.dumps({
        "ok": True,
        "query": query,
        "mode": mode,
        "namespace": resolved_ns,
        "top_k": top_k,
        "count": len(items),
        "results": items,
    })
```

### Helper — shared error formatter

```python
def _json_error(code: str, **details: Any) -> str:
    """Standard error envelope returned by graph tools."""
    payload: dict[str, Any] = {"ok": False, "error": code}
    payload.update(details)
    return json.dumps(payload)
```

Place `_json_error` next to `_project_namespace` (top of `memory_tools.py`). It's reused by `memory_graph_explain` and is generic enough that future memory tools can reuse it.

### ToolSpec

The spec lives next to the existing memory tool specs in `make_memory_tool_specs(user)` (`memory_tools.py:198`), but the impl uses `current_tool_context()` rather than the closed-over `user`. Why: graph tools must read the live user from `ToolContext`, in case the agent loop binds a different user (delegation, A2A passthrough). Keep the closure-based wiring for the legacy tools (they already work); use the context pattern for new tools — matching the guidance in `obscura/core/tool_context.py:9`.

```python
ToolSpec(
    name="memory_graph_query",
    description=(
        "Search memory with knowledge-graph awareness. Combines vector "
        "similarity, graph relevance (entity/relation overlap with the "
        "query), recency decay, and access frequency into a single ranking. "
        "Use mode='local' for focused entity neighborhoods, 'global' for "
        "community-level summaries, or 'hybrid' (default) for multi-hop "
        "reasoning. Slightly more expensive than semantic_search "
        "(~200-500ms vs ~20-50ms) — prefer semantic_search for plain "
        "similarity lookups."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query text.",
            },
            "mode": {
                "type": "string",
                "description": (
                    "Retrieval mode: 'naive' (vector only), 'local' "
                    "(single-entity neighborhood), 'global' (community "
                    "summary), 'hybrid' (default, multi-hop), or 'mix' "
                    "(combined hybrid + naive)."
                ),
                "enum": ["naive", "local", "global", "hybrid", "mix"],
                "default": "hybrid",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (default: 5).",
                "default": 5,
            },
            "namespace": {
                "type": "string",
                "description": (
                    "Memory channel namespace to search "
                    "(e.g. 'project:obscura', 'workspace:architecture'). "
                    "Defaults to project:<cwd-basename> when omitted."
                ),
            },
        },
        "required": ["query"],
    },
    handler=memory_graph_query_impl,
),
```

### Design discussion

**Why expose `mode` as a parameter?** The four modes have meaningfully different semantics — `local` is "entities one hop from the query terms," `global` is "community-level summary chunks," `hybrid` is "merged graph + vector traversal." Hiding mode behind a heuristic forces the model to phrase queries to coax the right behaviour. Exposing it cheaply lets the model say "show me everything connected to X" (`local`) versus "summarize what we know about Y" (`global`). The trade-off is one more knob for the model to potentially misuse — mitigated by the enum constraint in the JSON schema (Claude SDK and OpenAI both respect this).

**Why default to `hybrid`?** It's the most general mode and the one most likely to return useful results when the model doesn't know which to pick. The cost note ("~200-500ms vs ~20-50ms") in the description sets expectations — the model knows it's paying for richer retrieval. Models picking `naive` mode get the same cost profile as plain `semantic_search` but through a different tool, which is fine.

**Why not auto-detect mode from query?** Briefly considered: detect "what does X relate to" → `local`; detect "summarize" → `global`. Rejected — query-classification heuristics misfire and the failure mode is silent (wrong results, model can't tell). Explicit is better than smart.

**`namespace` parameter — default behaviour.** When omitted, default to `_project_namespace()` (the cwd-derived namespace, same as `store_searchable`). Rationale: the model invokes graph queries during work in a project, so project-scoped retrieval is the right default. Cross-namespace querying is the special case; the model passes `namespace=None` explicitly via... wait, that's not possible since the parameter is missing entirely. Two options:
1. Treat `namespace="*"` as "all namespaces" — explicit sentinel.
2. Document that omitting the parameter searches the project ns; cross-namespace is not exposed via this tool — agents that need it use plain `semantic_search` with `namespace` omitted (which already does the project-then-global merge in `memory_tools.py:130-145`).

**Recommendation: option 2.** Keep `memory_graph_query` simple. The model has `semantic_search` for cross-namespace queries; making both tools handle every retrieval shape just doubles the surface area.

**Return shape — why round to 3 decimals?** Cosmetic. Long fractional scores don't help the model and waste tokens. 3 places preserve enough precision for the model to compare scores.

**Why include `metadata`?** Some channels carry structured tags (`memory_type`, `obscura_key`, `lr_indexed_at`). The model occasionally needs them to correlate with `memory_graph_explain` output.

---

## 5. `memory_graph_explain` — tool spec + impl

The second new tool. Cheap lookup against the local NetworkX graph — no LLM call, no Qdrant read. Lets the model see "what is this memory connected to?" without paying for a full hybrid query.

### Implementation

```python
def memory_graph_explain_impl(
    key: str,
    namespace: str = "",
    depth: int = 1,
) -> str:
    """Inspect entities and graph neighbors for a stored memory.

    Reads from LightRAG's NetworkX graph directly. No LLM call, no
    embedding lookup — fast and free.
    """
    from obscura.core.tool_context import current_tool_context
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore

    ctx = current_tool_context()
    if ctx is None or ctx.user is None:
        return _json_error("no_context")

    store = VectorMemoryStore.for_user(ctx.user)
    if not isinstance(store, HybridVectorMemoryStore):
        return _json_error(
            "graph_unavailable",
            hint=(
                "Graph-aware retrieval is not active. "
                "Cannot explain entities for a chunk that was never indexed."
            ),
        )

    # Clamp depth into [1, 3] — graph explosion at depth 4+ on a personal
    # memory graph could return thousands of nodes and bury the answer.
    clamped_depth = max(1, min(depth, 3))
    resolved_ns = namespace or _project_namespace()
    doc_id = f"{resolved_ns}::{key}"

    try:
        explanation = store._lr.get_neighbors(
            doc_id=doc_id,
            depth=clamped_depth,
        )
    except KeyError:
        return _json_error(
            "key_not_found",
            namespace=resolved_ns,
            key=key,
            hint=(
                "This memory may not be graph-indexed yet. Save it via "
                "store_searchable to trigger indexing, or run the "
                "`obscura memory backfill-graph` CLI to index existing "
                "chunks."
            ),
        )
    except Exception as exc:
        return _json_error(
            "explain_failed",
            hint=str(exc)[:200],
        )

    return json.dumps({
        "ok": True,
        "key": key,
        "namespace": resolved_ns,
        "depth": clamped_depth,
        "entities": explanation.entities,
        "relations": explanation.relations,
        "neighbor_chunks": explanation.neighbors[:10],
    })
```

### ToolSpec

```python
ToolSpec(
    name="memory_graph_explain",
    description=(
        "Inspect the entities and relations associated with a stored "
        "memory. Returns the entities extracted from the chunk and its "
        "1-hop graph neighbors (or up to 3 hops via `depth`). Useful for "
        "understanding how a piece of memory connects to other memories. "
        "Cheap: no LLM call, reads the local graph directly."
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "The memory key (as returned by store_searchable).",
            },
            "namespace": {
                "type": "string",
                "description": (
                    "Namespace the memory was stored in. Defaults to "
                    "project:<cwd-basename> when omitted."
                ),
                "default": "",
            },
            "depth": {
                "type": "integer",
                "description": (
                    "Hop distance to traverse from the chunk's entities. "
                    "Clamped to [1, 3]. Default: 1."
                ),
                "default": 1,
            },
        },
        "required": ["key"],
    },
    handler=memory_graph_explain_impl,
),
```

### `LightRAGAdapter.get_neighbors` — new API spec

This method is **new in Phase 4**. It does not exist in Phases 1-3 and must be added to `obscura/lightrag_memory/adapter.py`. Spec:

```python
@dataclass(frozen=True)
class GraphExplanation:
    """Result of a graph neighborhood lookup for a single chunk."""

    entities: list[dict[str, Any]]
    """List of {"name": str, "type": str, "description": str} dicts —
    the entities that were extracted from this chunk at ingest time."""

    relations: list[dict[str, Any]]
    """List of {"source": str, "target": str, "description": str} dicts —
    the relations the chunk participates in."""

    neighbors: list[str]
    """List of doc_ids that share entities/relations with this chunk.
    Sorted by edge count (most-connected first). Ready to plug back
    into memory_graph_query as namespace::key pairs."""


class LightRAGAdapter:
    # ... existing methods from Phase 2 ...

    def get_neighbors(self, doc_id: str, depth: int = 1) -> GraphExplanation:
        """Read entities and neighbors for a chunk from the local NetworkX graph.

        Does not run any LLM call. Does not query Qdrant. Reads the
        pickled NetworkX graph LightRAG maintains in working_dir and
        traverses up to ``depth`` hops out from the chunk's entities.

        Raises:
            KeyError: if doc_id is not present in the graph (chunk never
                indexed, or graph file missing).
        """
        # Implementation strategy:
        # 1. Look up the chunk node by doc_id in lightrag.chunk_entity_relation_graph
        # 2. Collect entity nodes adjacent to it (these are LightRAG's
        #    "entity" type nodes; lightrag/utils.py has the type tag).
        # 3. For each entity, BFS out to `depth` hops, collecting:
        #    - entity nodes → entities list
        #    - relation edges → relations list
        #    - chunk nodes → neighbors list (deduped, sorted by edge count)
        # 4. Cap each list at sane limits (entities ≤ 50, relations ≤ 100,
        #    neighbors ≤ 50) before returning. The tool further trims
        #    neighbors to 10 in the response.
        ...
```

The actual implementation pulls from LightRAG's internal `chunk_entity_relation_graph` attribute — a `networkx.Graph` (or `MultiDiGraph` depending on storage backend; check the version pinned in `pyproject.toml`'s `lightrag-hku` dep). LightRAG ships with `lightrag.kg.networkx_impl.NetworkXStorage` which exposes `get_node`, `get_node_edges`, `get_all_labels` — these are the building blocks. The implementation lives in the adapter, not in the tool, so the tool stays portable if LightRAG changes its internal graph storage to AGE/Neo4j later.

### Design discussion

**Why no LLM call?** The graph is *already populated* at ingest time — when a chunk is inserted, LightRAG runs entity-extraction LLM calls and writes the results into the NetworkX graph. `get_neighbors` is a pure read against that pre-populated structure. This is the cheapest tool in the suite and the description tells the model so. Confirmed against LightRAG's API: the `chunk_entity_relation_graph` attribute holds extracted graph structure post-ingest; reads are O(degree).

**Why clamp depth to [1, 3]?** A personal memory graph will commonly have entities with degree 50-200 (a common entity like "Python" or "the user" connects to many chunks). At depth 2 that already balloons past 1k nodes; at depth 4+ a single query could return tens of thousands. The model gets confused by oversized responses (token bloat → the relevant neighbours drown). Three hops is the practical maximum for a debugging tool. If a future use case needs unbounded traversal, add a separate tool with explicit cost warnings rather than uncapping this one.

**Why not return full entity descriptions?** Already in the spec — the dataclass field for `entities` includes `description`. LightRAG's entity extraction populates that field with a short LLM-generated blurb. Capping at 50 entities keeps responses sane. If a chunk has more than 50 entities it's probably an unusually dense summary and the tool's 50-cap message is the right hint to the model ("there are more; narrow your query").

**`KeyError` handling — surfacing `key_not_found` vs silent empty result.** The tool returns an explicit error code. A silent empty result would be misleading: the model can't tell "no neighbours" from "this key isn't indexed." The hint tells the model the most likely cause (Phase 5 backfill needed) and an action it can take (`store_searchable` re-saves the key, triggering the Phase 2 fan-out).

---

## 6. Why NOT add other tools

The temptation to add helpers is real. Each of the following has been considered and rejected; **document the rationale here so the next engineer doesn't add cruft**:

### `memory_extract_entities(text)` — rejected

LightRAG's entity extraction is an internal pipeline operation — it runs at ingest, not at query time. Exposing it as a tool means the model would invoke entity extraction on arbitrary text, paying multiple LLM calls per invocation, with no persisted result. This is the wrong shape: if the model wants entities for new text, the right move is to save the text via `store_searchable` (which triggers the Phase 2 fan-out, including extraction) and then call `memory_graph_explain` against the resulting key. Two-step but principled. Don't add a tool that papers over the lifecycle.

### `memory_graph_walk(start_key, end_key)` — rejected

Path-finding between two specific chunks is a niche use case. `memory_graph_query(query=..., mode="local")` already covers "what's near X" via natural-language entry. Pure key-to-key walks would mostly be model-driven debugging, and `memory_graph_explain(key, depth=3)` on each endpoint is sufficient. Adding a third tool inflates the prompt without unlocking a new capability. **If telemetry shows agents repeatedly chaining `memory_graph_explain` calls in this pattern, revisit and add it then.** Until then, no.

### `memory_set_weights(vector=..., graph=..., decay=..., usage=...)` — rejected

Weight tuning is a configuration concern, not a model concern. Letting the model rewrite its own scoring weights opens a footgun: the model might over-weight `graph` to surface the kind of results it likes, and reward-hack itself into a local optimum. Weights live in `~/.obscura/config.toml` under `[vector_memory.lightrag.weights]` (Phase 3 spec) and are tuned by humans. **Hard rule: tools must not let the model self-modify retrieval scoring.**

### `memory_index_status()` — rejected

A "how many chunks are indexed in the graph" tool would be helpful for debugging, but the right surface for that is a CLI command (`obscura memory status` — already exists for the plain vector store, extend it with a graph row in Phase 5). Exposing index health to the model adds prompt weight for a question the model rarely needs to ask.

### `memory_graph_search_entities(name)` — rejected, but borderline

Find-an-entity-by-name has some appeal: "which memories mention 'Anthropic'?" Already covered by `memory_graph_query("Anthropic", mode="local")` — the entity name **is** a valid query string. The `local` mode neighbours-of-entity-mentioned-in-query is exactly this lookup. Fold it under `memory_graph_query`; don't split.

---

## 7. `semantic_search_impl` upgrade

The existing `semantic_search` tool gets a quiet internal upgrade. **No tool-spec change.** External callers (the model, the routes, A2A peers) see the same parameters and the same response keys plus one new optional field.

### Before — `obscura/tools/memory_tools.py:123-168`

```python
def semantic_search_impl(
    query: str,
    top_k: int = 5,
    namespace: str | None = None,
) -> str:
    """Search memory using semantic similarity, optionally in a specific namespace."""
    store = VectorMemoryStore.for_user(user)
    search_ns = namespace if namespace is not None else None
    if search_ns is None:
        proj_ns = _project_namespace()
        proj_results = store.search_similar(query, namespace=proj_ns, top_k=top_k)
        global_results = store.search_similar(query, namespace=None, top_k=top_k)
        seen_keys: set[str] = set()
        results = []
        for r in proj_results + global_results:
            rk = str(getattr(r, "key", r))
            if rk not in seen_keys:
                seen_keys.add(rk)
                results.append(r)
        results = results[:top_k]
    else:
        results = store.search_similar(query, namespace=search_ns, top_k=top_k)
    items = [
        {
            "key": str(r.key),
            "namespace": r.key.namespace if hasattr(r.key, "namespace") else "",
            "score": round(r.score, 3),
            "final_score": round(r.final_score, 3),
            "text": r.text,
            "memory_type": r.memory_type,
            "metadata": r.metadata,
        }
        for r in results
    ]
    return json.dumps({...})
```

### After

```python
def semantic_search_impl(
    query: str,
    top_k: int = 5,
    namespace: str | None = None,
) -> str:
    """Search memory using semantic similarity, optionally in a specific namespace.

    When the user's store is a :class:`HybridVectorMemoryStore`, routes
    through ``search_hybrid()`` (mode="hybrid") for graph-aware ranking.
    Otherwise behaves exactly as before — plain vector similarity.
    """
    # Lazy import to avoid forcing the lightrag extra on default-off users.
    try:
        from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
    except ImportError:
        HybridVectorMemoryStore = None  # type: ignore[assignment, misc]

    store = VectorMemoryStore.for_user(user)
    is_hybrid = (
        HybridVectorMemoryStore is not None
        and isinstance(store, HybridVectorMemoryStore)
    )

    def _do_search(ns: str | None) -> list:
        if is_hybrid:
            # mode="hybrid" is the closest analog to plain similarity:
            # combines vector + graph + decay + usage and returns chunks
            # ranked by `final_score`. Caller doesn't see the difference.
            return store.search_hybrid(
                query, mode="hybrid", top_k=top_k, namespace=ns,
            )
        return store.search_similar(query, namespace=ns, top_k=top_k)

    if namespace is None:
        proj_ns = _project_namespace()
        proj_results = _do_search(proj_ns)
        global_results = _do_search(None)
        seen_keys: set[str] = set()
        results = []
        for r in proj_results + global_results:
            rk = str(getattr(r, "key", r))
            if rk not in seen_keys:
                seen_keys.add(rk)
                results.append(r)
        results = results[:top_k]
    else:
        results = _do_search(namespace)

    items: list[dict[str, Any]] = []
    for r in results:
        item: dict[str, Any] = {
            "key": str(r.key),
            "namespace": r.key.namespace if hasattr(r.key, "namespace") else "",
            "score": round(r.score, 3) if r.score is not None else None,
            "final_score": (
                round(r.final_score, 3) if r.final_score is not None else None
            ),
            "text": r.text,
            "memory_type": r.memory_type,
            "metadata": r.metadata,
        }
        # Backwards-compatible: only include graph_relevance when active.
        # Old clients that ignore unknown fields keep working; new clients
        # can branch on its presence.
        if is_hybrid and r.rerank_score is not None:
            item["graph_relevance"] = round(r.rerank_score, 3)
        items.append(item)

    return json.dumps({
        "ok": True,
        "query": query,
        "namespace": namespace,
        "count": len(items),
        "results": items,
    })
```

### Why a `try`/`except ImportError`?

`HybridVectorMemoryStore` lives in the optional `lightrag_memory` package. On a default-off install, the package may not be importable (the user didn't install the `lightrag` extra). The `isinstance` check then needs a sentinel — `HybridVectorMemoryStore = None` — and `is_hybrid` collapses to `False`. The fallback path is the existing implementation, byte-for-byte. No behavioural change for default-off users.

### Decision: should `semantic_search` get a `mode` parameter too?

**No — keep it simple.** The two tools have different positioning:
- `semantic_search`: "give me chunks similar to this query." Stable interface, used by every existing caller.
- `memory_graph_query`: "give me chunks plus their graph context for multi-hop reasoning." Power tool, exposes mode.

Adding `mode` to `semantic_search` would (a) require updating every existing caller's prompt to teach them about it, and (b) blur the distinction between the two tools. Better positioning: the model uses `semantic_search` for plain lookups and reaches for `memory_graph_query` when it specifically wants graph-aware behaviour with mode control. The system-prompt section in §8 makes that distinction explicit.

### Existing-caller compatibility

Every caller of `semantic_search` (the routes at `obscura/routes/vector_memory.py`, the CLI bridge, A2A peers, the supervisor hook scaffold, the SemanticMemoryMixin) reads `results[*].score` and `results[*].final_score` if present. They do not key on the absence of `graph_relevance`. New clients can opt in to the field; old ones don't notice.

**One caveat for the engineer:** If any caller does dictionary-key validation (e.g. `pydantic` model parsing the response), the new optional field needs `model_config = ConfigDict(extra="ignore")` or equivalent. Grep for `pydantic` parsers of `semantic_search` results before merging — there is one in `obscura/routes/vector_memory.py` that returns the response straight through, so it's likely fine, but verify.

---

## 8. System-prompt section

The existing `build_channels_prompt_section` (`obscura/tools/memory_tools.py:36`) gets a conditional graph-aware block. The block is **only emitted when the user's store is hybrid**, otherwise the model sees tools it can't use and gets confused.

### Diff

```python
# obscura/tools/memory_tools.py:36

def build_channels_prompt_section(
    channels: list[MemoryChannel],
    is_graph_enabled: bool = False,  # NEW
) -> str:
    """Build a system prompt section describing available memory channels.

    Returns ``""`` if no channels are configured.

    Args:
        channels: Active memory channels for this user.
        is_graph_enabled: When True, append a "## Graph-aware memory" block
            describing memory_graph_query / memory_graph_explain. Should
            mirror whether VectorMemoryStore.for_user(user) returned a
            HybridVectorMemoryStore. Pass False (default) for plain stores.
    """
    if not channels and not is_graph_enabled:
        return ""

    lines: list[str] = []

    # Existing ## Memory Channels block — unchanged.
    if channels:
        lines.extend([
            "## Memory Channels",
            "",
            "Context is automatically injected from these channels based on what you're working on.",
            "You can also explicitly store/search memories in channel namespaces using `store_searchable` and `semantic_search`.",
            "",
        ])

        for ch in sorted(channels, key=lambda c: c.priority, reverse=True):
            trigger_parts: list[str] = []
            if ch.triggers.always:
                trigger_parts.append("always active")
            if ch.triggers.file_globs:
                trigger_parts.append(f"files: {', '.join(ch.triggers.file_globs)}")
            if ch.triggers.keywords:
                trigger_parts.append(f"keywords: {', '.join(ch.triggers.keywords)}")
            if ch.triggers.tool_names:
                trigger_parts.append(f"tools: {', '.join(ch.triggers.tool_names)}")

            trigger_str = "; ".join(trigger_parts) if trigger_parts else "manual"
            injection = "system prompt" if ch.injection == "system" else "per-turn"

            lines.append(
                f"- **{ch.name}** → namespace `{ch.namespace}` "
                f"({injection}, {trigger_str})",
            )

        lines.append("")
        lines.append(
            'To store a memory: `store_searchable(key, text, namespace="<namespace>", memory_type="fact")`',
        )
        lines.append(
            'To search a channel: `semantic_search(query, namespace="<namespace>")`',
        )

    # NEW — graph-aware memory section
    if is_graph_enabled:
        if lines:
            lines.append("")
        lines.extend([
            "## Graph-aware memory",
            "",
            "`semantic_search` results are ranked by combined vector similarity, "
            "graph relevance (entities and relations the query overlaps with), "
            "recency decay, and access frequency. Use it for plain lookups.",
            "",
            "`memory_graph_query(query, mode=...)` adds explicit mode control: "
            "`local` for focused entity neighborhoods, `global` for community-level "
            "summaries, `hybrid` (default) for multi-hop reasoning across entities.",
            "",
            "`memory_graph_explain(key)` shows which entities and relations a "
            "stored memory participates in. Cheap (no LLM call); use for debugging "
            "or to plan a follow-up `memory_graph_query`.",
        ])

    return "\n".join(lines)
```

### Caller updates

Both call sites need to pass `is_graph_enabled`. They derive it from the user's actual store:

```python
# obscura/cli/__init__.py:1031 (before)
from obscura.tools.memory_tools import build_channels_prompt_section
channels_doc = build_channels_prompt_section(_context_router.channels)

# After
from obscura.tools.memory_tools import build_channels_prompt_section
from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore  # may fail
try:
    is_hybrid = isinstance(
        VectorMemoryStore.for_user(cli_user), HybridVectorMemoryStore,
    )
except ImportError:
    is_hybrid = False
channels_doc = build_channels_prompt_section(
    _context_router.channels, is_graph_enabled=is_hybrid,
)
```

Same diff for `obscura/cli/session.py:1109` (the second caller, identical pattern).

To avoid duplicating the `try`/`except ImportError` dance at every call site, add a small helper to `memory_tools.py`:

```python
def _is_user_graph_enabled(user: AuthenticatedUser) -> bool:
    """Return True iff this user's vector memory is graph-aware.

    Centralizes the lazy-import + isinstance check so callers don't
    have to handle the ImportError case themselves.
    """
    try:
        from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
    except ImportError:
        return False
    return isinstance(VectorMemoryStore.for_user(user), HybridVectorMemoryStore)
```

Callers become:

```python
channels_doc = build_channels_prompt_section(
    _context_router.channels,
    is_graph_enabled=_is_user_graph_enabled(cli_user),
)
```

### Token budget

The new block is six lines of body content (three short paragraphs). At ~80 tokens per paragraph that's ~250 tokens added to the system prompt **only when graph is enabled**. For default-off users the cost is zero. Worth it: the model needs to know these tools exist and when to use them; the alternative of letting the model discover via `tool_search` is reactive and slower.

**Cache invalidation note.** Anthropic prompt caching keys on prefix bytes. Adding a new section to the system prompt invalidates the cached prefix once. Subsequent turns within the same session reuse the new prefix and see no further invalidation. Acceptable.

### Cross-reference between the tools

The wording above explicitly distinguishes:
- `semantic_search` → "plain lookups," no mode control, the everyday tool.
- `memory_graph_query` → "explicit mode control," for when the model wants graph traversal semantics.
- `memory_graph_explain` → "debugging or plan a follow-up" — positions it as a precursor to a second-step `memory_graph_query`.

This is the model's shortest path to picking the right tool for "give me relevant chunks for this query" (`semantic_search`) vs "give me chunks plus their entity/relation context for multi-hop reasoning" (`memory_graph_query`).

---

## 9. Tool registration & discovery

### How memory tools register today

The existing memory tools register via the `make_memory_tool_specs(user)` factory (`memory_tools.py:198`), which is called from three call sites:

1. `obscura/tools/providers/__init__.py:142` — registers via the broker (`context.broker.register_tool_spec(tool_spec)`).
2. `obscura/cli/__init__.py:1123` — adds to the CLI's `system_tools` list before backend init.
3. `obscura/cli/session.py:1210` — same pattern for the durable-session CLI.

**Memory tools do not use the `@tool` decorator** — they are built imperatively as `ToolSpec` objects inside the factory function. This is intentional because each tool closes over a specific `AuthenticatedUser`. (The new graph tools could use `@tool`, since they read user from `ToolContext` instead of closing over it, but for consistency with the existing memory tools they belong in the same factory.)

### Recommendation: extend `make_memory_tool_specs`

Add the two new tools inside the existing factory, behind a conditional:

```python
def make_memory_tool_specs(user: AuthenticatedUser) -> list[ToolSpec]:
    """Create memory tool specs bound to a user."""

    # ... existing impls ...

    specs = [
        ToolSpec(name="store_memory", ...),
        ToolSpec(name="recall_memory", ...),
        ToolSpec(name="semantic_search", ...),
        ToolSpec(name="store_searchable", ...),
    ]

    # Graph-aware tools — only registered when LightRAG is active for this user.
    if _is_user_graph_enabled(user):
        specs.extend([
            ToolSpec(name="memory_graph_query", ...),
            ToolSpec(name="memory_graph_explain", ...),
        ])

    return specs
```

### Conditional registration — option chosen and rationale

**Option chosen: register only when enabled** (option 2 from the brief).

Rationale:
- **Cleaner system prompt for non-enabled users.** The Anthropic / OpenAI / Codex backends all dump the registered tool list into the system prompt. Tools the model can't use are noise.
- **Cleaner error surface.** With the tool not registered, the model can't invoke it accidentally and won't see a `graph_unavailable` error. (The `graph_unavailable` branch in the impl is defensive — it should never fire under normal wiring, but it's there in case the model invokes the tool through a side channel like a hardcoded prompt.)
- **Cleaner tool-search results.** `tool_search` discovery doesn't surface graph tools to default-off users.

The defensive `graph_unavailable` branch in the impl stays — it covers test cases where someone manually constructs a tool spec list, and it's free in code size.

### Where the conditional check lives

`_is_user_graph_enabled(user)` (defined in §8) is the single point of truth. `make_memory_tool_specs` calls it once at registration time. **Result is per-user, computed once at registry build, locked in for the session.** If the user's hybrid status flips mid-session (e.g. the adapter falls back at first-use), the tool list does not retroactively shrink — but since the tool's impl is also defensive, this is benign: the model gets `graph_unavailable` and falls back to `semantic_search`.

### Backend exposure

The Claude SDK and Codex/Copilot backends both read tools from `ToolRegistry.all()` at backend `start()`. The registry is built before backend start — see the `make_memory_tool_specs` callers above. Phase 4 changes the **contents** of that list for graph-enabled users; the mechanism is unchanged.

---

## 10. Lifecycle hooks

LightRAG's adapter holds an executor pool (Phase 2: `ThreadPoolExecutor(max_workers=2)` per user) and a cached event loop for async LLM calls. Both need explicit cleanup or they leak threads.

### Warm-up — first-login provisioning

Today, `_ensure_user_account` in `obscura/auth/middleware.py:51-61` runs on first successful Supabase auth and provisions `MemoryStore` + `VectorMemoryStore` for the user. The `VectorMemoryStore.for_user(user)` call there will now return `HybridVectorMemoryStore` when the env flag is on, which **transitively** triggers `LightRAGAdapter.for_user(user, ...)` inside the modified `for_user` factory (§3).

So warm-up is **already implicit** in the Phase 4 changes. No explicit warm-up hook is needed.

But the implicit warm-up only happens when the user hits a protected endpoint. If the user's first interaction is via the CLI (not the web/API path), they don't pass through `middleware.py` and the warm-up doesn't fire there — it fires on the user's first `for_user(user)` call from the CLI bootstrap, which is fine but synchronous-on-first-query (visible latency).

**Recommendation:** add an explicit warm-up at CLI bootstrap when `_lightrag_enabled()`:

```python
# obscura/cli/__init__.py — wherever cli_user is first available
# (grep for `VectorMemoryStore.for_user(cli_user)` — the existing call
# is the right location, this just makes sure it happens early).

# Phase 4 addition: explicit early warm-up for the CLI path.
# Spinning up a LightRAGAdapter takes ~200-500ms (loads NetworkX
# pickle, opens Qdrant collection). Do it now so the first tool
# invocation isn't slow.
try:
    _ = VectorMemoryStore.for_user(cli_user)
except Exception:
    logger.exception("Memory warm-up failed (non-fatal)")
```

The middleware path needs no change — the existing call already triggers warm-up:

```python
# obscura/auth/middleware.py:57-58 — unchanged after Phase 4
MemoryStore.for_user(user)
VectorMemoryStore.for_user(user)  # now returns Hybrid when enabled
```

### Shutdown — close on logout / process exit

`VectorMemoryStore` does not currently expose a `close()` method. The Qdrant client closes lazily and SQLite connections close on GC. The new `HybridVectorMemoryStore` introduces the **first** explicit shutdown need, because of the executor pool.

#### `HybridVectorMemoryStore.close()` — new method

```python
# obscura/lightrag_memory/hybrid_store.py — new method on the class.

def close(self) -> None:
    """Drain pending ingest jobs and stop the LightRAG adapter.

    Called explicitly at logout or process exit. Idempotent.
    Errors are logged but not raised — shutdown must complete.
    """
    try:
        self._ingest_executor.shutdown(wait=True, cancel_futures=False)
    except Exception:
        logger.exception("Failed to drain ingest executor for user %s",
                         self.user_id[:8])
    try:
        self._lr.close()
    except Exception:
        logger.exception("Failed to close LightRAG adapter for user %s",
                         self.user_id[:8])
```

`wait=True, cancel_futures=False` is the safe choice: pending writes drain before close. Risk: a slow LLM call during ingest can hold shutdown for ~10-30s. Acceptable for `atexit`; for explicit logout, consider a `timeout=10` via a daemon-thread join pattern instead. Phase 4 ships the simple version; revisit if shutdown latency becomes a complaint.

#### `LightRAGAdapter.close()` — must exist by Phase 4

This was specified as an internal close in Phase 2 (drain executor, stop event loop). Confirm during Phase 4 implementation that it's present in `obscura/lightrag_memory/adapter.py`. If Phase 2 shipped without it (e.g. Phase 2 owner deferred it), Phase 4 adds:

```python
class LightRAGAdapter:
    def close(self) -> None:
        """Stop the cached event loop and drain any in-flight ainsert calls.

        Idempotent. Safe to call from atexit.
        """
        if self._closed:
            return
        try:
            self._executor.shutdown(wait=True)
        except Exception:
            logger.exception("LightRAGAdapter executor shutdown failed")
        try:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._loop_thread.join(timeout=5)
        except Exception:
            logger.exception("LightRAGAdapter event loop teardown failed")
        self._closed = True
```

#### Process-exit hook — `atexit`

```python
# obscura/lightrag_memory/__init__.py — new module-level registration.

import atexit
import logging

from obscura.lightrag_memory.adapter import LightRAGAdapter

logger = logging.getLogger(__name__)


def _shutdown_all_adapters() -> None:
    """Close every per-user LightRAGAdapter at process exit.

    Registered via atexit so it runs on normal Python interpreter shutdown.
    Errors are swallowed — we're already exiting.
    """
    try:
        LightRAGAdapter.close_all()
    except Exception:
        logger.warning("LightRAG adapter shutdown raised during atexit")


atexit.register(_shutdown_all_adapters)
```

`LightRAGAdapter.close_all()` is a classmethod that iterates `cls._instances` and calls `close()` on each, then clears the dict:

```python
@classmethod
def close_all(cls) -> None:
    """Close every cached adapter. Safe to call multiple times."""
    with cls._lock:
        for adapter in list(cls._instances.values()):
            try:
                adapter.close()
            except Exception:
                logger.exception("LightRAGAdapter close failed during close_all")
        cls._instances.clear()
```

#### Logout hook — optional in Phase 4

The auth middleware does not currently fire a logout event for Supabase tokens (token expiry happens on the client; the server just rejects the next request). If/when explicit logout is added, plumb a `HybridVectorMemoryStore.close()` call into the path. Out of scope for Phase 4.

### Test cleanup

Tests using `MockLightRAG` (Phase 6) won't have a real executor to drain. The mock should expose a no-op `close()` so the production code paths exercise cleanly under test. `LightRAGAdapter._instances.clear()` between tests is sufficient when paired with the mock fixture.

---

## 11. Error rendering for the model

All graph tools return errors as a uniform JSON envelope. The `_json_error` helper (defined in §4) produces:

```json
{"ok": false, "error": "<machine_code>", "hint": "<human_readable>", ...}
```

### Standard error codes

| Code | Meaning | Hint surfaced to model |
|---|---|---|
| `no_context` | Tool invoked without a bound `ToolContext` (caller bug, should not happen in production). | "Tool invoked without a bound user. Caller bug." |
| `graph_unavailable` | The user's store is not a `HybridVectorMemoryStore`. | "Graph-aware retrieval is not active. Use semantic_search instead, or enable OBSCURA_LIGHTRAG=on." |
| `invalid_mode` | `mode` arg not in the enum. | Includes `valid` list and `given` value. |
| `key_not_found` | `memory_graph_explain` lookup for a doc_id not in the graph. | "This memory may not be graph-indexed yet. Save it via store_searchable, or run `obscura memory backfill-graph`." |
| `search_failed` | LightRAG `aquery` raised. | First 200 chars of the exception message (no stack trace). |
| `explain_failed` | NetworkX traversal raised (graph file corrupted, etc). | First 200 chars of the exception message. |

### What NOT to include in errors

- **No stack traces.** They eat tokens and don't help the model.
- **No internal state.** Don't leak Qdrant URLs, working_dir paths, user IDs, etc.
- **No nested error chains.** One layer of error code + hint is enough; deeper detail belongs in logs.

### Why structured codes

The model can branch on `error` cheaply ("if `graph_unavailable`, fall back to `semantic_search`"). Free-form error text invites string-matching, which breaks on translation/rewording. The `hint` field carries the human-readable explanation; the `error` code is the stable contract.

The 200-character cap on raw exception messages (in `search_failed` / `explain_failed`) is a safety belt — LightRAG's internal exceptions can be quite verbose. Truncating prevents one bad query from blowing up the model's context.

---

## 12. Capability gating

Tools in Obscura are gated by capability strings. The existing `semantic_search` and `store_searchable` tools are gated under the broader memory capability set — grep for `memory.semantic` shows nothing matched in this repo as of the audit, suggesting memory tools currently have no explicit per-tool capability gate. They inherit whatever default-tier policy is active.

### Recommendation: reuse the existing memory gate

**The new tools should match `semantic_search`'s gating**, whatever that turns out to be. Concretely:

- If `semantic_search` is gated by capability `memory.semantic` (or similar), gate `memory_graph_query` and `memory_graph_explain` the same way.
- If `semantic_search` has no explicit gate (just default tier), the new tools also have no explicit gate.

The reasoning: **both new tools are enhanced retrieval, not a new class of operation.** A user who has been granted access to `semantic_search` should not have to re-grant a separate capability to use `memory_graph_query`. Splitting the gate adds operational friction without unlocking a meaningful security boundary.

### When to introduce `memory.graph` separately

Consider a separate `memory.graph` capability **only if**:

1. **Resource control becomes load-bearing.** Graph queries are 10x more expensive than vector queries (~200-500ms vs ~20-50ms, plus background LLM cost during ingest). If a deployment wants to enable cheap memory but disable expensive graph queries (e.g. for limited-tier users in a multi-user shared deployment), a separate gate is warranted. **For single-user local Obscura, this doesn't apply.**

2. **Audit / compliance requires it.** Graph queries reveal entity relationships, which can be sensitive. If audit requirements demand a per-tool grant trail, split the gate.

3. **Backfill triggers from tool calls.** If the tool is ever extended to trigger backfill (it is not, in this phase — backfill is CLI-only per Phase 5), separate gating becomes important to prevent runaway LLM cost from a single tool call.

None of these apply in Phase 4. **Reuse the existing memory capability.** Document the rationale in the implementation comment so a future engineer doesn't add `memory.graph` reflexively.

### `required_tier` on the `ToolSpec`

The `ToolSpec` dataclass takes a `required_tier` field (`obscura/core/tools.py:519`, default `"public"`). Match the existing memory tools — `semantic_search` and `store_searchable` use the default. So do the new tools.

---

## 13. Testing for this phase

Phase 6 is the dedicated test phase, but **the engineer implementing Phase 4 should drop in skeleton tests** to lock in the contract. Concrete cases below — Phase 6 will round these out and add the integration matrix.

Test layout for Phase 4 work:

```
tests/unit/obscura/tools/test_memory_graph_tools.py    # NEW
tests/unit/obscura/vector_memory/test_for_user.py      # extend if exists, else NEW
tests/unit/obscura/tools/test_build_channels_prompt.py # NEW
```

### Unit tests — concrete list

1. **`for_user` returns hybrid when env on.** Set `OBSCURA_LIGHTRAG=on` via `monkeypatch`, mock `LightRAGAdapter.for_user` to return a stub, call `VectorMemoryStore.for_user(user)`, assert `isinstance(store, HybridVectorMemoryStore)`. Reset `_instances` between tests.

2. **`for_user` returns plain when env off.** Default env, assert `not isinstance(store, HybridVectorMemoryStore)`.

3. **`for_user` falls back gracefully on adapter failure.** Mock `LightRAGAdapter.for_user` to raise `RuntimeError("Qdrant unreachable")`. Set env on. Call `for_user`, assert `isinstance(store, VectorMemoryStore)` (plain), assert one `WARNING` was logged via `caplog`. Memory operations still work afterwards.

4. **`memory_graph_query_impl` returns `graph_unavailable` on plain store.** Default env, bind a `ToolContext(user=test_user)`, call `memory_graph_query_impl("anything")`, parse JSON, assert `result["error"] == "graph_unavailable"` and `result["ok"] is False`.

5. **`memory_graph_query_impl` returns expected JSON shape on hybrid store.** Mock `HybridVectorMemoryStore.search_hybrid` to return three `VectorEntry` stubs with known `score` / `rerank_score` / `final_score`. Call the impl, parse JSON, assert keys `results`, `mode`, `top_k`, `count`, and per-result `key`, `text`, `graph_relevance`, `final_score`, `memory_type`.

6. **`memory_graph_query_impl` rejects invalid mode.** Pass `mode="bogus"`, assert `error == "invalid_mode"` and `valid` list contains the five enum values.

7. **`memory_graph_explain_impl` clamps depth.** Mock `_lr.get_neighbors` to record the `depth` arg. Test with `depth=0` → mock called with `depth=1`. With `depth=5` → mock called with `depth=3`. With `depth=2` → mock called with `depth=2`.

8. **`memory_graph_explain_impl` surfaces `key_not_found`.** Mock `_lr.get_neighbors` to raise `KeyError`. Assert returned JSON has `error == "key_not_found"`.

9. **`build_channels_prompt_section(channels=[], is_graph_enabled=True)`** includes substring `"Graph-aware memory"` (channels-empty edge case).

10. **`build_channels_prompt_section(channels=[c1], is_graph_enabled=True)`** includes both the Memory Channels block and the Graph-aware memory block, separated by a blank line.

11. **`build_channels_prompt_section(channels=[c1], is_graph_enabled=False)`** does NOT include "Graph-aware memory" substring.

12. **`semantic_search_impl` payload includes `graph_relevance` when hybrid.** Mock the user's store to return a `HybridVectorMemoryStore` whose `search_hybrid` returns entries with `rerank_score=0.42`. Call the impl, parse JSON, assert `results[0]["graph_relevance"] == 0.42`.

13. **`semantic_search_impl` payload omits `graph_relevance` when plain.** Mock `VectorMemoryStore` (plain), assert `"graph_relevance" not in results[0]`.

14. **Tool registration is conditional on env var.** With `OBSCURA_LIGHTRAG=on` and adapter mock present, `make_memory_tool_specs(user)` includes specs for `memory_graph_query` and `memory_graph_explain`. With env off, those names are absent. Assert via `[s.name for s in specs]`.

15. **`atexit` shutdown closes adapters.** Construct two `LightRAGAdapter` instances via mocked `for_user`, call `LightRAGAdapter.close_all()`, assert each adapter's `close()` was invoked exactly once and the `_instances` dict is empty.

### Test fixtures

Reuse the Phase-6 `MockLightRAG` fixture (per `00-overview.md` line 352-356) — Phase 4 tests can assume it exists in `tests/unit/obscura/lightrag_memory/conftest.py`. If Phase 6 hasn't landed yet at the time of Phase 4 implementation, build a minimal local mock in `tests/unit/obscura/tools/conftest.py` and let Phase 6 consolidate.

### Integration tests — out of scope for Phase 4

Real LightRAG end-to-end tests stay opt-in via `RUN_LR_INTEGRATION=1` per Phase 6. Don't gate Phase 4 PR merge on integration coverage.

---

## 14. Open questions / decisions deferred

### A2A peer access — defer to Phase 5

Should `memory_graph_query` be invocable by A2A peers (other agents calling in over gRPC), not just the local user? **Probably yes**: same capability gate handles it, the impl reads user from `ToolContext` so peer-as-user works automatically. **But verify in Phase 5.** Specifically: the A2A transport layer (`obscura/integrations/a2a/`) sets up its own `ToolContext` with the peer's user identity, and the `VectorMemoryStore.for_user(peer_user)` call will return the peer's store (correct isolation). The graph collection is per-user, so cross-peer leakage is not possible. Phase 5 adds this to its acceptance test matrix.

### Web UI rendering — flagged for follow-up

Does the web UI need to render `graph_relevance` for `semantic_search` results? Currently the UI shows score and final_score only. Adding a third score column is a UX decision and a small-but-not-zero diff to the React components. **Out of scope for Phase 4**; spawn a follow-up task once the model-facing surface is stable. Suggested follow-up title: `Add graph_relevance column to web UI semantic search results`.

### Telemetry — A/B shadow logging is Phase 3 territory

`OBSCURA_LIGHTRAG_SHADOW=1` (Phase 3) logs both paths' top-5 results for comparison. Phase 4 doesn't extend this. If the engineer adding Phase 4 finds the shadow logging convenient for their own testing, fine; do not add new shadow paths in this PR.

### Mode coverage — should `memory_graph_query` expose `naive`?

The enum includes `naive` (vector-only, ignoring the graph). It's redundant with `semantic_search` from a results standpoint. Two reasons to keep it:

1. **Symmetry with LightRAG's API** — surfacing all five modes makes the tool's behaviour predictable to anyone who knows LightRAG.
2. **A/B benchmarking** — a model can call `memory_graph_query(q, mode="naive")` and `memory_graph_query(q, mode="hybrid")` and compare. With `naive` hidden, the model would have to switch tools, which it tends to handle worse than switching args.

Keep `naive` in the enum. If telemetry shows the model picking `naive` when `semantic_search` would do, revisit and remove from the enum.

### Backfill triggering from tool errors

If `memory_graph_explain` returns `key_not_found` because the chunk isn't graph-indexed, **should the tool offer to backfill it?** No. Backfill is rate-limited and human-confirmed (Phase 5 spec). A tool that triggers ingest as a side effect violates least-surprise and would let a single agent burn LLM cost without operator intent. The `hint` field tells the model what to do (`store_searchable` re-saves the key, triggering Phase 2 fan-out) — that's enough.

---

## File summary — what gets touched in Phase 4

| File | Change | Lines |
|---|---|---|
| `obscura/vector_memory/vector_memory.py` | Modify `for_user` classmethod + add `_lightrag_enabled()` helper. | ~50 |
| `obscura/tools/memory_tools.py` | Add `_json_error`, `_is_user_graph_enabled`, `memory_graph_query_impl`, `memory_graph_explain_impl`. Modify `build_channels_prompt_section` and `make_memory_tool_specs`. Update `semantic_search_impl`. | ~250 |
| `obscura/cli/__init__.py` | Pass `is_graph_enabled` to `build_channels_prompt_section` (line 1031). | ~5 |
| `obscura/cli/session.py` | Same as above (line 1109). | ~5 |
| `obscura/lightrag_memory/__init__.py` | Add `atexit` registration. | ~20 |
| `obscura/lightrag_memory/adapter.py` | Add `get_neighbors` method, `GraphExplanation` dataclass, `close_all` classmethod. Confirm `close()` exists. | ~80 |
| `obscura/lightrag_memory/hybrid_store.py` | Add `close()` method. | ~15 |
| `tests/unit/obscura/tools/test_memory_graph_tools.py` | NEW — skeleton tests for new tools. | ~200 |
| `tests/unit/obscura/vector_memory/test_for_user.py` | NEW or extend — fallback + env-flag tests. | ~80 |
| `tests/unit/obscura/tools/test_build_channels_prompt.py` | NEW — prompt assembly tests. | ~50 |

Total: ~755 lines of code (new + modified), most concentrated in `memory_tools.py`. No changes to backend protocols, no changes to the agent loop, no new dependencies beyond the `lightrag` extra already added in Phase 1.

---

## Done definition

A reviewer signs off when:

- All 10 acceptance criteria in §2 pass.
- `make lint` and `make typecheck` are clean.
- The 15 unit tests in §13 pass with `OBSCURA_LIGHTRAG=on` and `OBSCURA_LIGHTRAG=off` (both modes exercised).
- A manual smoke test against a single user with `OBSCURA_LIGHTRAG=on`:
  1. `obscura "store_searchable a fact about pyramids and the Nile"` — verify `set()` returns; check `~/.obscura/lightrag/<hash>/` working dir is populated.
  2. `obscura "memory_graph_query Egypt"` — verify the result includes the saved chunk with non-null `graph_relevance` and `final_score`.
  3. `obscura "memory_graph_explain <key returned above>"` — verify entities include "Egypt", "Nile", "pyramids" or similar.
  4. `obscura "semantic_search Egypt"` — verify response now includes `graph_relevance` field on each result.
  5. Toggle env off, restart, repeat (1) — verify no LightRAG side effects (working dir untouched), `memory_graph_query` is **not** in the tool list (run `tool_search "memory"` and confirm).
- No regressions on the existing Phase-3 search tests (`tests/unit/obscura/vector_memory/`).
