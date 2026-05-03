# Phase 3 — Query Path / Hybrid Score

> **Status:** plan, ready to implement.
> **Owner:** Elliott Bregni (`bregnie34@gmail.com`)
> **Depends on:** Phase 1 (scaffold), Phase 2 (ingest path producing graph state).
> **Drafted:** 2026-04-26
> **Estimated effort:** 3-4 days of focused work.

This is the heart of the LightRAG-on-Obscura integration. Phases 1 and 2 give us a per-user `LightRAGAdapter` and a `HybridVectorMemoryStore` that fans writes out to LightRAG's graph + vector indices. Phase 3 turns those indices into useful retrieval — a `search_hybrid()` method that blends four signals (vector similarity, graph relevance, recency decay, usage frequency) into a single ranked result list and threads through `recall()` without breaking any existing caller.

This phase is unusually dense because it touches the scoring formula (math), the adapter integration (async), the backend protocol (one new method), and a piece of infrastructure that has been broken since the day it was written (`_touch_results_async` is defined on `VectorMemoryStore` at `obscura/vector_memory/vector_memory.py:557` but never called from any read path). Phase 3 fixes that on the way through.

The doc is structured for an engineer to execute it cold. Every section is load-bearing.

---

## 1. Goal & non-goals

### 1.1 What this phase produces

1. **`HybridVectorMemoryStore.search_hybrid()`** — a new public method on the subclass introduced in Phase 2. Returns the same `list[VectorMemoryEntry]` shape as `search_similar` and `search_reranked`, ranked by a four-term blended score.
2. **`hybrid_score()` function** — a pure, stateless scoring function in `obscura/lightrag_memory/scoring.py`. Takes pre-computed component values, returns a final score. Easy to unit-test in isolation.
3. **`HybridWeights` dataclass** — frozen, validated, loadable from `[vector_memory.lightrag.weights]` in `~/.obscura/config.toml`. Defaults baked in.
4. **`access_count` tracking finally working.** A new `_touch_and_count_async()` helper bumps both `accessed_at` and a new `access_count` payload field on every chunk returned from `search_hybrid()`. The existing `_touch_results_async` (line 557) is generalized to support this.
5. **`backend.update_metadata()` protocol method** — a new method on `VectorBackend` (`obscura/vector_memory/backends/base.py`) for partial-payload updates. Implemented for both `QdrantBackend` (`set_payload`) and `SQLiteBackend` (`UPDATE` of the JSON `metadata` column). Phase 5 reuses this for `lr_indexed_at` backfill markers.
6. **`SemanticMemoryMixin.recall()` opt-in** — a new `use_graph: bool = True` kwarg that routes to `search_hybrid` when the store is a `HybridVectorMemoryStore`. Falls back to `search_reranked` when LightRAG is disabled. Zero-risk default (the isinstance check is the safety net).
7. **Telemetry hooks** — per-query metrics for hit count, hydration success, average per-component contribution, latency breakdown, fallback-fired counter. Wired to whatever Obscura's metric sink already is (TBD per engineer — either `obscura.core.deep_log` or a Prometheus-style counter).
8. **A small in-memory LRU result cache** (optional, default-on) keyed on `(user_id, query_hash, mode, top_k, weights_hash)` with a 60s TTL. Reuses `obscura/core/llm_cache.py` machinery — no new cache code.

### 1.2 What this phase explicitly does NOT produce

- **No new model-facing tools.** `memory_graph_query` and `memory_graph_explain` land in Phase 4. Phase 3 changes are entirely server-side; the existing `semantic_search` tool transparently benefits when its underlying store is hybrid.
- **No backfill of legacy chunks into the graph.** Lazy-on-touch and explicit batch CLI are Phase 5 concerns. Phase 3 assumes the graph is whatever Phase 2 has written so far. Any chunk in Qdrant but not yet in LightRAG is simply absent from `lr_hits` — the fallback path covers it.
- **No LLM-based answer synthesis.** We always pass `only_need_context=True` when calling LightRAG's `aquery`. This skips the second-pass LLM that LightRAG would otherwise run to compose a natural-language answer. We're using LightRAG as a retrieval engine, not as a Q&A agent.
- **No per-call weight overrides on tools.** Tool authors get the configured defaults; weight tuning is a config-file activity in Phase 3. Per-call override surfaces in Phase 4 if needed.
- **No multi-namespace queries.** Phase 3 keeps single-namespace semantics matching `search_reranked`. Cross-namespace search is a future phase if a use case emerges.
- **No learned-to-rank weight fitting.** The scoring formula is linear with hand-tuned defaults. Future work could collect (query, click) feedback and fit weights — out of scope.

---

## 2. Acceptance criteria

These are concrete, testable assertions. Phase 3 is done when all of them hold.

1. `store.search_hybrid("any query")` on a `HybridVectorMemoryStore` returns a `list[VectorMemoryEntry]` ranked descending by `final_score`, where `final_score = w_v*vec + w_g*graph + w_d*decay + w_u*usage` with weights from `HybridWeights`.
2. When `LightRAGAdapter.aquery()` returns 0 hits, `search_hybrid()` falls back to `super().search_reranked(query, ...)` and returns its results unchanged in shape (a counter is incremented).
3. When `LightRAGAdapter.aquery()` raises any exception, `search_hybrid()` catches, logs, increments the fallback counter, and falls back to `search_reranked`. Never re-raises.
4. When `LightRAGAdapter.aquery()` exceeds `timeout_ms` (default 400ms, configurable), the in-flight task is cancelled and `search_hybrid()` falls back to `search_reranked` if `fallback_on_timeout=True`. Otherwise raises `TimeoutError`.
5. Every entry returned from `search_hybrid()` has all three of `score`, `rerank_score`, `final_score` populated. Specifically: `score == vector_sim`, `rerank_score == graph_relevance_normalized`, `final_score == hybrid_score(...)`.
6. After a `search_hybrid()` call, every returned entry has its `access_count` payload field incremented by 1 (new entries default 0 → 1; previously-touched 5 → 6) and its `accessed_at` updated to "now". This happens asynchronously — the call returns before the touch completes.
7. `recall(use_graph=True)` on a plain `VectorMemoryStore` (LightRAG disabled) does not raise, does not call `search_hybrid`, and returns the same results as `recall(use_graph=False)`. The `isinstance` check is the gate.
8. `recall(use_graph=True)` on a `HybridVectorMemoryStore` calls `search_hybrid` and propagates `top_k` and `memory_types` correctly.
9. `recall(use_graph=False)` on a `HybridVectorMemoryStore` skips graph entirely and returns `super().search_reranked()` results — useful for cheap consolidator/maintenance work.
10. `HybridWeights(vector=0.5, graph=0.3, decay=0.15, usage=0.05)` is the default. Loading from `[vector_memory.lightrag.weights]` overrides any present field; missing fields fall back to defaults.
11. `HybridWeights` validates: any negative weight raises `ValueError`; weights summing to ≠1.0 emit a `logging.warning` but do not raise (relative ranking is preserved).
12. The new `backend.update_metadata(key, partial_payload)` is implemented on `QdrantBackend` and `SQLiteBackend`. Calling it twice with `{"access_count": 1}` then `{"access_count": 2}` leaves the chunk's payload at `{"access_count": 2, ...other unchanged fields...}`.

---

## 3. The `hybrid_score()` formula — full derivation

The overview document (`plans/lightrag/00-overview.md`) sketches:

```
score = vector_similarity + graph_relevance + recency_decay + usage_frequency
```

Phase 3 must turn that into a defensible, normalized, weighted blend with documented behavior at the edges. This section walks through every term.

### 3.1 Why a linear blend

Three alternatives to a linear blend were considered:

| Approach | Pros | Cons |
|---|---|---|
| **Linear** (chosen) | Interpretable, debuggable, each term's contribution visible per-result, weights tunable by hand | Treats components as independent — no interaction terms |
| **Multiplicative** (e.g. `vec * graph * decay * usage`) | Naturally penalizes any zero | One zero term zeroes the whole score; very sensitive to component scaling; harder to debug |
| **Learned-to-rank** (gradient-boosted ranker on `(vec, graph, decay, usage)` features) | Could fit user feedback signal | No labeled feedback yet; over-engineered for v1; harder to ship and reason about |

A linear blend is the right v1 because:

1. The user's stated mental model in `00-overview.md:11-13` is additive — matching the implementation to the stated model reduces surprise.
2. Each component contribution can be logged individually, which makes weight-tuning a numerical exercise rather than a blind one.
3. A future learned-to-rank model can use the same component vector as input features. We preserve optionality.

The future-work hook is clear: collect `(query_hash, returned_entries, user_feedback_signal)` triples and fit weights offline. The serialized component values are all we need.

### 3.2 Term 1 — `vector_sim`

LightRAG's `aquery` returns hits with similarity scores. With the embedder shared across Obscura and LightRAG (`_make_default_embedding_fn` at `obscura/vector_memory/vector_memory.py:86` produces `all-MiniLM-L6-v2` outputs that are L2-normalized in `_st_embed` line 130-131), cosine similarity is mathematically in `[-1, 1]` but in practice for natural-language embeddings it's effectively in `[0, 1]`. Negative cosine values appear only for genuinely opposing semantic content, which is rare in personal-memory corpora.

**Normalization rule:**

```python
vector_sim = max(0.0, min(1.0, raw_vector_sim))
```

Clamp negatives to 0 (not 0.5 — a weakly negative result shouldn't contribute as much as zero similarity). Clamp >1 to 1 (defensive against any backend returning unnormalized scores).

**Contract assumption:** LightRAG's per-hit field for vector similarity is whichever of `score`, `vdb_score`, `vector_score`, or `similarity` the active mode emits. Document the exact field name in the adapter's `GraphHit` dataclass — the adapter normalizes whatever LightRAG produces into a `vector_sim: float` attribute. If LightRAG changes its output shape between versions, the adapter is the single point of breakage.

**Guard:** in `search_hybrid`, after pulling `hit.vector_sim`, assert `isinstance(hit.vector_sim, (int, float))` and emit a warning + treat as 0 if the value is `None` or `NaN`. Don't crash on adapter-side surprises.

### 3.3 Term 2 — `graph_relevance`

This is the LightRAG-specific signal: how well does this chunk match the query's graph structure (entity overlap + community membership + relation traversal cost)?

**Range varies by mode.** From inspection of LightRAG's source:

- `naive` mode: pure vector. There is no graph score — `graph_relevance` is undefined and we set it to 0. Only `vector_sim` contributes.
- `local` mode: 1-hop entity neighborhood scores. Range typically `[0, 1]` (some implementations use raw edge counts, which need normalization).
- `global` mode: community-summary relevance scores. Range `[0, 1]` after their internal softmax.
- `hybrid` mode: combines local + global, weights internal. The output per-hit is what LightRAG calls a "context score" — its range is mode-implementation-dependent.
- `mix` mode: similar to `hybrid` but with naive vector contribution mixed in.

We cannot trust a fixed `[0, 1]` range across modes. Two normalization options:

**Option A — Min-max normalize within the result set:**

```python
g_min = min(h.graph_score for h in hits)
g_max = max(h.graph_score for h in hits)
g_range = g_max - g_min
normalized = (h.graph_score - g_min) / g_range if g_range > 0 else 0.5
```

**Pros:** Robust to any monotonic transformation LightRAG applies; preserves rank order within the result set; bounded `[0, 1]` by construction.

**Cons:** A result set where all hits have very similar graph scores looks artificially spread out (or all collapses to 0.5 if range is 0). Cross-query comparability is lost — the same chunk can have different normalized graph_relevance in different queries.

**Option B — Logistic squash:**

```python
# Squash any positive raw score to [0, 1] with a steepness tuned per-mode
normalized = 1.0 / (1.0 + math.exp(-k * (raw - midpoint)))
```

**Pros:** Globally stable; same raw score maps to same normalized score across queries.

**Cons:** Requires tuning `k` and `midpoint` per mode; tuning is empirical and brittle to LightRAG version drift.

**Recommendation: Option A (min-max within result set).** Reasoning:

1. We're ranking *within* a single query's hit set. Cross-query comparability is not a goal — `final_score` is only ever sorted within one call.
2. Min-max requires zero tuning. The `g_range > 0` guard handles the degenerate "all the same" case by collapsing to 0.5 (neutral contribution from this term).
3. If LightRAG changes its raw-score scale, min-max keeps working without a tuning pass.

**Edge case — single-hit result set:** `g_range = 0`. We set `normalized = 0.5`. This avoids the result being penalized for having no peers. The other three terms (`vector_sim`, `decay`, `usage`) still drive the absolute `final_score`.

**Edge case — `naive` mode:** No graph score from LightRAG. The adapter populates `graph_score = 0` for all hits in `naive` mode. After min-max with `g_range = 0`, all entries get `0.5` for normalized graph_relevance. This is a feature, not a bug — under naive mode the term contributes a fixed baseline that doesn't perturb ranking, and `vector_sim` does all the work.

### 3.4 Term 3 — `decay_multiplier`

`compute_decay()` (`obscura/vector_memory/decay.py:86`) returns a value already in `[0, 1]`:

- `1.0` if the memory_type is immune (e.g. `preference`, `profile_identity`).
- For decaying types: `0.5 ** (age_days / half_life_days)`, with an access-recency boost folded in.
- Floor isn't enforced inside `compute_decay` — it's used by `is_below_floor` for GC, not by the multiplier itself. So values can be very small (e.g. `1e-6`) for ancient un-touched chunks.

**No transform.** Use the value directly.

**Why this term doesn't double-count with `usage_norm`:** `compute_decay` already does an "access boost" (lines 127-132 of `decay.py`) — chunks accessed within `access_boost_days` get up to a 50% age reduction. `usage_norm` is fundamentally different: it's a *count* of accesses, not a *recency-of-access* signal. A chunk accessed once 3 days ago has decay-boost ≈ recent, but `access_count = 1`. A chunk accessed 50 times over the past year has decay-boost ≈ recent (last access fresh) AND `access_count = 50`. The two signals overlap for trivially-touched memories but diverge sharply for hot ones.

**Call site contract:** `search_hybrid` must call `compute_decay` per hydrated entry, not trust whatever `final_score` the backend already populated (the Qdrant backend at `qdrant_backend.py:344` bakes decay into `final_score` server-side, but here we're computing our own composite — re-applying decay is correct).

### 3.5 Term 4 — `usage_norm`

Raw input: `entry.metadata.get("access_count", 0)` — an integer. Transform with logarithmic saturation:

```python
SATURATION_K = 100  # config-tunable
usage_norm = min(1.0, math.log1p(access_count) / math.log1p(SATURATION_K))
```

**Why `log1p`:** access counts have a power-law distribution. A handful of chunks get hundreds of hits; most get 0-3. Linear normalization (e.g. `count / 100`) compresses the tail brutally — a chunk with `count=50` would only score 0.5, unfairly close to the unloved chunks. Log-scale gives differentiation across the meaningful range.

**Why `SATURATION_K = 100`:** for personal memory:

- 0-3 hits: typical chunk. `usage_norm` ≈ 0 to 0.3.
- 5-15 hits: chunk you actually use. `usage_norm` ≈ 0.39 to 0.6.
- 30-50 hits: hot chunk (frequently re-recalled). `usage_norm` ≈ 0.77 to 0.85.
- 100+ hits: very hot, saturates at 1.0.

This is right-tuned for personal use. A chunk hitting 1000+ times would saturate the same as 100, but that's fine — once a chunk is "very frequently used", further differentiation isn't useful for ranking.

**Tunable via config.** `[vector_memory.lightrag] usage_saturation_k = 100` overrides the default. Different deployments (a multi-user shared agent, an API service with millions of queries) might tune `K` higher.

**Edge case — `access_count` missing.** Legacy entries from before Phase 3 don't have `access_count` in their metadata. `entry.metadata.get("access_count", 0)` returns 0; `log1p(0) = 0`; contribution is 0. Correct: until a chunk is touched by the new path, it gets no usage credit. Phase 5 backfill could backdate `access_count` from a touch-event log if one ever existed (it doesn't, currently).

**Edge case — `access_count` very large.** `log1p(huge) = huge`; `min(1.0, ...)` clips to 1. Fine.

**Edge case — `access_count` is `None` or non-int.** Defensive: `usage_count = int(entry.metadata.get("access_count") or 0)`.

### 3.6 The `HybridWeights` dataclass

```python
# obscura/lightrag_memory/scoring.py

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Saturation constant for usage_norm; tunable via config.
DEFAULT_SATURATION_K = 100


@dataclass(frozen=True)
class HybridWeights:
    """Linear-blend weights for the four hybrid-retrieval signals.

    The four terms — vector similarity, graph relevance, recency decay, and
    usage frequency — are each normalized to ``[0, 1]`` upstream and combined
    here.  Weights need not sum to 1.0; the relative ranking is preserved
    either way.  But we warn (not error) when they don't, because divergence
    from 1.0 makes the absolute score harder to read.
    """

    vector: float = 0.5
    graph: float = 0.3
    decay: float = 0.15
    usage: float = 0.05

    def __post_init__(self) -> None:
        # Reject negatives.
        for name, value in (
            ("vector", self.vector),
            ("graph", self.graph),
            ("decay", self.decay),
            ("usage", self.usage),
        ):
            if value < 0:
                raise ValueError(f"HybridWeights.{name} must be >= 0, got {value}")
        total = self.vector + self.graph + self.decay + self.usage
        if abs(total - 1.0) > 0.001:
            logger.warning(
                "HybridWeights do not sum to 1.0 (sum=%.4f); "
                "ranking is preserved but absolute scores will be off-scale",
                total,
            )

    def fingerprint(self) -> str:
        """Stable hash for cache keys."""
        return f"{self.vector:.4f}_{self.graph:.4f}_{self.decay:.4f}_{self.usage:.4f}"


def hybrid_score(
    *,
    vector_sim: float,
    graph_relevance: float,
    decay_multiplier: float,
    usage_count: int,
    weights: HybridWeights,
    saturation_k: int = DEFAULT_SATURATION_K,
) -> float:
    """Compute the linear-blend hybrid score.

    All four input components are clamped to ``[0, 1]`` defensively, so callers
    don't need to worry about LightRAG's score-shape quirks reaching this far.
    """
    # Defensive clamping — upstream should already have done this, but
    # double-clamping is cheap and turns "garbage in" into "boring out"
    # rather than "ranked first".
    v = max(0.0, min(1.0, vector_sim if vector_sim is not None else 0.0))
    g = max(0.0, min(1.0, graph_relevance if graph_relevance is not None else 0.0))
    d = max(0.0, min(1.0, decay_multiplier if decay_multiplier is not None else 0.0))

    # Usage normalization with log saturation.
    if usage_count is None or usage_count < 0:
        usage_count = 0
    u_raw = math.log1p(usage_count) / math.log1p(saturation_k)
    u = min(1.0, max(0.0, u_raw))

    return (
        weights.vector * v
        + weights.graph * g
        + weights.decay * d
        + weights.usage * u
    )


def load_hybrid_weights_from_disk() -> HybridWeights:
    """Load weights from ``[vector_memory.lightrag.weights]`` in config.toml.

    Falls back to defaults for any missing field.  Returns the dataclass-default
    if the section is absent or unreadable.
    """
    try:
        from pathlib import Path

        from obscura.core.config_io import try_load_config

        home_cfg = try_load_config(Path.home() / ".obscura" / "config.toml")
        section: dict[str, Any] = (
            (home_cfg or {})
            .get("vector_memory", {})
            .get("lightrag", {})
            .get("weights", {})
        )
        return HybridWeights(
            vector=float(section.get("vector", 0.5)),
            graph=float(section.get("graph", 0.3)),
            decay=float(section.get("decay", 0.15)),
            usage=float(section.get("usage", 0.05)),
        )
    except Exception:
        logger.debug("Could not load HybridWeights from disk, using defaults", exc_info=True)
        return HybridWeights()
```

**Note on `fingerprint()`:** the cache key for the result LRU is `(user_id, query_hash, mode, top_k, weights.fingerprint())`. If the user edits `config.toml` and reloads weights mid-process, fingerprints diverge and the cache naturally invalidates per-weight-set. No explicit cache-flush needed.

---

## 4. `search_hybrid()` — full implementation

### 4.1 Signature

```python
def search_hybrid(
    self,
    query: str,
    namespace: str | None = None,
    top_k: int = 5,
    *,
    mode: str = "hybrid",
    first_stage_k: int = 50,
    weights: HybridWeights | None = None,
    timeout_ms: int | None = None,
    fallback_on_timeout: bool = True,
    memory_types: list[str] | None = None,
) -> list[VectorMemoryEntry]:
    ...
```

Mirrors `search_reranked` (`vector_memory.py:450`) for `query`, `namespace`, `top_k`, `first_stage_k`. Adds the four LightRAG-specific kwargs: `mode`, `weights`, `timeout_ms`, `fallback_on_timeout`. Adds `memory_types` (the host wants it propagated — see §7.2).

`namespace=None` semantics match `search_reranked`: search across all namespaces. Most callers pass an explicit namespace though.

### 4.2 Step-by-step body

```python
# obscura/lightrag_memory/hybrid_store.py

import asyncio
import logging
import time
from typing import Any

from obscura.lightrag_memory.scoring import (
    HybridWeights,
    hybrid_score,
    load_hybrid_weights_from_disk,
)
from obscura.memory import MemoryKey
from obscura.vector_memory.decay import compute_decay
from obscura.vector_memory.vector_memory import VectorMemoryStore
from obscura.vector_memory.backends.base import VectorEntry

logger = logging.getLogger(__name__)


class HybridVectorMemoryStore(VectorMemoryStore):
    # ... (Phase 2 set/delete overrides assumed present) ...

    # Cached weights — invalidated on first config-mtime change.  Keeps every
    # query from re-parsing config.toml.  Simple impl: parse once, hold; a
    # future SIGHUP-style reload could invalidate.
    _cached_weights: HybridWeights | None = None

    def _resolve_weights(self, weights: HybridWeights | None) -> HybridWeights:
        if weights is not None:
            return weights
        if self._cached_weights is None:
            self._cached_weights = load_hybrid_weights_from_disk()
        return self._cached_weights

    def search_hybrid(
        self,
        query: str,
        namespace: str | None = None,
        top_k: int = 5,
        *,
        mode: str = "hybrid",
        first_stage_k: int = 50,
        weights: HybridWeights | None = None,
        timeout_ms: int | None = None,
        fallback_on_timeout: bool = True,
        memory_types: list[str] | None = None,
    ) -> list[VectorMemoryEntry]:
        """Hybrid retrieval blending vector + graph + decay + usage.

        Falls back to ``super().search_reranked()`` on:
        - LightRAG empty result
        - LightRAG raises
        - LightRAG exceeds ``timeout_ms``

        Returns the same shape as ``search_reranked`` for caller compatibility.
        """
        t_start = time.monotonic()
        weights = self._resolve_weights(weights)
        timeout_ms = timeout_ms if timeout_ms is not None else self._lr_default_timeout_ms()

        # 1. LightRAG retrieval (async, with optional timeout).
        try:
            t_lr_start = time.monotonic()
            lr_hits = self._run_aquery_blocking(
                query=query,
                namespace=namespace,
                mode=mode,
                top_k=first_stage_k,
                timeout_ms=timeout_ms,
            )
            t_lr_ms = (time.monotonic() - t_lr_start) * 1000
        except asyncio.TimeoutError:
            self._emit_metric("hybrid_query_timeout", 1, mode=mode)
            if fallback_on_timeout:
                return self._fallback_to_reranked(
                    query, namespace, top_k, memory_types, reason="timeout"
                )
            raise
        except Exception:
            logger.exception("LightRAG aquery failed; falling back to vector-only")
            self._emit_metric("hybrid_query_error", 1, mode=mode)
            return self._fallback_to_reranked(
                query, namespace, top_k, memory_types, reason="exception"
            )

        if not lr_hits:
            self._emit_metric("hybrid_query_empty", 1, mode=mode)
            return self._fallback_to_reranked(
                query, namespace, top_k, memory_types, reason="empty"
            )

        # 2. Hydrate hits via backend.get_vector.  Drop drift (chunk in graph
        #    but absent from Qdrant — cleared/expired since ingest).
        t_hydrate_start = time.monotonic()
        hydrated: list[tuple[VectorEntry, float, float]] = []
        # Each tuple: (entry, raw_vector_sim, raw_graph_score)
        drift_count = 0
        for hit in lr_hits:
            # Namespace post-filter (see §4.4 for rationale).
            if namespace is not None and hit.namespace != namespace:
                continue
            entry = self.backend.get_vector(MemoryKey(hit.namespace, hit.key))
            if entry is None:
                drift_count += 1
                continue
            # Memory-type filter at hydrate time.
            if memory_types is not None and entry.memory_type not in memory_types:
                continue
            hydrated.append((entry, hit.vector_sim, hit.graph_score))

        if not hydrated:
            # Either everything drifted away, or memory_types filter ate them.
            # Either way, falling back is the right move.
            self._emit_metric("hybrid_query_all_drift", 1, mode=mode)
            return self._fallback_to_reranked(
                query, namespace, top_k, memory_types, reason="hydration_empty"
            )

        if drift_count:
            logger.info(
                "search_hybrid: dropped %d/%d drift hits (graph references "
                "absent from backend)",
                drift_count,
                len(lr_hits),
            )
            self._emit_metric("hybrid_drift_drops", drift_count, mode=mode)

        # 3. Normalize graph_relevance via min-max within the hit set.
        graph_raw = [g for (_, _, g) in hydrated]
        g_min = min(graph_raw)
        g_max = max(graph_raw)
        g_range = g_max - g_min

        def normalize_g(raw: float) -> float:
            if g_range <= 0:
                return 0.5  # See §3.3 "all-same" edge case.
            return (raw - g_min) / g_range

        # 4. Score and populate component fields.
        t_score_start = time.monotonic()
        scored: list[VectorEntry] = []
        component_sums = {"v": 0.0, "g": 0.0, "d": 0.0, "u": 0.0}
        for entry, raw_vec, raw_graph in hydrated:
            vec_sim = max(0.0, min(1.0, raw_vec))
            graph_norm = normalize_g(raw_graph)
            decay_mult = compute_decay(
                entry.memory_type,
                entry.created_at,
                entry.accessed_at,
                self.decay_config,
            )
            usage_count = int(entry.metadata.get("access_count") or 0)

            entry.score = vec_sim
            entry.rerank_score = graph_norm
            entry.final_score = hybrid_score(
                vector_sim=vec_sim,
                graph_relevance=graph_norm,
                decay_multiplier=decay_mult,
                usage_count=usage_count,
                weights=weights,
            )
            scored.append(entry)
            component_sums["v"] += weights.vector * vec_sim
            component_sums["g"] += weights.graph * graph_norm
            component_sums["d"] += weights.decay * decay_mult
            component_sums["u"] += weights.usage * min(
                1.0,
                __import__("math").log1p(usage_count) / __import__("math").log1p(100),
            )

        t_score_ms = (time.monotonic() - t_score_start) * 1000

        # 5. Sort and truncate.
        scored.sort(key=lambda e: e.final_score, reverse=True)
        results = scored[:top_k]

        # 6. Bump access_count + accessed_at on returned entries.  Async,
        #    fire-and-forget — query latency unaffected.
        self._touch_and_count_async(results)

        # 7. Telemetry.
        n = max(1, len(scored))
        self._emit_query_telemetry(
            query=query,
            mode=mode,
            top_k=top_k,
            first_stage_k=first_stage_k,
            n_lr_hits=len(lr_hits),
            n_hydrated=len(hydrated),
            n_returned=len(results),
            drift_count=drift_count,
            t_total_ms=(time.monotonic() - t_start) * 1000,
            t_lr_ms=t_lr_ms,
            t_hydrate_ms=(time.monotonic() - t_hydrate_start) * 1000 - t_score_ms,
            t_score_ms=t_score_ms,
            avg_v=component_sums["v"] / n,
            avg_g=component_sums["g"] / n,
            avg_d=component_sums["d"] / n,
            avg_u=component_sums["u"] / n,
            fallback=False,
        )

        return results
```

### 4.3 Helper — `_run_aquery_blocking`

LightRAG's `aquery` is async. The agent loop and most call sites are themselves async, but `search_similar` and `search_reranked` are sync, and we want `search_hybrid` to match the same call shape (the rest of the codebase calls these synchronously). The adapter from Phase 1 maintains a dedicated event loop in a daemon thread; we drive it with `asyncio.run_coroutine_threadsafe`:

```python
def _run_aquery_blocking(
    self,
    *,
    query: str,
    namespace: str | None,
    mode: str,
    top_k: int,
    timeout_ms: int | None,
) -> list[Any]:  # list[GraphHit] — adapter-defined dataclass
    """Blocking wrapper around the adapter's async aquery, with timeout."""
    coro = self._lr.aquery(
        query=query,
        namespace=namespace,
        mode=mode,
        top_k=top_k,
        only_need_context=True,  # critical — skips LLM answer synthesis
    )
    timeout_s = (timeout_ms / 1000.0) if timeout_ms else None
    # The adapter exposes its event loop; this runs the coroutine on it
    # from the calling thread and blocks until completion (or timeout).
    future = asyncio.run_coroutine_threadsafe(coro, self._lr.loop)
    try:
        return future.result(timeout=timeout_s)
    except asyncio.TimeoutError:
        # Cancel the in-flight task on the adapter's loop so it doesn't
        # keep running and consume LightRAG resources.
        future.cancel()
        raise
```

`only_need_context=True` is the critical latency-saving flag. Without it, LightRAG runs an LLM pass to compose a natural-language answer from the retrieved context — that's the part we don't want. With it, we get back just the retrieved-context structures, which is exactly what we'll feed into our own scoring.

### 4.4 Step 4 detail — namespace filtering

Two routes:

**Route A — Push the namespace into LightRAG's metadata-filter at query time.** LightRAG's `QueryParam` supports a `metadata_filter` field on some modes (varies by version). If supported in our version, build `{"obscura_namespace": namespace}` and pass it. Pros: filtering happens before graph traversal, faster; smaller hit set. Cons: not all modes support it; relies on adapter populating the filter correctly.

**Route B — Post-filter on hydrated hits.** After getting the raw hits, drop any whose `namespace != target_namespace`. Pros: works regardless of LightRAG mode/version. Cons: a query for namespace `foo` might return mostly `bar` hits and waste graph-traversal effort.

**Recommendation: A if available, B as fallback, both as a safety belt.**

The implementation in §4.2 has both: `_run_aquery_blocking` passes `namespace` to the adapter (which embeds it as a metadata filter if the mode supports it), AND the hydration loop post-filters via `if namespace is not None and hit.namespace != namespace: continue`. The post-filter is cheap and idempotent — if the adapter already filtered, the post-filter just confirms.

The adapter (Phase 1's `LightRAGAdapter.aquery`) needs to translate `namespace` to LightRAG's filter shape and accept any mode. If a mode doesn't support metadata filters, the adapter logs a debug-level warning and relies on post-filter. This is documented in the adapter, not here.

### 4.5 Step 5 detail — hydration

```python
entry = self.backend.get_vector(MemoryKey(hit.namespace, hit.key))
```

The backend's `get_vector` (Qdrant: `qdrant_backend.py:231`, SQLite: equivalent) returns `VectorEntry | None`. We check for `None` and treat it as drift. Drift means LightRAG's graph still references a chunk that's no longer in Qdrant — possibly because:

1. The user called `clear_namespace()` or `delete()` and the LightRAG-side delete didn't fire (Phase 2 includes the `delete` override that calls `lr_adapter.delete_safe` — but a race or a crash mid-delete could leave drift).
2. The chunk's `expires_at` passed since the graph was last consolidated, and `purge_expired` (Qdrant `qdrant_backend.py:167`) has run while LightRAG is unaware.
3. A future migration changed key schemas without re-indexing.

The drift counter is per-query; if it crosses a threshold (say 25% of hits), Phase 5's backfill should be considered. We log the count and emit a metric; we do not block on it.

**Important:** when hydrating, we deliberately do NOT trust LightRAG's payload for canonical text. The `text`, `metadata`, `created_at`, `accessed_at`, `memory_type` all come from the Obscura backend. This guarantees that if a chunk's text was edited via `set()` after being graph-indexed, we use the latest text — not whatever LightRAG cached in its own vector store. Decay computation also uses backend-canonical timestamps. The graph is for routing; the backend is for content.

### 4.6 Step 6 detail — `_touch_and_count_async`

See §5 below for the full implementation.

### 4.7 Helper — `_fallback_to_reranked`

```python
def _fallback_to_reranked(
    self,
    query: str,
    namespace: str | None,
    top_k: int,
    memory_types: list[str] | None,
    *,
    reason: str,
) -> list[VectorMemoryEntry]:
    """Vector-only fallback path.  Optionally reformats final_score to be
    comparable with the hybrid path (see §6 reasoning)."""
    self._emit_metric("hybrid_fallback", 1, reason=reason)
    results = super().search_reranked(
        query=query,
        namespace=namespace,
        top_k=top_k,
        memory_types=memory_types,
    )
    # Re-format final_score in the hybrid frame for comparability.
    weights = self._resolve_weights(None)
    for e in results:
        decay_mult = compute_decay(
            e.memory_type, e.created_at, e.accessed_at, self.decay_config,
        )
        usage_count = int(e.metadata.get("access_count") or 0)
        # Treat e.score as vector_sim; graph_relevance = 0 (no graph signal).
        e.rerank_score = 0.0  # graph contribution
        e.final_score = hybrid_score(
            vector_sim=max(0.0, min(1.0, e.score or 0.0)),
            graph_relevance=0.0,
            decay_multiplier=decay_mult,
            usage_count=usage_count,
            weights=weights,
        )
    # Re-sort by the new final_score (was sorted by RecencyReranker's product).
    results.sort(key=lambda x: x.final_score, reverse=True)
    # Touch returned entries on fallback path too — usage tracking is path-
    # agnostic.
    self._touch_and_count_async(results)
    return results[:top_k]
```

Reasoning in §6.

### 4.8 Helper — `_lr_default_timeout_ms`

```python
def _lr_default_timeout_ms(self) -> int:
    """Default query timeout from config, with env var override."""
    import os
    env_val = os.environ.get("OBSCURA_LIGHTRAG_TIMEOUT_MS")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    try:
        from pathlib import Path
        from obscura.core.config_io import try_load_config
        cfg = try_load_config(Path.home() / ".obscura" / "config.toml") or {}
        return int(cfg.get("vector_memory", {}).get("lightrag", {}).get(
            "query_timeout_ms", 400,
        ))
    except Exception:
        return 400
```

### 4.9 Helper — `_emit_metric` and `_emit_query_telemetry`

These are stubs the engineer should wire to whatever metric system Obscura uses. Both should be no-throw:

```python
def _emit_metric(self, name: str, value: int = 1, **tags: Any) -> None:
    try:
        from obscura.core.deep_log import emit_metric  # or wherever
        emit_metric(f"vector_memory.lightrag.{name}", value, **tags)
    except Exception:
        logger.debug("metric emit failed: %s", name, exc_info=True)

def _emit_query_telemetry(self, **kw: Any) -> None:
    """Single structured log line per query, plus per-component metrics."""
    logger.info("hybrid_query %s", kw)
    self._emit_metric("hybrid_query_count", 1, mode=kw.get("mode"))
    self._emit_metric("hybrid_query_latency_ms", int(kw.get("t_total_ms", 0)),
                      mode=kw.get("mode"))
```

Implementation detail; engineer can adapt to whatever sink already exists. The thing that matters is the data captured (see §11).

---

## 5. `_touch_and_count_async` — the usage-tracking wiring

### 5.1 Where it lives

On `HybridVectorMemoryStore`, not on the base `VectorMemoryStore`. Reasoning:

- The existing `_touch_results_async` (`vector_memory.py:557`) is dead code — never called from any read path. We could fix that on the base class as part of Phase 3, but that broadens blast radius.
- Keep Phase 3's footprint on the base class to *zero* changes. The base class already exposes `backend.touch_vector(key)`; the subclass calls it plus the new `backend.update_metadata` for the count.
- Phase 5 or a separate cleanup task can revisit and move the touch wiring into the base class for `search_similar`/`search_reranked` once that's deemed safe.

### 5.2 What it does

For each returned entry:

1. Increment `access_count` payload field by 1.
2. Update `accessed_at` to now (UTC).

Both updates go through `backend.update_metadata(key, partial)` — see §5.5 for that protocol method.

### 5.3 Why "async"

Touching `accessed_at` on the read path shouldn't block the query return. Users see retrieval results; they don't see touch acks. The existing pattern in `_touch_results_async` (background daemon thread, fire-and-forget) is the right shape. We extend it with the `access_count` increment.

### 5.4 Implementation

```python
import contextlib
import threading
from datetime import UTC, datetime


class HybridVectorMemoryStore(VectorMemoryStore):

    def _touch_and_count_async(self, entries: list[VectorEntry]) -> None:
        """Background-touch each entry: bump accessed_at + access_count.

        Fire-and-forget.  No synchronization with the calling thread; if
        the process exits mid-touch, the lost update is tolerable because
        access_count is advisory (see §5.6).
        """
        if not entries:
            return

        # Snapshot what we need from each entry — entries themselves may be
        # mutated by other code by the time the thread runs.
        snapshots = [
            (e.key, int(e.metadata.get("access_count") or 0))
            for e in entries
        ]
        # Optimistic local mutation so callers see the new value immediately.
        for e in entries:
            old = int(e.metadata.get("access_count") or 0)
            e.metadata["access_count"] = old + 1

        def _do() -> None:
            now_iso = datetime.now(UTC).isoformat()
            for key, old_count in snapshots:
                with contextlib.suppress(Exception):
                    self.backend.update_metadata(
                        key,
                        {
                            "access_count": old_count + 1,
                            "accessed_at": now_iso,
                        },
                    )

        # daemon=True: fire-and-forget; lost touches on shutdown are
        # acceptable for advisory data.
        t = threading.Thread(target=_do, daemon=True)
        t.start()
```

The optimistic local mutation is a small UX win: if a caller checks `e.metadata["access_count"]` synchronously after `search_hybrid` returns, they see the new value. The async write then makes it durable.

### 5.5 The new `backend.update_metadata` protocol method

`backend.update_metadata` does not exist yet. The overview anticipates it as a Phase 5 deliverable (`00-overview.md:330`). For Phase 3 we move it forward — Phase 5 just reuses it for `lr_indexed_at` backfill markers.

**Protocol addition** (`obscura/vector_memory/backends/base.py`):

```python
@runtime_checkable
class VectorBackend(Protocol):
    # ... existing methods ...

    def update_metadata(
        self,
        key: MemoryKey,
        partial: dict[str, Any],
    ) -> bool:
        """Merge ``partial`` into the entry's payload.  Top-level fields like
        ``accessed_at`` and ``access_count`` write to the payload root.  Any
        other keys are merged into ``metadata`` (the user-facing JSON dict).

        Returns True if the key existed and was updated, False if absent.
        Implementations must be safe to call concurrently — last-write-wins
        is acceptable; see §5.6 for the rationale on count races.
        """
        ...
```

**`QdrantBackend.update_metadata`:**

```python
def update_metadata(self, key: MemoryKey, partial: dict[str, Any]) -> bool:
    point_id = _point_id(key.namespace, key.key)
    # Split partial: known top-level fields go straight to payload root;
    # everything else is merged into metadata.
    TOP_LEVEL = {"accessed_at", "access_count", "lr_indexed_at"}
    top_level = {k: v for k, v in partial.items() if k in TOP_LEVEL}
    metadata_part = {k: v for k, v in partial.items() if k not in TOP_LEVEL}

    try:
        if metadata_part:
            # Need to read-modify-write metadata since Qdrant's set_payload
            # replaces the value at the path.  For the metadata sub-dict we
            # want a merge.
            existing = self.client.retrieve(
                self.collection_name,
                [point_id],
                with_payload=["metadata"],
                with_vectors=False,
            )
            if not existing:
                return False
            current_md = existing[0].payload.get("metadata", {}) or {}
            new_md = {**current_md, **metadata_part}
            payload = {**top_level, "metadata": new_md}
        else:
            payload = top_level

        self.client.set_payload(self.collection_name, payload, [point_id])
        return True
    except Exception:
        logger.debug("update_metadata failed for %s:%s", key.namespace, key.key,
                     exc_info=True)
        return False
```

**`SQLiteBackend.update_metadata`:**

```python
def update_metadata(self, key: MemoryKey, partial: dict[str, Any]) -> bool:
    """Merge partial into the row's metadata + top-level columns."""
    import json
    TOP_LEVEL_COLS = {"accessed_at": "accessed_at"}
    # accessed_at maps to a column; everything else folds into metadata JSON.
    conn = self._get_conn()
    cur = conn.execute(
        "SELECT metadata FROM vector_memory WHERE namespace = ? AND key = ?",
        (key.namespace, key.key),
    )
    row = cur.fetchone()
    if row is None:
        return False
    current_md = json.loads(row["metadata"] or "{}")
    md_updates = {k: v for k, v in partial.items()
                  if k != "accessed_at"}
    new_md = {**current_md, **md_updates}
    accessed_at = partial.get("accessed_at")
    if accessed_at is not None:
        conn.execute(
            "UPDATE vector_memory SET metadata = ?, accessed_at = ? "
            "WHERE namespace = ? AND key = ?",
            (json.dumps(new_md), accessed_at, key.namespace, key.key),
        )
    else:
        conn.execute(
            "UPDATE vector_memory SET metadata = ? "
            "WHERE namespace = ? AND key = ?",
            (json.dumps(new_md), key.namespace, key.key),
        )
    conn.commit()
    return True
```

**Note on SQLite schema:** the existing schema (lines 86-99) has `accessed_at` in `metadata` JSON, not as a column. Check during implementation whether the column exists; if not, either add a migration to introduce one (preferred — avoid JSON parsing for hot path) or fold `accessed_at` into the JSON merge above. The Qdrant backend uses payload root.

### 5.6 Concurrency — race on `access_count`

Two concurrent `search_hybrid` calls hitting the same chunk:

- T0: Call A reads `access_count = 5`, schedules `update_metadata({access_count: 6, ...})`.
- T0+ε: Call B reads `access_count = 5` (before A's write lands), schedules `update_metadata({access_count: 6, ...})`.
- T1: Both writes hit, last-write-wins. Final value = 6, not 7.

**This is acceptable.** `access_count` is advisory. We don't make decisions on it that need exactness — we use it as a continuous-ish signal for ranking. Losing a count here and there in the noise is fine. The rare degenerate case is two queries hitting the same chunk in a tight loop, where the count may stay at N rather than going to N+2 — but even then, the chunk is still incrementing, just at half the expected rate.

If exact counts ever matter (e.g., for an audit log or quota enforcement), the right move is:

- Switch to a separate counter store with atomic increment (Redis `INCR`, or a SQLite `UPDATE ... SET access_count = access_count + 1`).
- Or keep the JSON-blob style but use a per-chunk lock (per-key threading.Lock from a WeakValueDictionary).
- Don't try to make Qdrant's `set_payload` atomic — it's not designed for it.

**For Phase 3:** document the race in code comments, accept it, move on.

### 5.7 Backwards compat — `access_count` missing

Pre-Phase-3 chunks have no `access_count` in their metadata. The `int(entry.metadata.get("access_count") or 0)` pattern handles this cleanly:

- Reading: 0.
- Writing: optimistic local mutation sets it to 1; async write persists.

After one `search_hybrid` call, the chunk has `access_count = 1`. Subsequent calls increment normally. No migration needed.

---

## 6. Fallback behavior — graceful degradation

### 6.1 Fallback triggers

Three conditions trigger fallback to `super().search_reranked()`:

| Trigger | Detection | Counter | Default behavior |
|---|---|---|---|
| LightRAG returns 0 hits | `len(lr_hits) == 0` | `hybrid_query_empty` | Always fallback |
| LightRAG raises | `try/except Exception` around `_run_aquery_blocking` | `hybrid_query_error` | Always fallback (never re-raise) |
| LightRAG times out | `asyncio.TimeoutError` | `hybrid_query_timeout` | Fallback if `fallback_on_timeout=True` (default) |
| Hydration drops everything | `len(hydrated) == 0` after drift filter | `hybrid_query_all_drift` | Always fallback |

The first three are all-or-nothing. The fourth (hydration empty) covers the case where LightRAG returns hits but every one is drift — the chunks were deleted from Qdrant. Functionally indistinguishable from "graph empty" for the user.

### 6.2 Why never re-raise on `Exception`

`search_hybrid` is called from `recall()` and ultimately from agent tool invocations. A retrieval failure should NEVER propagate as a tool-call error if there's any reasonable fallback. The user's mental model is "find me memories"; "there's a NetworkX pickle corruption deep in the graph engine" is implementation detail. We log it, count it, and degrade.

The one exception (sic) is `asyncio.TimeoutError` when `fallback_on_timeout=False` — this is an explicit caller choice. If a tool wants to enforce a strict latency budget and prefers an error to slow results, it can opt out of the fallback. But the default is fallback, because the agent loop generally prefers degraded results over failed tools.

### 6.3 The `final_score` reformat in fallback

Naive fallback would just return whatever `search_reranked` produced — but its `final_score` is `vector_sim * recency_decay` (`vector_memory.py:511`), which is on a different scale and shape than hybrid's `weights.vector*v + ... + weights.usage*u`.

Why does this matter?

- **Comparability for shadow-mode A/B (Phase 4 wires that):** if we want to compare top-K from the hybrid path against top-K from the fallback path, we need the scores to be on the same scale. Reformat means `final_score` is always in the hybrid frame.
- **Stable contract for downstream consumers:** the model prompt rendering may include `final_score` in the displayed candidate; a sudden scale shift on fallback would be confusing.
- **Future-proof:** if Phase 4 adds a tool that exposes `final_score`, we don't have to special-case "is this a hybrid or fallback result?"

The reformat is in `_fallback_to_reranked` (§4.7): we treat `e.score` as `vector_sim`, set `graph_relevance = 0`, recompute decay, look up `usage_count` from existing metadata, and run `hybrid_score` with `graph_relevance=0`. The `weights.graph * 0 = 0` term drops out, so fallback scores are slightly lower than hybrid scores for the same chunk (no graph contribution) — which is actually a feature: when ranking is comparable cross-path, we naturally prefer hybrid hits when both paths return.

### 6.4 Observability — fallback rate as a health signal

If the fallback counter consistently exceeds, say, 5% of queries, something is wrong:

- LightRAG instance is broken or unreachable.
- The graph is empty (Phase 5 backfill needed).
- Timeouts firing systematically (latency budget too tight or LightRAG misconfigured).

The Phase 4 metrics dashboard surfaces the fallback rate; for Phase 3 we just emit the counters and trust that ops will look at them.

---

## 7. `recall()` integration on `SemanticMemoryMixin`

### 7.1 The current shape

`obscura/vector_memory/vector_memory.py:665`:

```python
def recall(
    self,
    query: str,
    top_k: int = 3,
    memory_types: list[str] | None = None,
    use_reranking: bool = True,
    recency_weight: float = 0.2,
) -> list[VectorMemoryEntry]:
    """Recall semantically similar memories with optional reranking."""
    namespace = f"{self.config.memory_namespace}:semantic"

    if use_reranking:
        return self.vector_memory.search_reranked(
            query,
            namespace=namespace,
            top_k=top_k,
            memory_types=memory_types,
            recency_weight=recency_weight,
        )

    return self.vector_memory.search_similar(
        query,
        namespace=namespace,
        top_k=top_k,
        memory_types=memory_types,
    )
```

### 7.2 The Phase 3 diff

Add a `use_graph` kwarg, default `True`:

```python
def recall(
    self,
    query: str,
    top_k: int = 3,
    memory_types: list[str] | None = None,
    *,
    use_graph: bool = True,
    use_reranking: bool = True,
    recency_weight: float = 0.2,
) -> list[VectorMemoryEntry]:
    """Recall semantically similar memories.

    Routes through the hybrid (graph-aware) path when the underlying store
    is a ``HybridVectorMemoryStore`` and ``use_graph`` is True (default).
    Otherwise falls back to two-stage rerank, then plain similarity.
    """
    namespace = f"{self.config.memory_namespace}:semantic"

    if use_graph:
        # Lazy import to avoid pulling lightrag deps when disabled.
        try:
            from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
            if isinstance(self.vector_memory, HybridVectorMemoryStore):
                return self.vector_memory.search_hybrid(
                    query,
                    namespace=namespace,
                    top_k=top_k,
                    memory_types=memory_types,
                )
        except ImportError:
            # lightrag_memory module not present — fall through.
            pass

    if use_reranking:
        return self.vector_memory.search_reranked(
            query,
            namespace=namespace,
            top_k=top_k,
            memory_types=memory_types,
            recency_weight=recency_weight,
        )

    return self.vector_memory.search_similar(
        query,
        namespace=namespace,
        top_k=top_k,
        memory_types=memory_types,
    )
```

### 7.3 Why this is zero-risk

Three layers of safety:

1. **Lazy import.** The `obscura.lightrag_memory` module is in the optional `lightrag` extra. If it's not installed, the import fails, we catch `ImportError`, and the rest of the function runs as before.
2. **`isinstance` check.** Even if the module is installed, a plain `VectorMemoryStore` (LightRAG disabled at the `for_user()` factory level) is not a `HybridVectorMemoryStore`, so the check fails and we fall through.
3. **`use_graph=False` opt-out.** Callers who want explicit vector-only retrieval (consolidator, low-latency paths) pass `use_graph=False` and skip the graph path entirely.

The default is `use_graph=True`, but the actual behavior change only happens when LightRAG is opted in *at deploy time* via `for_user()`. So `use_graph=True` at the `recall()` level is just "use graph if available" — it doesn't force LightRAG to load.

### 7.4 Memory-type filter propagation

`memory_types` must propagate into `search_hybrid`. The signature in §4.1 includes it; the body in §4.2 applies it at hydrate time (skip entries whose `memory_type` isn't in the filter).

Why filter at hydrate time, not in the LightRAG query?

- LightRAG's metadata filter doesn't always support multi-value matches across modes. Passing `memory_types=["fact", "summary"]` is a list; LightRAG's filter might only support a single equality.
- Hydrate-time filtering uses Obscura's own metadata, which is canonical. The graph's snapshot of `memory_type` could be stale if a chunk's type was changed via `set()` (which Phase 2 supports — re-indexing on upsert).
- It's negligibly slower — the hydration loop already exists.

If a future iteration finds the LightRAG filter is reliable, we can push it down. For Phase 3, hydrate-time is the safe default.

### 7.5 The two existing call sites

`SemanticMemoryMixin.recall()` is the canonical agent entry. It's called from `obscura/agent/agents.py:1129-1167` (`_load_relevant_memory`). That call site doesn't currently pass `use_graph`, so it picks up the default `True` automatically — meaning agents on a hybrid store transparently get graph-aware retrieval. No agent-side changes.

The CLI bridge (`obscura/cli/vector_memory_bridge.py`) is the other path the user mentioned. Spot-check it during implementation; it calls `search_*` directly on the store, not through `recall()`, so the routing logic in `recall()` is bypassed there. If `vector_memory_bridge.py` should also pick up hybrid, route it through `search_hybrid` when the store is a `HybridVectorMemoryStore`. Document this as an extension if discovered, but don't expand Phase 3 scope.

---

## 8. Performance & latency budget

### 8.1 Expected latencies per mode

Based on LightRAG's documented mode behaviors and `only_need_context=True`:

| Mode | What it does | Expected latency (p50) | Expected latency (p99) |
|---|---|---|---|
| `naive` | Pure vector search via LightRAG's vector store | 30-50 ms | 80-120 ms |
| `local` | Entity-match + 1-hop neighborhood traversal | 100-200 ms | 300-500 ms |
| `global` | Community-summary lookup over the graph | 150-300 ms | 400-700 ms |
| `hybrid` | Local + global combined | 200-500 ms | 600-1000 ms |
| `mix` | Hybrid + naive vector mixed in | 250-600 ms | 700-1200 ms |

Plain Qdrant via `search_reranked` (the fallback path) runs ~20-50 ms. So even at best-case `naive` mode, hybrid retrieval has a 10-30 ms graph-overhead penalty. At default `hybrid` mode, the overhead is 200-450 ms.

### 8.2 The 400ms budget

`OBSCURA_LIGHTRAG_TIMEOUT_MS=400` (overridable via config). On timeout, fall back to vector-only.

Why 400ms specifically:

- Below `local` mode's p99, so most queries complete.
- Above `hybrid` mode's p50, so queries that take longer than typical fail fast.
- Within human-perceivable response budget (humans notice ~200ms; 400ms feels deliberate, not slow).
- Leaves headroom: the agent loop has its own latency overhead (model streaming, tool wrapping, etc.); 400ms here puts the per-tool budget at ~500-600ms, which is plausible.

Tuning advice for engineers:

- If `hybrid_query_timeout` rate is high (>10%), bump the timeout or downgrade the default mode to `local`.
- If `hybrid_query_count` >> `hybrid_query_count{mode=hybrid}`, the engineer is probably calling with `mode="local"` defaults from somewhere — check config and call sites.

### 8.3 Result caching

The query path is read-heavy. Within a single REPL/agent session, the same prompt may resolve to similar `recall()` calls 5-10 times. A small in-memory LRU with TTL would dedupe.

**Reuse `obscura/core/llm_cache.py`'s `LLMCache` machinery.** That module already implements:

- Thread-safe `OrderedDict`-backed LRU.
- TTL via `is_expired()`.
- Hit/miss/eviction statistics.

Build a thin `HybridQueryCache` wrapper:

```python
# obscura/lightrag_memory/query_cache.py

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from obscura.core.llm_cache import LLMCache

logger = logging.getLogger(__name__)


@dataclass
class HybridQueryCache:
    """Thin wrapper around LLMCache for hybrid-search results."""

    inner: LLMCache

    @classmethod
    def default(cls) -> "HybridQueryCache":
        return cls(inner=LLMCache(max_entries=128, default_ttl=60.0))

    def _key(
        self,
        *,
        user_id: str,
        query: str,
        mode: str,
        top_k: int,
        weights_fp: str,
        namespace: str | None,
        memory_types: tuple[str, ...] | None,
    ) -> str:
        payload = json.dumps(
            {
                "u": user_id,
                "q": query,
                "m": mode,
                "k": top_k,
                "w": weights_fp,
                "ns": namespace,
                "mt": memory_types,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, **kwargs: Any) -> list[Any] | None:
        # LLMCache stores strings; we serialize the result list.
        # See note below — this is one of the rough edges of reusing the cache.
        ...

    def put(self, *, value: list[Any], **kwargs: Any) -> None:
        ...
```

**The rough edge:** `LLMCache` stores strings (response text). We'd need to JSON-serialize `list[VectorMemoryEntry]` — but `VectorMemoryEntry` includes `datetime` and other non-JSON types. Two paths:

**Path A — Serialize entries:** custom encoder, fully reconstruct on cache hit. Adds complexity.

**Path B — A separate small in-memory LRU dict** that holds `list[VectorMemoryEntry]` by reference. Simpler. Doesn't reuse `LLMCache` directly but borrows the pattern.

**Recommendation: Path B.** The existing `LLMCache` is over-engineered for this; we only need an in-memory map with TTL and bounded size. ~30 lines:

```python
class HybridQueryCache:
    def __init__(self, max_entries: int = 128, ttl_seconds: float = 60.0):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, list[VectorEntry]]] = OrderedDict()

    def get(self, key: str) -> list[VectorEntry] | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.monotonic() - ts >= self._ttl:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return list(value)  # defensive copy

    def put(self, key: str, value: list[VectorEntry]) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), list(value))
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
```

Wire into `search_hybrid` at the top of step 1:

```python
cache_key = self._query_cache_key(query, mode, top_k, weights, namespace, memory_types)
cached = self._query_cache.get(cache_key)
if cached is not None:
    self._emit_metric("hybrid_query_cache_hit", 1, mode=mode)
    # Touch on cache hit too — usage signal still fires.
    self._touch_and_count_async(cached)
    return cached
```

And after step 5:

```python
self._query_cache.put(cache_key, results)
```

**Cache invalidation:** any `set()` or `delete()` on the underlying store invalidates the cache. The simplest safe rule: clear the cache on any write. Add to `set()` and `delete()` overrides:

```python
def set(self, key, text, ...):
    super().set(key, text, ...)
    self._query_cache.clear()  # invalidate everything; safest
    # ... existing Phase 2 fan-out ...
```

Slightly heavy-handed but correct. A 60-second TTL plus write-clear is more than safe enough.

**Disable knob:** `OBSCURA_LIGHTRAG_QUERY_CACHE=0` to disable entirely for debugging.

---

## 9. Result-shape contract

The exact fields populated on returned `VectorMemoryEntry` from `search_hybrid`. This contract is consumed by Phase 4 tools and any UI rendering.

| Field | Source | Always populated? | Notes |
|---|---|---|---|
| `key: MemoryKey` | Backend hydration (`backend.get_vector(...)`) | Yes | `{namespace, key}` |
| `namespace: str` | Same as `key.namespace` | Yes | Implicit via `key` |
| `text: str` | Backend hydration | Yes | Canonical from Obscura, not LightRAG |
| `metadata: dict[str, Any]` | Backend hydration | Yes | Includes `access_count` if present |
| `embedding: list[float]` | **Empty** for Qdrant backend (`embedding=[]` quirk at `qdrant_backend.py:337`) | Yes (possibly empty) | For SQLite backend likely populated; engineer should test |
| `memory_type: str` | Backend hydration | Yes | E.g. "fact", "summary", "general" |
| `created_at: datetime` | Backend hydration | Yes | UTC timezone |
| `accessed_at: datetime \| None` | Backend hydration | No (None for never-touched) | The Phase 3 path will write this in `_touch_and_count_async` |
| `score: float` | LightRAG `vector_sim`, clamped to `[0, 1]` | Yes | Pure vector similarity |
| `rerank_score: float` | LightRAG `graph_score`, min-max-normalized to `[0, 1]` | Yes | Graph relevance contribution; 0 on fallback path |
| `final_score: float` | `hybrid_score(...)` output | Yes | The composite; sort key |

### 9.1 What's NOT in the contract

- **No raw LightRAG payload.** We deliberately don't expose LightRAG-specific fields like `community_id` or `entity_chunks`. If a future feature wants those, it goes through a separate API (Phase 4's `memory_graph_explain`).
- **No per-component breakdown on the entry itself.** `final_score` is the composite; component contributions are emitted as telemetry. If an engineer wants per-result component values for debugging, they enable verbose logging — we don't bloat the entry.
- **No "from-graph-or-fallback" flag on the entry.** The fallback path's reformat (§6.3) makes scores comparable; the entry shape is identical regardless of path. The query-level `fallback` flag is in telemetry.

### 9.2 Implications for Phase 4

When Phase 4 wires `semantic_search_impl` to call `search_hybrid` under the hood:

- The response payload should add a `graph_relevance` field surfaced from `entry.rerank_score`.
- The model prompt rendering should use `final_score` for ordering and either `score` or `rerank_score` if a "why this one" hint is desired.

Phase 3 doesn't touch the tool serialization; it just makes sure the contract is right.

---

## 10. Failure modes & error handling

### 10.1 Embedding-function mismatch

**Failure:** LightRAG was initialized with a different embedding function than Obscura's (e.g., a previous run used OpenAI ada-002, current run uses MiniLM-L6-v2). The vector dimensions are different, or the same dimension but different model — the latter is more insidious because no error is raised; similarity scores are just meaningless.

**Mitigation:**

1. **Phase 1 scaffolding** must enforce shared embedder at adapter init: `LightRAGAdapter.for_user(user, embedding_fn)` stores the dimension and embedding-fn identity, and asserts at `aquery` time that the configured embedder matches.
2. **Phase 3 defensive guard:** in `search_hybrid`, after the first call, persist the dimension into the adapter state. On subsequent calls, assert dimension matches. Log a `CRITICAL` and disable LightRAG for the rest of the session if mismatch detected.
3. **Phase 5 backfill:** on backfill, store the embedding-model identity in payload metadata. On retrieval, if the backend's stored model identity doesn't match the current adapter's, drop the result with a logged warning.

For Phase 3 scope: just the assertion. The actual embedder identity check goes in Phase 1.

### 10.2 Stale graph references (drift)

Already covered in §4.5. Drop hits whose hydration returns `None`, log a counter. If the counter consistently exceeds 25% of hits, escalate to backfill.

### 10.3 Concurrent queries through the adapter

The Phase 1 adapter maintains a dedicated event loop in a daemon thread. `asyncio.run_coroutine_threadsafe` is the canonical pattern for safe cross-thread coroutine submission. Nothing for Phase 3 to do beyond using that helper.

If LightRAG itself is not thread-safe internally, the adapter's serialized event loop is the boundary — only one query runs at a time inside LightRAG. This may be a performance bottleneck under high concurrency; if it ever becomes one, the adapter grows a worker pool. Out of scope for Phase 3.

### 10.4 NetworkX pickle corruption

LightRAG uses NetworkX for the graph backend (per `00-overview.md:70`). It serializes to a pickle file. If the pickle is corrupted (process killed mid-write, disk error, etc.), `aquery` will raise on first load.

**Mitigation:**

1. **Adapter-level catch.** The adapter catches deserialization errors at startup and disables graph functionality for the session, logs a `CRITICAL`, and surfaces a flag.
2. **Phase 3 query-side check.** If the adapter exposes a `graph_disabled` flag, `search_hybrid` short-circuits straight to the fallback path without attempting `aquery`.
3. **Recovery suggestion.** Logged error includes "run `obscura memory backfill-graph --rebuild` to recreate" — Phase 5 deliverable.

For Phase 3: the fallback path covers it; we just need to detect early and not spam `aquery` errors.

### 10.5 Adapter event loop exits

If the adapter's event loop dies (uncaught exception in a task, daemon thread shutdown), `run_coroutine_threadsafe` will hang on `future.result()`. The timeout (§4.3) is the safety net — at worst, every query will time out at 400ms and fall through.

The adapter should detect dead-loop and lazily recreate it. Phase 1 concern; for Phase 3, just trust the timeout.

### 10.6 Empty-namespace query

`namespace=None` is a valid input — search across all namespaces. The hydration loop's `if namespace is not None and hit.namespace != namespace: continue` no-ops. No special handling needed.

### 10.7 `top_k=0` or `first_stage_k=0`

Defensive: short-circuit at the top of `search_hybrid`:

```python
if top_k <= 0:
    return []
if first_stage_k <= 0:
    first_stage_k = max(top_k * 5, 20)  # sane default
```

### 10.8 Empty query string

LightRAG may behave oddly on `aquery("")` — possibly returning all chunks or nothing. We don't validate user input here; the agent layer should. If it does happen, the fallback path handles "0 hits" correctly.

---

## 11. Telemetry

Every `search_hybrid` call emits structured data. Two destinations:

1. **Structured log line** (single `logger.info("hybrid_query %s", kw)` call) for ad-hoc grep/jq analysis.
2. **Counters and histograms** to whatever metric sink Obscura has (`obscura.core.deep_log` or similar — engineer wires the actual call).

### 11.1 Per-query telemetry payload

```python
{
    "query_hash": "sha256_of_query_string"[:16],
    "user_id_hash": "sha256_of_user_id"[:16],     # privacy-preserving
    "mode": "hybrid",                              # or local/global/naive/mix
    "top_k": 5,
    "first_stage_k": 50,

    # LightRAG outcome
    "n_lr_hits": 38,
    "n_hydrated": 36,                              # after drift drops
    "n_returned": 5,
    "drift_count": 2,
    "drift_pct": 0.053,                            # 2 / 38

    # Latency breakdown (ms)
    "t_total_ms": 287,
    "t_lr_ms": 245,                                # LightRAG aquery
    "t_hydrate_ms": 18,                            # backend.get_vector loop
    "t_score_ms": 8,                               # hybrid_score loop
    "t_overhead_ms": 16,                           # everything else

    # Per-component avg contributions across returned results
    "avg_w_vec_contrib": 0.31,
    "avg_w_graph_contrib": 0.12,
    "avg_w_decay_contrib": 0.09,
    "avg_w_usage_contrib": 0.02,

    # Path
    "fallback": False,
    "fallback_reason": null,                       # "timeout" / "empty" / "exception" / "all_drift"
    "cache_hit": False,
}
```

### 11.2 Counters

| Counter | Tags | Semantics |
|---|---|---|
| `hybrid_query_count` | `mode` | Every call, including fallbacks |
| `hybrid_query_latency_ms` | `mode` | Histogram or gauge |
| `hybrid_query_empty` | `mode` | LightRAG returned 0 hits |
| `hybrid_query_error` | `mode` | LightRAG raised |
| `hybrid_query_timeout` | `mode` | Timed out |
| `hybrid_query_all_drift` | `mode` | Hydration dropped everything |
| `hybrid_drift_drops` | `mode` | Per-hit count of drift drops |
| `hybrid_fallback` | `reason` | Any fallback fired |
| `hybrid_query_cache_hit` | `mode` | Result-cache hit |

### 11.3 Why log `query_hash` instead of `query`

Privacy. Personal-memory queries can contain sensitive content. Hashing gives us cluster-detection (frequent queries) without storing the content. The first 16 hex chars are enough for cluster identification while being non-reversible.

If a debug session needs to inspect the actual query, run with `OBSCURA_LIGHTRAG_DEBUG_QUERY=1` to log the raw query string (clearly opt-in, never default).

### 11.4 Why per-component contributions

The component-contribution averages let engineers tune weights without rolling A/B experiments. If a deployment shows `avg_w_graph_contrib` consistently dwarfing `avg_w_vec_contrib`, the graph term is over-weighted. The numbers come "for free" from the hybrid scorer; we just sum them.

---

## 12. Tests for this phase

Test layout (mirrors existing `tests/unit/obscura/vector_memory/` structure):

```
tests/unit/obscura/lightrag_memory/
├── conftest.py                        # MockLightRAG fixture, sample VectorEntry helpers
├── test_hybrid_score.py               # pure scoring math
├── test_hybrid_store_search.py        # search_hybrid integration
├── test_hybrid_store_touch.py         # _touch_and_count_async
├── test_recall_integration.py         # SemanticMemoryMixin.recall() routing
└── test_update_metadata.py            # backend protocol method
```

### 12.1 `test_hybrid_score.py` — scoring math

**8+ cases:**

```python
import pytest
from obscura.lightrag_memory.scoring import HybridWeights, hybrid_score

DEFAULT = HybridWeights()

def test_all_zero():
    assert hybrid_score(
        vector_sim=0, graph_relevance=0, decay_multiplier=0,
        usage_count=0, weights=DEFAULT,
    ) == 0.0

def test_all_one():
    # vec=1, graph=1, decay=1, usage_norm=log1p(100)/log1p(100)=1
    score = hybrid_score(
        vector_sim=1, graph_relevance=1, decay_multiplier=1,
        usage_count=100, weights=DEFAULT,
    )
    assert score == pytest.approx(0.5 + 0.3 + 0.15 + 0.05, abs=1e-6)

def test_negative_vector_clamped():
    # Negative vector_sim should be treated as 0.
    score = hybrid_score(
        vector_sim=-0.3, graph_relevance=0, decay_multiplier=0,
        usage_count=0, weights=DEFAULT,
    )
    assert score == 0.0

def test_vector_above_one_clamped():
    score = hybrid_score(
        vector_sim=1.5, graph_relevance=0, decay_multiplier=0,
        usage_count=0, weights=DEFAULT,
    )
    assert score == pytest.approx(0.5, abs=1e-6)  # 1.0 * 0.5

def test_usage_log_saturation():
    # usage=100 saturates; usage=200 same as 100 (clipped).
    s100 = hybrid_score(
        vector_sim=0, graph_relevance=0, decay_multiplier=0,
        usage_count=100, weights=DEFAULT,
    )
    s200 = hybrid_score(
        vector_sim=0, graph_relevance=0, decay_multiplier=0,
        usage_count=200, weights=DEFAULT,
    )
    assert s100 == s200 == pytest.approx(0.05, abs=1e-6)

def test_weights_only_vector():
    weights = HybridWeights(vector=1.0, graph=0.0, decay=0.0, usage=0.0)
    score = hybrid_score(
        vector_sim=0.7, graph_relevance=1.0, decay_multiplier=1.0,
        usage_count=100, weights=weights,
    )
    assert score == pytest.approx(0.7, abs=1e-6)

def test_negative_weight_rejected():
    with pytest.raises(ValueError):
        HybridWeights(vector=-0.1)

def test_weights_not_summing_to_one_warns(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="obscura.lightrag_memory.scoring"):
        HybridWeights(vector=0.5, graph=0.5, decay=0.5, usage=0.5)
    assert any("do not sum to 1.0" in r.message for r in caplog.records)

def test_none_inputs_treated_as_zero():
    score = hybrid_score(
        vector_sim=None, graph_relevance=None, decay_multiplier=None,
        usage_count=None, weights=DEFAULT,
    )
    assert score == 0.0

def test_fingerprint_stability():
    a = HybridWeights(0.5, 0.3, 0.15, 0.05)
    b = HybridWeights(0.5, 0.3, 0.15, 0.05)
    assert a.fingerprint() == b.fingerprint()
    c = HybridWeights(0.6, 0.3, 0.15, 0.05)
    assert a.fingerprint() != c.fingerprint()
```

### 12.2 `test_hybrid_store_search.py` — integration

**`MockLightRAGAdapter` fixture** (in `conftest.py`):

```python
import asyncio
from dataclasses import dataclass
from typing import Any

@dataclass
class MockGraphHit:
    namespace: str
    key: str
    vector_sim: float
    graph_score: float

class MockLightRAGAdapter:
    """Test double for LightRAGAdapter — never imports lightrag."""
    def __init__(self):
        self._canned: list[MockGraphHit] = []
        self._raise: Exception | None = None
        self._delay_ms: int = 0
        self.aquery_calls: list[dict] = []
        self.loop = asyncio.new_event_loop()
        # Dedicated thread to run the loop, mirroring the real adapter.
        import threading
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def set_canned_response(self, hits: list[MockGraphHit]) -> None:
        self._canned = hits

    def set_raise(self, exc: Exception) -> None:
        self._raise = exc

    def set_delay_ms(self, ms: int) -> None:
        self._delay_ms = ms

    async def aquery(self, **kw: Any) -> list[MockGraphHit]:
        self.aquery_calls.append(kw)
        if self._delay_ms:
            await asyncio.sleep(self._delay_ms / 1000.0)
        if self._raise:
            raise self._raise
        return list(self._canned)

@pytest.fixture
def mock_adapter():
    return MockLightRAGAdapter()

@pytest.fixture
def hybrid_store(tmp_path, mock_adapter):
    """A HybridVectorMemoryStore wired to a SQLite backend in tmp_path
    and the mock adapter."""
    from obscura.auth.models import AuthenticatedUser
    from obscura.vector_memory.backends import BackendConfig, SQLiteBackend
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore

    user = AuthenticatedUser(user_id="test_user", ...)  # adapt to actual ctor
    config = BackendConfig(user_id="test_user", embedding_dim=384)
    backend = SQLiteBackend(config=config, db_path=tmp_path / "vm.db")
    store = HybridVectorMemoryStore.__new__(HybridVectorMemoryStore)
    # Bypass full init; set what we need directly.
    store.user = user
    store.user_id = "test_user"
    store.backend = backend
    store.embedding_fn = lambda t: [0.1] * 384
    store.embedding_dim = 384
    store.decay_config = ...  # default
    store._lr = mock_adapter
    store._cached_weights = None
    return store
```

**Tests:**

```python
def test_search_hybrid_orders_by_final_score(hybrid_store, mock_adapter):
    # Seed two chunks with known content.
    hybrid_store.set("k1", "fact one", namespace="ns")
    hybrid_store.set("k2", "fact two", namespace="ns")
    # Mock returns both; manipulate scores so k2 should rank above k1.
    mock_adapter.set_canned_response([
        MockGraphHit("ns", "k1", vector_sim=0.6, graph_score=0.2),
        MockGraphHit("ns", "k2", vector_sim=0.5, graph_score=1.0),
    ])
    # graph_norm: k1 -> 0.0, k2 -> 1.0 after min-max.
    # final_score (defaults):
    #   k1: 0.5*0.6 + 0.3*0.0 + 0.15*decay + 0.05*0 = 0.3 + decay_term
    #   k2: 0.5*0.5 + 0.3*1.0 + 0.15*decay + 0.05*0 = 0.55 + decay_term
    # k2 wins.
    results = hybrid_store.search_hybrid("query", namespace="ns")
    assert results[0].key.key == "k2"
    assert results[1].key.key == "k1"

def test_search_hybrid_empty_falls_back(hybrid_store, mock_adapter):
    hybrid_store.set("k1", "fact", namespace="ns")
    mock_adapter.set_canned_response([])  # empty
    results = hybrid_store.search_hybrid("query", namespace="ns")
    # Should fall through to search_reranked → returns the seeded chunk.
    assert len(results) == 1
    assert results[0].key.key == "k1"

def test_search_hybrid_exception_falls_back(hybrid_store, mock_adapter):
    hybrid_store.set("k1", "fact", namespace="ns")
    mock_adapter.set_raise(RuntimeError("graph corrupt"))
    results = hybrid_store.search_hybrid("query", namespace="ns")
    assert len(results) == 1

def test_search_hybrid_timeout_falls_back(hybrid_store, mock_adapter):
    hybrid_store.set("k1", "fact", namespace="ns")
    mock_adapter.set_delay_ms(500)
    results = hybrid_store.search_hybrid(
        "query", namespace="ns", timeout_ms=50,
    )
    assert len(results) == 1

def test_search_hybrid_drift_drops(hybrid_store, mock_adapter):
    # Seed chunk k1, but adapter returns hits for k1 AND k_missing.
    hybrid_store.set("k1", "fact", namespace="ns")
    mock_adapter.set_canned_response([
        MockGraphHit("ns", "k1", 0.5, 0.5),
        MockGraphHit("ns", "k_missing", 0.9, 0.9),  # not in backend
    ])
    results = hybrid_store.search_hybrid("query", namespace="ns")
    keys = {r.key.key for r in results}
    assert "k_missing" not in keys
    assert "k1" in keys

def test_search_hybrid_namespace_filter(hybrid_store, mock_adapter):
    hybrid_store.set("k1", "fact A", namespace="nsA")
    hybrid_store.set("k2", "fact B", namespace="nsB")
    mock_adapter.set_canned_response([
        MockGraphHit("nsA", "k1", 0.5, 0.5),
        MockGraphHit("nsB", "k2", 0.5, 0.5),
    ])
    results = hybrid_store.search_hybrid("query", namespace="nsA")
    keys = {r.key.key for r in results}
    assert keys == {"k1"}

def test_search_hybrid_memory_types_filter(hybrid_store, mock_adapter):
    hybrid_store.set("k1", "fact", namespace="ns", memory_type="fact")
    hybrid_store.set("k2", "summary", namespace="ns", memory_type="summary")
    mock_adapter.set_canned_response([
        MockGraphHit("ns", "k1", 0.5, 0.5),
        MockGraphHit("ns", "k2", 0.5, 0.5),
    ])
    results = hybrid_store.search_hybrid(
        "query", namespace="ns", memory_types=["fact"],
    )
    keys = {r.key.key for r in results}
    assert keys == {"k1"}

def test_search_hybrid_populates_score_fields(hybrid_store, mock_adapter):
    hybrid_store.set("k1", "fact", namespace="ns")
    mock_adapter.set_canned_response([
        MockGraphHit("ns", "k1", 0.7, 0.4),
    ])
    results = hybrid_store.search_hybrid("query", namespace="ns")
    e = results[0]
    assert e.score == 0.7  # vector_sim
    # rerank_score is graph_norm; with single hit, normalize_g returns 0.5.
    assert e.rerank_score == 0.5
    assert e.final_score > 0
```

### 12.3 `test_hybrid_store_touch.py` — usage tracking

```python
def test_touch_and_count_increments(hybrid_store, mock_adapter):
    hybrid_store.set("k1", "fact", namespace="ns")
    mock_adapter.set_canned_response([MockGraphHit("ns", "k1", 0.5, 0.5)])

    # First call: access_count goes 0 -> 1.
    hybrid_store.search_hybrid("q", namespace="ns")
    # Wait briefly for async write to land.
    import time; time.sleep(0.1)
    e = hybrid_store.backend.get_vector(MemoryKey("ns", "k1"))
    assert e.metadata.get("access_count") == 1

    # Second call: 1 -> 2.
    hybrid_store.search_hybrid("q", namespace="ns")
    time.sleep(0.1)
    e = hybrid_store.backend.get_vector(MemoryKey("ns", "k1"))
    assert e.metadata.get("access_count") == 2

def test_touch_and_count_race_tolerant(hybrid_store, mock_adapter):
    """Two concurrent queries on the same key — final count is in {1, 2}.
    Lost update under race is acceptable per §5.6."""
    hybrid_store.set("k1", "fact", namespace="ns")
    mock_adapter.set_canned_response([MockGraphHit("ns", "k1", 0.5, 0.5)])

    import threading
    threads = [
        threading.Thread(target=hybrid_store.search_hybrid,
                         args=("q",), kwargs={"namespace": "ns"})
        for _ in range(2)
    ]
    for t in threads: t.start()
    for t in threads: t.join()
    import time; time.sleep(0.2)
    e = hybrid_store.backend.get_vector(MemoryKey("ns", "k1"))
    assert e.metadata.get("access_count") in (1, 2)

def test_touch_legacy_entry_starts_at_zero(hybrid_store, mock_adapter):
    """Entry stored before access_count was tracked starts at 0, goes to 1."""
    # Manually insert without access_count metadata.
    from obscura.memory import MemoryKey
    hybrid_store.backend.store_vector(
        key=MemoryKey("ns", "legacy"),
        text="legacy fact",
        embedding=[0.1] * 384,
        metadata={},  # no access_count
        memory_type="fact",
        expires_at=None,
    )
    mock_adapter.set_canned_response([
        MockGraphHit("ns", "legacy", 0.5, 0.5),
    ])
    hybrid_store.search_hybrid("q", namespace="ns")
    import time; time.sleep(0.1)
    e = hybrid_store.backend.get_vector(MemoryKey("ns", "legacy"))
    assert e.metadata.get("access_count") == 1
```

### 12.4 `test_recall_integration.py` — `recall()` routing

```python
def test_recall_use_graph_on_plain_store_falls_through(tmp_path):
    """recall(use_graph=True) on a plain VectorMemoryStore does not raise
    and returns the same as use_graph=False."""
    from obscura.vector_memory.vector_memory import VectorMemoryStore
    # ... build a plain store ...
    store = ...
    agent = ...  # something with SemanticMemoryMixin and store
    results_graph = agent.recall("q", use_graph=True)
    results_no_graph = agent.recall("q", use_graph=False)
    assert results_graph == results_no_graph

def test_recall_use_graph_on_hybrid_store_calls_search_hybrid(
    hybrid_store, mock_adapter,
):
    """recall(use_graph=True) on a HybridVectorMemoryStore routes correctly."""
    # ... build agent with hybrid_store as vector_memory ...
    mock_adapter.set_canned_response([MockGraphHit("ns:semantic", "k1", 0.5, 0.5)])
    hybrid_store.set("k1", "fact", namespace="agent_id:semantic")
    # Patch the agent's namespace generation if needed.
    results = agent.recall("q", use_graph=True)
    # Mock should have been called.
    assert mock_adapter.aquery_calls
```

### 12.5 `test_update_metadata.py` — backend protocol method

```python
def test_qdrant_update_metadata(qdrant_backend):
    from obscura.memory import MemoryKey
    qdrant_backend.store_vector(
        key=MemoryKey("ns", "k1"),
        text="t",
        embedding=[0.1] * 384,
        metadata={"existing": "val"},
        memory_type="fact",
        expires_at=None,
    )
    ok = qdrant_backend.update_metadata(
        MemoryKey("ns", "k1"),
        {"access_count": 5, "accessed_at": "2026-04-26T00:00:00+00:00"},
    )
    assert ok is True
    e = qdrant_backend.get_vector(MemoryKey("ns", "k1"))
    assert e.metadata.get("existing") == "val"  # untouched
    # access_count and accessed_at land in payload root in Qdrant.
    # Verify via direct retrieval if needed.

def test_update_metadata_missing_key_returns_false(qdrant_backend):
    from obscura.memory import MemoryKey
    ok = qdrant_backend.update_metadata(
        MemoryKey("ns", "absent"), {"access_count": 1},
    )
    assert ok is False

def test_sqlite_update_metadata(sqlite_backend):
    # Mirror of qdrant test above.
    ...
```

### 12.6 What we don't test in this phase

- **LightRAG itself.** Mocked entirely. Real LightRAG retrieval quality is out of scope; integration tests against real adapter come in Phase 6 with the `RUN_LR_INTEGRATION=1` opt-in.
- **`compute_decay`'s correctness.** Tested separately in `tests/unit/obscura/vector_memory/test_decay.py` (assumed exists). We trust it here.
- **The cache invalidation semantics.** Phase 3 ships the cache; Phase 6 covers cache cases more deeply.

---

## 13. Open questions / decisions deferred

### 13.1 Per-call `weights` override on the model-facing tool

**Question:** should `memory_graph_query` (Phase 4 tool) accept `weights={vector: 0.7, ...}` as a parameter?

**Position:** No, by default. Reasoning:

- The model has no good signal for tuning weights — it would be guessing.
- Adding a high-dimensional knob to the tool surface invites bad usage and complicates the prompt.
- Operators tune weights via config; users don't tune per call.

If a use case does emerge (e.g., "for this codebase question, weight vector higher"), expose a small enum: `mode_preset: "balanced" | "lexical" | "graph_heavy"` rather than raw weights. **Defer to Phase 4.**

### 13.2 Multi-namespace queries

**Question:** can `search_hybrid` accept `namespace=["ns_a", "ns_b"]` (list, not str)?

**Position:** Phase 3 keeps single-namespace. Reasoning:

- `search_reranked` is single-namespace; matching its shape simplifies the API.
- Multi-namespace introduces ambiguity in min-max normalization (do we normalize within each namespace or across?).
- No current call site needs multi-namespace.

If future need emerges, the cleanest extension is a list parameter that's translated to multiple parallel `aquery` calls then merged — call site complexity is in the caller, not the store. **Out of scope for Phase 3.**

### 13.3 Per-user weight personalization

**Question:** can we learn weights from feedback over time?

**Position:** Future work. Requires:

1. Feedback signal collection (which results did the user/agent actually use?).
2. Storage layer for per-user weight history.
3. Periodic fitting (offline batch job or online gradient).

The serialized component values (already logged via telemetry §11.4) are sufficient as features for an offline fitter. None of this is on the Phase 3 critical path. **Out of scope.**

### 13.4 What happens during `MemoryConsolidator` runs?

The consolidator (`obscura/vector_memory/consolidator.py`) periodically deletes old episodes and creates summaries. After consolidation, the original chunks are gone — drift in the graph until the next backfill or `delete()` propagation.

The Phase 2 `delete()` override propagates to LightRAG; if the consolidator calls `store.delete(key)` properly (it should — see `consolidator.consolidate` around line 130), drift is bounded. If it bypasses the store and calls `backend.delete_vector` directly, drift accumulates.

**Phase 3 action:** add a comment to `consolidator.py` (or just a check) that consolidator should always go through `store.delete()` not `backend.delete_vector()`. The delete-propagation is then automatic. **Verify during implementation; if the consolidator uses backend-direct, log a warning and either fix or document as Phase 5 cleanup.**

### 13.5 Cache invalidation granularity

**Question:** is "clear all" on every write the right invalidation strategy?

**Position:** Yes for v1. Per-key invalidation requires tracking which queries returned which keys — adds bookkeeping for marginal benefit (60s TTL is short anyway). Reconsider if the cache hit rate is high enough to matter. **Document as cache-tuning future work.**

### 13.6 What if a downstream tool wants pre-rerank results?

**Question:** could a future tool want the raw `lr_hits` before scoring/sorting?

**Position:** add a `_search_hybrid_raw` private method that returns hydrated `(entry, vec_sim, graph_norm, decay_mult, usage_count)` tuples without final sorting/touch. `search_hybrid` becomes a thin wrapper. **Don't ship the private method in Phase 3** — only build it if Phase 4's tools want it. Mentioned for future-proofing.

---

## 14. Summary — implementation checklist

For an engineer executing Phase 3 cold, the punch list:

1. [ ] Create `obscura/lightrag_memory/scoring.py` with `HybridWeights`, `hybrid_score`, `load_hybrid_weights_from_disk`. Cover with unit tests in `tests/unit/obscura/lightrag_memory/test_hybrid_score.py` (10+ cases).
2. [ ] Add `update_metadata` to the `VectorBackend` Protocol in `obscura/vector_memory/backends/base.py`.
3. [ ] Implement `update_metadata` on `QdrantBackend` (`obscura/vector_memory/backends/qdrant_backend.py`) and `SQLiteBackend` (`obscura/vector_memory/backends/sqlite_backend.py`). Test in `tests/unit/obscura/lightrag_memory/test_update_metadata.py`.
4. [ ] Extend `obscura/lightrag_memory/hybrid_store.py` (created in Phase 2) with:
   - [ ] `_resolve_weights`
   - [ ] `_run_aquery_blocking`
   - [ ] `_lr_default_timeout_ms`
   - [ ] `search_hybrid`
   - [ ] `_fallback_to_reranked`
   - [ ] `_touch_and_count_async`
   - [ ] `_emit_metric` and `_emit_query_telemetry`
   - [ ] (optional) result cache integration
5. [ ] Modify `SemanticMemoryMixin.recall()` in `obscura/vector_memory/vector_memory.py:665` to add `use_graph: bool = True` and route to `search_hybrid` when applicable.
6. [ ] Verify `obscura/vector_memory/consolidator.py` uses `store.delete()` not `backend.delete_vector()`.
7. [ ] Tests:
   - [ ] `test_hybrid_score.py`
   - [ ] `test_hybrid_store_search.py` (with `MockLightRAGAdapter` fixture in `conftest.py`)
   - [ ] `test_hybrid_store_touch.py`
   - [ ] `test_recall_integration.py`
   - [ ] `test_update_metadata.py`
8. [ ] Add `[vector_memory.lightrag.weights]` and `[vector_memory.lightrag] query_timeout_ms` examples to `docs/config.md` or wherever Obscura config docs live.
9. [ ] Run `make lint` and `make typecheck`; fix issues.
10. [ ] Run `pytest tests/unit/obscura/lightrag_memory/` — all green.
11. [ ] Run full unit suite — confirm no regressions in existing `vector_memory` tests.

When this list is complete, Phase 3 is done. Phase 4 (model-facing tools) builds directly on top.

---

## 15. Critical files for implementation

- **Read for context (don't modify):**
  - `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/vector_memory.py:387` — `search_similar`
  - `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/vector_memory.py:450` — `search_reranked` (the structural template)
  - `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/vector_memory.py:557` — `_touch_results_async` (existing pattern)
  - `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/decay.py:86` — `compute_decay`
  - `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/vector_memory_rerank.py:122` — `RecencyReranker` (analogous shape)
  - `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/backends/qdrant_backend.py:329-345` — server-side decay pattern
  - `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/backends/qdrant_backend.py:403` — `touch_vector` (analog for `update_metadata`)
  - `/Users/elliottbregni/dev/obscura-main/obscura/core/llm_cache.py` — pattern reference for the result cache
- **Modify in this phase:**
  - `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/vector_memory.py:665` — `SemanticMemoryMixin.recall()`
  - `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/backends/base.py` — Protocol addition
  - `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/backends/qdrant_backend.py` — `update_metadata` method
  - `/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/backends/sqlite_backend.py` — `update_metadata` method
- **Create in this phase:**
  - `/Users/elliottbregni/dev/obscura-main/obscura/lightrag_memory/scoring.py`
  - `/Users/elliottbregni/dev/obscura-main/obscura/lightrag_memory/hybrid_store.py` (extend Phase 2's stub)
  - `/Users/elliottbregni/dev/obscura-main/obscura/lightrag_memory/query_cache.py` (optional)
  - `/Users/elliottbregni/dev/obscura-main/tests/unit/obscura/lightrag_memory/conftest.py`
  - `/Users/elliottbregni/dev/obscura-main/tests/unit/obscura/lightrag_memory/test_hybrid_score.py`
  - `/Users/elliottbregni/dev/obscura-main/tests/unit/obscura/lightrag_memory/test_hybrid_store_search.py`
  - `/Users/elliottbregni/dev/obscura-main/tests/unit/obscura/lightrag_memory/test_hybrid_store_touch.py`
  - `/Users/elliottbregni/dev/obscura-main/tests/unit/obscura/lightrag_memory/test_recall_integration.py`
  - `/Users/elliottbregni/dev/obscura-main/tests/unit/obscura/lightrag_memory/test_update_metadata.py`
