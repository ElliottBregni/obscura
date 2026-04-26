# LightRAG-on-Obscura — Implementation Plan

Layer LightRAG-style graph-aware retrieval on top of Obscura's existing Qdrant + decay-based memory. The user's decay/lifecycle logic stays load-bearing — LightRAG owns vector + graph **retrieval**, Obscura still owns memory **lifecycle**.

Hybrid scoring model:

```
score = vector_similarity + graph_relevance + recency_decay + usage_frequency
```

## Documents

| File | Lines | Scope |
|------|-------|-------|
| [00-overview.md](00-overview.md) | 431 | Architecture findings, phase summary, decisions, risks |
| [phase-1-scaffold.md](phase-1-scaffold.md) | 1,293 | `lightrag` extra in `pyproject.toml`, new `obscura.lightrag_memory` package, feature flag, `LightRAGAdapter` skeleton, scoring math module |
| [phase-2-ingest.md](phase-2-ingest.md) | 1,430 | `HybridVectorMemoryStore.set/delete/clear_namespace/close`, `LightRAGAdapter.insert_safe/delete_safe`, `indexable_types` whitelist, async fan-out, telemetry |
| [phase-3-query.md](phase-3-query.md) | 1,942 | `search_hybrid()`, `hybrid_score()` derivation + normalization, `_touch_and_count_async()` wiring, `backend.update_metadata()` protocol addition, three fallback triggers, 400ms latency budget |
| [phase-4-tools.md](phase-4-tools.md) | 1,257 | Single-point `for_user` modification, `memory_graph_query` + `memory_graph_explain` tools, conditional system-prompt section, lifecycle hooks |
| [phase-5-migration.md](phase-5-migration.md) | 1,761 | `BackfillEngine` + `obscura memory backfill-graph` CLI, lazy on-touch token-bucket, consolidator graph-cleanup hook, cost telemetry, single-process locking |
| [phase-6-tests.md](phase-6-tests.md) | 2,610 | `MockLightRAG` fixture, 8-module unit suite, opt-in integration suite via `pytest-recording`, pyproject/conftest/CI diffs |

**Total:** ~10,700 lines of implementation-ready documentation.

## Reading order

1. **[00-overview.md](00-overview.md)** — start here. Architecture findings, the load-bearing facts about the existing codebase, phase summary, and risks. Read top to bottom.
2. **[phase-1-scaffold.md](phase-1-scaffold.md)** — first concrete code. Sets up package structure, dependency extra, feature flag, and pure-math scoring module. No behavior change.
3. **Phases 2-6** — can be read in parallel by separate engineers if work is split, but each builds on Phase 1's scaffolding.

## Phase dependencies

```
Phase 1 (scaffold) ─┬─→ Phase 2 (ingest) ─┬─→ Phase 4 (tools) ─→ Phase 6 (tests)
                    │                      │
                    └─→ Phase 3 (query) ───┤
                                           │
                                           └─→ Phase 5 (migration)
```

- **Phase 3 forward-moves** the `backend.update_metadata()` protocol method originally scoped to Phase 5 (it's needed for `access_count` tracking). Phase 5 then *uses* the same method for `lr_indexed_at` markers.
- **Phase 4** depends on both Phase 2 (writes through `HybridVectorMemoryStore`) and Phase 3 (queries via `search_hybrid`).
- **Phase 6** wraps everything; the `MockLightRAG` fixture is referenced by tests in Phases 2-5.

## Key decisions baked in

- **NetworkX** for graph backend (single-node, zero ops, pickled to `~/.obscura/lightrag/<user>/`). Swap to Postgres+AGE if Obscura ever runs multi-tenant.
- **Reuse Qdrant** for LightRAG's vector storage (collection `obscura_lightrag_<user_hash>`, isolated from existing `obscura_<user_hash>`).
- **Share embedding function** with `_make_default_embedding_fn()` — one model load, no dimension mismatch.
- **Async fan-out on writes** — never block tool calls on LLM-driven entity extraction.
- **Indexable types whitelist:** `{fact, summary, general}`. Skip `episode` to control LLM cost.
- **Default scoring weights:** `vector=0.5, graph=0.3, decay=0.15, usage=0.05`. Tunable via `[vector_memory.lightrag.weights]`.
- **Feature flag default off in v1**: `OBSCURA_LIGHTRAG=on|off`. Single integration point at `VectorMemoryStore.for_user()`.
- **No two-tool-explosion**: only `memory_graph_query` and `memory_graph_explain` are added. Existing `semantic_search` upgrades transparently.

## Effort estimate

| Phase | Effort |
|-------|--------|
| 1 — scaffold | 1 day |
| 2 — ingest | 2-3 days |
| 3 — query | 3-4 days |
| 4 — tools | 1 day |
| 5 — migration | 2 days |
| 6 — tests | 2-3 days |

**Net: ~2 weeks** of focused work; existing decay logic completely untouched.

## Open questions for the user

1. **NetworkX vs. Postgres+AGE** for the graph backend. Default recommendation is NetworkX. Confirm or override.
2. **Default scoring weights** — `vector=0.5, graph=0.3, decay=0.15, usage=0.05`. Bias differently?
3. **Index `episode` memories?** Default no. Opt-in via `metadata={"graph_index": True}` per-write or `[vector_memory.lightrag] indexable_types = ["fact","summary","episode"]` config-wide.
4. **Where to flip the master switch** — `OBSCURA_LIGHTRAG=on` default is off in v1. Plan to flip on in v2 once backfill is comfortable.
