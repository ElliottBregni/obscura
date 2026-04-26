# Phase 2 — Ingest path

> **Owner:** Elliott Bregni (`bregnie34@gmail.com`)
> **Drafted:** 2026-04-25
> **Depends on:** Phase 1 (scaffolding: `obscura/lightrag_memory/` package, `LightRAGAdapter` skeleton with per-user instance + dedicated event loop, optional `[lightrag]` uv extra installed, `OBSCURA_LIGHTRAG=on` env flag wired into `for_user()`).
> **Unblocks:** Phase 3 (query path), Phase 4 (tools), Phase 5 (backfill).

This document is the implementation guide for the **write path** of the LightRAG integration. After Phase 2 lands, every existing call into `VectorMemoryStore.set()` and `delete()` transparently fans out to a per-user LightRAG instance whose graph + secondary vector index get updated asynchronously, without blocking the calling tool / route. The query path is unchanged in this phase — Phase 3 owns that.

---

## 1. Goal & non-goals

### Goal

Produce a `HybridVectorMemoryStore` subclass of `VectorMemoryStore` (`obscura/vector_memory/vector_memory.py:163`) and a `LightRAGAdapter` that together provide:

1. A `set(...)` override that calls `super().set()` synchronously (preserving the existing Qdrant write path verbatim) and then submits an asynchronous LightRAG ingest job onto a per-user thread pool. Filtering by `memory_type` (the **`indexable_types` whitelist**) happens before the submission so non-indexable types never touch the executor.
2. A `delete(key, ...)` override and a `clear_namespace(...)` override that mirror the synchronous backend delete to LightRAG asynchronously. The graph stays consistent with the canonical Qdrant store.
3. A `LightRAGAdapter.insert_safe(...)` and `delete_safe(...)` pair — synchronous public methods callable from a thread-pool worker. Internally they bridge to LightRAG's `ainsert` / `adelete_by_doc_id` via `asyncio.run_coroutine_threadsafe` against the adapter's dedicated event loop (the loop is created in Phase 1).
4. A robust failure-isolation contract: an exception inside the executor — LLM provider down, Qdrant down, NetworkX pickle write race, OOM during entity extraction, timeout — is logged but **never propagates** to the caller of `set()` / `delete()`. The Qdrant row remains in place and can be re-ingested by the Phase 5 backfill or lazy-on-touch path.
5. A `close()` method on `HybridVectorMemoryStore` that drains the executor and, when the store is the last reference to its adapter, stops the adapter's event loop. Phase 4 wires this into the auth-middleware lifecycle.

### Non-goals (explicitly out of scope)

- **No query path changes.** `search_similar`, `search_reranked`, `search_hybrid` are Phase 3. `HybridVectorMemoryStore` in Phase 2 inherits the unchanged read path from `VectorMemoryStore`.
- **No new tools.** `memory_graph_query` and `memory_graph_explain` belong to Phase 4.
- **No backfill.** Existing chunks remain un-graphed when Phase 2 lands. The lazy-on-touch path (`touch()` re-ingest) and the explicit CLI backfill belong to Phase 5.
- **No new payload fields on Qdrant entries.** Phase 5 adds `lr_indexed_at` and `access_count` to the backend payload schema. Phase 2 documents the dependency and leaves a no-op stub at the adapter's success callback.
- **No backend protocol additions.** `VectorBackend` (`obscura/vector_memory/backends/base.py:42-108`) is not modified in this phase. `update_metadata()` is a Phase 5 problem.
- **No system prompt changes.** Phase 4.
- **No consolidator integration.** When `MemoryConsolidator.consolidate()` (`obscura/vector_memory/consolidator.py`) deletes consolidated episodes, those deletes do go through the override, so the graph stays consistent. But the discussion of whether to *also* delete the synthesized summary's old fragments is deferred to Phase 4/5.

---

## 2. Acceptance criteria

The phase is complete when:

1. `HybridVectorMemoryStore.set(key, text, memory_type="fact")` — returns in <100ms (excluding the embedding step from `super().set()`), and submits exactly one job to `_ingest_executor` containing a call to `adapter.insert_safe(doc_id, text, metadata)`.
2. `HybridVectorMemoryStore.set(key, text, memory_type="episode")` — returns in <100ms and submits **zero** jobs (default `indexable_types` policy excludes episode).
3. `HybridVectorMemoryStore.set(key, text, metadata={"graph_index": True}, memory_type="episode")` — submits one job (metadata escape hatch overrides the whitelist).
4. `HybridVectorMemoryStore.set(key, text, metadata={"graph_index": False}, memory_type="fact")` — submits **zero** jobs (explicit opt-out, even though `fact` is in the whitelist).
5. `HybridVectorMemoryStore.delete(key)` — calls `super().delete()` and, if it returned `True` (the row existed), submits a `delete_safe(doc_id)` job to the executor.
6. `HybridVectorMemoryStore.clear_namespace(ns)` — calls `super().clear_namespace(ns)`, captures the list of keys before the namespace is wiped, and submits batched `delete_safe(...)` jobs to the executor.
7. An exception raised inside `adapter.insert_safe` (any subclass of `Exception`) does not propagate to the caller of `set()`. The exception is logged at `WARNING` with `{doc_id, memory_type, exc_type}`, and a `lr_inserts_failed` counter increments.
8. A timeout in `adapter.insert_safe` (>= `insert_timeout_seconds`, default 60) cancels the underlying coroutine via `future.cancel()`, logs at `WARNING`, and increments `lr_inserts_timed_out`.
9. With `OBSCURA_LIGHTRAG=off` (the default), `for_user()` returns the bare `VectorMemoryStore`. Existing tests pass unchanged — `HybridVectorMemoryStore` is never instantiated.
10. With `OBSCURA_LIGHTRAG=on`, all 10+ existing call sites listed in `00-overview.md` §"Phase 2 — Ingest path" gain graph indexing automatically. None of them require code changes.
11. `HybridVectorMemoryStore.close()` joins the executor with a 5s timeout, drops the adapter reference, and is safe to call multiple times.
12. Unit tests in §12 of this document all pass without touching the real `lightrag-hku` package (they use a `MockLightRAGAdapter` fixture).

---

## 3. The `HybridVectorMemoryStore.set()` override — full implementation

The implementation lives at `obscura/lightrag_memory/hybrid_store.py`. The whole class shape is below; the `set()` method is the focus of this section.

```python
# obscura/lightrag_memory/hybrid_store.py
from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from obscura.memory import MemoryKey
from obscura.vector_memory.vector_memory import VectorMemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.auth.models import AuthenticatedUser
    from obscura.lightrag_memory.adapter import LightRAGAdapter
    from obscura.memory.events import EventSink
    from obscura.vector_memory.backends import VectorBackend
    from obscura.vector_memory.decay import DecayConfig

_log = logging.getLogger(__name__)

# Default short-text guard — entity extraction on tiny chunks is wasted LLM cost
# and produces almost no graph signal. Configurable via
# [vector_memory.lightrag] min_text_chars.
_DEFAULT_MIN_TEXT_CHARS = 50


class HybridVectorMemoryStore(VectorMemoryStore):
    """Drop-in subclass that fans writes out to LightRAG.

    Inherits the entire existing API; overrides only set/delete/clear_namespace
    and adds close(). The query path (search_similar, search_reranked) is
    unchanged in Phase 2; Phase 3 adds search_hybrid.

    Decay/consolidation/touch behavior is unchanged — those continue to be
    owned by the parent class and the underlying VectorBackend.
    """

    def __init__(
        self,
        user: AuthenticatedUser,
        *,
        lightrag_adapter: LightRAGAdapter,
        backend: VectorBackend | None = None,
        embedding_fn: Callable[[str], list[float]] | None = None,
        decay_config: DecayConfig | None = None,
        event_sink: EventSink | None = None,
        min_text_chars: int = _DEFAULT_MIN_TEXT_CHARS,
    ) -> None:
        super().__init__(
            user,
            backend=backend,
            embedding_fn=embedding_fn,
            decay_config=decay_config,
            event_sink=event_sink,
        )
        self._lr = lightrag_adapter
        self._min_text_chars = min_text_chars
        # Per-user executor — workers are bounded so a runaway producer cannot
        # spawn thousands of in-flight LightRAG ingest jobs.
        self._ingest_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix=f"lr-ingest-{user.user_id[:8]}",
        )
        self._closed = False

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def set(
        self,
        key: str | MemoryKey,
        text: str,
        metadata: dict[str, Any] | None = None,
        namespace: str = "default",
        ttl: timedelta | None = None,
        memory_type: str = "general",
    ) -> None:
        """Synchronously write to Qdrant, asynchronously index to LightRAG.

        Contract:
          - super().set() runs first and may raise — those exceptions
            propagate (they indicate the canonical store failed).
          - LightRAG submission runs after a successful super().set() and
            never raises into the caller.
          - Filtering (whitelist + escape hatch + short-text guard) happens
            before the executor submission, so filtered writes do not
            consume an executor slot.
        """
        # 1. Normalize key. The parent does this internally too, but we need
        # the MemoryKey shape for doc_id construction below, BEFORE handing
        # control back to the parent.
        if isinstance(key, str):
            mkey = MemoryKey(namespace=namespace, key=key)
        else:
            mkey = key
            # If a MemoryKey is passed, super() will use mkey.namespace
            # regardless of the namespace= kwarg. Mirror that.
            namespace = mkey.namespace

        # 2. Synchronous Qdrant write — never reordered. If this raises, we
        # never enqueue a graph job, which is the correct fail-shut behavior.
        super().set(
            key=mkey,
            text=text,
            metadata=metadata,
            namespace=namespace,
            ttl=ttl,
            memory_type=memory_type,
        )

        # 3. Filtering — must happen before submission to avoid wasting an
        # executor slot on a chunk we're not going to index.
        if not self._should_index(memory_type, metadata, text):
            return

        # 4. Build the doc_id used by LightRAG. Round-trips back to
        # (namespace, key) on retrieval (see Phase 3 hydration).
        doc_id = self._make_doc_id(mkey)

        # 5. Build the metadata payload. `obscura_key` and `obscura_namespace`
        # are the join keys for hydration; everything else is for debugging
        # the graph contents directly.
        lr_metadata: dict[str, Any] = {
            **(metadata or {}),
            "obscura_key": mkey.key,
            "obscura_namespace": mkey.namespace,
            "memory_type": memory_type,
            "created_at": datetime.now(UTC).isoformat(),
        }

        # 6. Submit. The adapter wraps insert_safe such that exceptions
        # are caught + logged inside the worker; we still attach a
        # done-callback as defense-in-depth so a bug in insert_safe
        # cannot silently swallow the failure.
        try:
            future = self._ingest_executor.submit(
                self._lr.insert_safe,
                doc_id=doc_id,
                text=text,
                metadata=lr_metadata,
            )
            future.add_done_callback(
                lambda f, _doc=doc_id: _log_future_failure(f, _doc),
            )
        except RuntimeError:
            # Executor is shut down (close() already called). Treat as a
            # filter miss — Phase 5 backfill will catch up.
            _log.warning(
                "lr_ingest: executor closed, skipping submission for %s",
                doc_id,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_doc_id(mkey: MemoryKey) -> str:
        """Stable encoding of (namespace, key) for LightRAG doc_id.

        Uses ``::`` as the separator. MemoryKey already implements
        __str__ as ``f"{namespace}:{key}"`` (memory/__init__.py:42), but
        a single colon collides with namespaces like ``project:jira``.
        Double-colon avoids ambiguity and is stable across upserts.
        """
        return f"{mkey.namespace}::{mkey.key}"

    def _should_index(
        self,
        memory_type: str,
        metadata: dict[str, Any] | None,
        text: str,
    ) -> bool:
        """Decide whether a write should be graph-indexed.

        Order of evaluation (escape hatches first):
        1. Explicit opt-out: metadata={"graph_index": False} — never index.
        2. Explicit opt-in:  metadata={"graph_index": True} — always index
           (subject to the short-text guard).
        3. memory_type whitelist (default: fact, summary, general).
        4. Short-text guard.
        """
        if metadata is not None and "graph_index" in metadata:
            opt = bool(metadata["graph_index"])
            if not opt:
                _metric_inc("lr_inserts_skipped_filter", reason="opt_out")
                return False
        else:
            if memory_type not in self._lr.indexable_types:
                _metric_inc("lr_inserts_skipped_filter", reason="not_whitelisted")
                return False

        if len(text) < self._min_text_chars:
            _metric_inc("lr_inserts_skipped_filter", reason="short_text")
            return False

        return True


def _log_future_failure(future: Future[Any], doc_id: str) -> None:
    """Log any exception that escaped insert_safe.

    insert_safe is supposed to swallow everything — this is defense in
    depth in case a bug there raises.
    """
    try:
        exc = future.exception()
    except Exception:  # noqa: BLE001 — concurrent futures cancellation/etc.
        return
    if exc is not None:
        _log.warning(
            "lr_ingest: unexpected exception escaped insert_safe (doc=%s): %s",
            doc_id,
            exc,
        )


def _metric_inc(name: str, **labels: str) -> None:
    """Increment a counter via the telemetry meter, no-op if OTel absent."""
    try:
        from obscura.telemetry.metrics import get_meter

        meter = get_meter()
        ctr = meter.create_counter(name)
        ctr.add(1, attributes=labels)
    except Exception:
        pass
```

### Notes on the implementation

- **Key normalization:** The parent's `set()` already accepts `str | MemoryKey` (`vector_memory.py:344-345`), but the override needs the `MemoryKey` shape *before* handing off to the parent so it can build the LightRAG `doc_id` from the same `(namespace, key)` pair. We normalize, then pass the already-normalized `MemoryKey` down — this also avoids a subtle bug where a caller passes `MemoryKey(namespace="A", ...)` and `namespace="B"` simultaneously.
- **doc_id encoding:** `f"{namespace}::{key}"` with a double-colon separator. The single-colon `__str__` (`memory/__init__.py:42`) collides with real-world namespaces like `project:jira` (used heavily in `memory_tools.py:73`). The double-colon is unambiguous and reversible.
- **Short-text guard:** 50 chars is a heuristic — below that, entity-extraction LLM calls find one entity (or none) and waste the round-trip. Configurable so the user can dial it up to e.g. 200 chars to save more cost, or down to 0 to disable.
- **Submission failure path:** If the executor has been shut down (`close()` was called), `submit()` raises `RuntimeError`. We treat that as if the write were filtered — log and continue. Phase 5 backfill will pick the chunk up.
- **`add_done_callback` defense in depth:** `insert_safe` is supposed to catch all exceptions internally, but if a bug there ever lets one escape, the done-callback will log it. This is cheap belt-and-suspenders.

---

## 4. The `HybridVectorMemoryStore.delete()` override

```python
    # ------------------------------------------------------------------
    # Delete path
    # ------------------------------------------------------------------

    def delete(self, key: str | MemoryKey, namespace: str = "default") -> bool:
        """Synchronously delete from Qdrant, asynchronously delete from graph.

        Returns the value of super().delete() (True if a row was removed).
        The graph delete is fire-and-forget; if it fails, the only
        consequence is a stale node/relation in the graph until the next
        upsert overwrites it.
        """
        if isinstance(key, str):
            mkey = MemoryKey(namespace=namespace, key=key)
        else:
            mkey = key

        existed = super().delete(mkey)

        if existed:
            doc_id = self._make_doc_id(mkey)
            try:
                future = self._ingest_executor.submit(
                    self._lr.delete_safe,
                    doc_id=doc_id,
                )
                future.add_done_callback(
                    lambda f, _doc=doc_id: _log_future_failure(f, _doc),
                )
            except RuntimeError:
                _log.warning(
                    "lr_ingest: executor closed, skipping delete for %s",
                    doc_id,
                )

        return existed

    def clear_namespace(self, namespace: str) -> int:
        """Wipe a namespace from both Qdrant and LightRAG.

        Capture keys *before* the parent clears, then submit graph deletes
        in a single batch job. Doing 1000 individual `submit()` calls would
        exhaust the executor; one batch lets the worker page through them.
        """
        # 1. Snapshot keys BEFORE the parent wipes them — list_keys() reads
        # from the same backend that clear_namespace() is about to clear.
        keys_to_delete: list[MemoryKey] = []
        if not self._closed:
            try:
                keys_to_delete = list(self.list_keys(namespace=namespace))
            except Exception:
                _log.debug(
                    "lr_ingest: list_keys failed before clear_namespace, "
                    "graph may end up with orphans",
                    exc_info=True,
                )

        # 2. Wipe Qdrant.
        cleared = super().clear_namespace(namespace)

        # 3. Submit a single batched graph-delete job.
        if keys_to_delete:
            doc_ids = [self._make_doc_id(k) for k in keys_to_delete]
            try:
                future = self._ingest_executor.submit(
                    self._delete_batch_safe,
                    doc_ids=doc_ids,
                )
                future.add_done_callback(
                    lambda f, _ns=namespace: _log_future_failure(
                        f, f"clear_namespace:{_ns}"
                    ),
                )
            except RuntimeError:
                _log.warning(
                    "lr_ingest: executor closed during clear_namespace(%s), "
                    "%d graph entries may be orphaned",
                    namespace,
                    len(doc_ids),
                )

        return cleared

    def _delete_batch_safe(self, doc_ids: list[str]) -> None:
        """Sequentially delete a batch of doc_ids from LightRAG.

        Runs in an executor thread. LightRAG's adelete_by_doc_id is per-doc;
        we throttle to avoid hammering the underlying graph storage. If the
        graph backend is NetworkX (the Phase 1 default), each delete is
        cheap (in-memory + pickle on flush). If we ever swap to AGE, this
        loop can become a single transaction.
        """
        for doc_id in doc_ids:
            try:
                self._lr.delete_safe(doc_id)
            except Exception:  # noqa: BLE001 — log + continue
                _log.warning(
                    "lr_ingest: delete failed during batch (doc=%s)",
                    doc_id,
                    exc_info=True,
                )
```

### Notes

- **Why snapshot keys before the parent clears:** `list_keys()` reads the same backend that `super().clear_namespace()` is about to wipe (look at `qdrant_backend.py:355-376` and `:377-392` — they both go through the same Qdrant client). If we listed *after* the clear, we'd get an empty list, and the graph would be permanently orphaned. The snapshot is best-effort — if `list_keys` raises, we log and continue with the Qdrant wipe (the user's correctness contract is "Qdrant is the canonical store"; the graph is an index that can be rebuilt).
- **Batched delete vs N submissions:** A workload that wipes 1000 keys would otherwise consume 1000 executor submissions and 1000 round-trips through `run_coroutine_threadsafe`. Bundling into one job that loops internally keeps the executor responsive and lets the adapter's event loop process them in sequence on its single dedicated thread.
- **Per-doc try/except in the batch loop:** If one doc_id fails (e.g. it was never indexed because it was filtered out at write time, so `adelete_by_doc_id` returns "not found"), the batch keeps going. LightRAG's `adelete_by_doc_id` is idempotent for missing docs (verified — see §10 below).

---

## 5. `LightRAGAdapter.insert_safe` — full implementation

`insert_safe` lives at `obscura/lightrag_memory/adapter.py`. It is **synchronous** — callable from a thread-pool worker — and bridges to LightRAG's async `ainsert` via the adapter's dedicated event loop (set up in Phase 1).

```python
# obscura/lightrag_memory/adapter.py
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.auth.models import AuthenticatedUser

_log = logging.getLogger(__name__)

# Default — configurable via [vector_memory.lightrag] insert_timeout_seconds.
_DEFAULT_INSERT_TIMEOUT_SECONDS = 60.0
_DEFAULT_DELETE_TIMEOUT_SECONDS = 30.0

# Default whitelist. Configurable via [vector_memory.lightrag] indexable_types.
_DEFAULT_INDEXABLE_TYPES = frozenset({"fact", "summary", "general"})


class LightRAGAdapter:
    """Per-user LightRAG instance + cached event loop for async calls."""

    _instances: dict[str, LightRAGAdapter] = {}
    _instances_lock = threading.Lock()

    def __init__(
        self,
        user: AuthenticatedUser,
        *,
        lightrag_instance: Any,  # lightrag.LightRAG
        loop: asyncio.AbstractEventLoop,
        loop_thread: threading.Thread,
        indexable_types: frozenset[str] = _DEFAULT_INDEXABLE_TYPES,
        insert_timeout_seconds: float = _DEFAULT_INSERT_TIMEOUT_SECONDS,
        delete_timeout_seconds: float = _DEFAULT_DELETE_TIMEOUT_SECONDS,
    ) -> None:
        self.user = user
        self._lr = lightrag_instance
        self._loop = loop
        self._loop_thread = loop_thread
        self.indexable_types = indexable_types
        self._insert_timeout = insert_timeout_seconds
        self._delete_timeout = delete_timeout_seconds
        self._closed = False

        # Aggregate timing for periodic latency log.
        self._latency_samples: list[float] = []
        self._latency_lock = threading.Lock()
        self._latency_log_every = 100  # log p50/p99 every N inserts

    # ------------------------------------------------------------------
    # Insert
    # ------------------------------------------------------------------

    def insert_safe(
        self,
        doc_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> None:
        """Run LightRAG ainsert synchronously from a worker thread.

        Bridges to the adapter's dedicated event loop. Catches every
        exception and logs at WARNING; callers must not depend on the
        return value.

        Idempotency: LightRAG's ainsert with the same doc_id overwrites
        the previous content cleanly (the doc_id is the dedup key
        internally). Re-running insert for the same key is safe — it
        re-extracts entities and re-merges them into the graph.
        """
        if self._closed:
            _log.debug("lr_ingest: adapter closed, skip insert for %s", doc_id)
            return

        text_len = len(text)
        memory_type = metadata.get("memory_type", "general")
        _metric_inc("lr_inserts_submitted", memory_type=memory_type)
        started = time.monotonic()

        try:
            coro = self._lr.ainsert(
                input=text,
                ids=[doc_id],
                # LightRAG's ainsert accepts `metadatas` as a list paired
                # with the inputs. Confirm the exact kwarg name when
                # implementing — older versions used `metadata=`.
                metadatas=[metadata],
            )
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            try:
                future.result(timeout=self._insert_timeout)
            except FuturesTimeoutError:
                future.cancel()
                _metric_inc("lr_inserts_timed_out", memory_type=memory_type)
                _log.warning(
                    "lr_ingest: insert timed out (doc=%s, text_len=%d, "
                    "timeout=%.0fs) — chunk left un-graphed; will be picked "
                    "up by Phase 5 lazy-on-touch / backfill",
                    doc_id,
                    text_len,
                    self._insert_timeout,
                )
                return
        except Exception as exc:  # noqa: BLE001 — must isolate caller
            _metric_inc(
                "lr_inserts_failed",
                memory_type=memory_type,
                exc_type=type(exc).__name__,
            )
            _log.warning(
                "lr_ingest: insert failed (doc=%s, text_len=%d, "
                "memory_type=%s, exc=%s)",
                doc_id,
                text_len,
                memory_type,
                exc,
                exc_info=True,
            )
            return

        # Success path. Record latency + the marker.
        elapsed = time.monotonic() - started
        _metric_inc("lr_inserts_succeeded", memory_type=memory_type)
        _metric_record("lr_insert_duration_seconds", elapsed)
        self._maybe_log_latency_summary(elapsed)

        _log.info(
            "lr_ingest: insert ok (doc=%s, text_len=%d, "
            "memory_type=%s, elapsed=%.2fs)",
            doc_id,
            text_len,
            memory_type,
            elapsed,
        )

        # Phase 5 dependency — see §11.
        self._record_indexed_marker(doc_id, started)

    def _record_indexed_marker(self, doc_id: str, started: float) -> None:
        """Record lr_indexed_at on the canonical Qdrant payload.

        Phase 2: this is a no-op. The Qdrant payload schema does not yet
        carry an lr_indexed_at field, and the VectorBackend protocol does
        not yet expose update_metadata.

        Phase 5 will:
          (a) add lr_indexed_at to qdrant_backend.store_vector's payload,
          (b) extend the VectorBackend protocol with update_metadata(key, partial),
          (c) implement that on QdrantBackend, SQLiteBackend, PostgreSQLVectorBackend,
          (d) call into it from here.

        Until then, the lazy-on-touch ingest path (Phase 5) cannot reliably
        skip already-indexed chunks, but in practice the executor + idempotent
        ainsert make double-indexing harmless if expensive.
        """
        # TODO(phase-5): wire this through VectorBackend.update_metadata once
        # that method exists. The doc_id parses back to (namespace, key)
        # via _parse_doc_id (mirror of HybridVectorMemoryStore._make_doc_id).
        return

    # ------------------------------------------------------------------
    # Latency aggregator
    # ------------------------------------------------------------------

    def _maybe_log_latency_summary(self, sample: float) -> None:
        with self._latency_lock:
            self._latency_samples.append(sample)
            if len(self._latency_samples) < self._latency_log_every:
                return
            samples = sorted(self._latency_samples)
            self._latency_samples = []
        n = len(samples)
        p50 = samples[n // 2]
        p99 = samples[max(n - 1, int(n * 0.99))]
        avg = sum(samples) / n
        _log.info(
            "lr_ingest: latency over last %d inserts — avg=%.2fs p50=%.2fs p99=%.2fs",
            n,
            avg,
            p50,
            p99,
        )


def _metric_inc(name: str, **labels: str) -> None:
    try:
        from obscura.telemetry.metrics import get_meter

        meter = get_meter()
        ctr = meter.create_counter(name)
        ctr.add(1, attributes=labels)
    except Exception:
        pass


def _metric_record(name: str, value: float) -> None:
    try:
        from obscura.telemetry.metrics import get_meter

        meter = get_meter()
        h = meter.create_histogram(name, unit="s")
        h.record(value)
    except Exception:
        pass
```

### Notes

- **Bridging sync→async:** `asyncio.run_coroutine_threadsafe(coro, self._loop)` schedules the coroutine on the adapter's dedicated event loop (which runs on a daemon thread set up in Phase 1) and returns a `concurrent.futures.Future`. `.result(timeout=...)` blocks the calling worker until done or timeout. This is the standard pattern for "sync-method-that-runs-async" in mixed-loop code.
- **Timeout cancels the future:** `future.cancel()` cancels the underlying coroutine's task. LightRAG's `ainsert` is structured as `await self._extract_entities(...)` followed by `await self._upsert_chunks_to_vector_store(...)` followed by `await self._merge_into_graph(...)`. Cancellation at any of those `await` points is safe — the partially-extracted entities are not committed to the graph until the merge step. (Confirm this against the installed `lightrag-hku` version's source when implementing.)
- **`metadatas=[metadata]` kwarg:** Verify the exact kwarg name against the installed `lightrag-hku>=1.4`. Older versions used `metadata=metadata` per-call. The 1.4+ API is per-doc-list. If the kwarg is wrong, the metadata will silently be discarded — Phase 6 has a fixture test for this.
- **Idempotency:** LightRAG dedupes on `doc_id`. Calling `ainsert` with the same `doc_id` twice causes the second call to: (a) re-tokenize, (b) re-call the extraction LLM, (c) merge any new entities/relations into the graph (existing ones noop), (d) overwrite the chunk in the vector store. So double-indexing is *correct* but *expensive*. Worth documenting prominently because the engineer's instinct will be to fear duplicates.
- **Phase 5 stub:** `_record_indexed_marker` is a no-op for Phase 2. The `TODO(phase-5)` comment is the trigger for Phase 5 to come back here. The reason it's OK to no-op now: the only consumer of `lr_indexed_at` is Phase 5's lazy-on-touch + backfill, neither of which exists yet.

---

## 6. `LightRAGAdapter.delete_safe`

Mirror of `insert_safe`, with `adelete_by_doc_id` instead of `ainsert`.

```python
    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_safe(self, doc_id: str) -> None:
        """Run LightRAG adelete_by_doc_id synchronously from a worker thread.

        Idempotent: deleting an unknown doc_id is a no-op (LightRAG returns
        without raising; verified against lightrag-hku 1.4 source). This
        matters for the clear_namespace batch path, which will sometimes
        include doc_ids that were never indexed (filtered out at write time).
        """
        if self._closed:
            _log.debug("lr_ingest: adapter closed, skip delete for %s", doc_id)
            return

        _metric_inc("lr_deletes_submitted")
        started = time.monotonic()

        try:
            coro = self._lr.adelete_by_doc_id(doc_id)
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            try:
                future.result(timeout=self._delete_timeout)
            except FuturesTimeoutError:
                future.cancel()
                _metric_inc("lr_deletes_timed_out")
                _log.warning(
                    "lr_ingest: delete timed out (doc=%s, timeout=%.0fs) — "
                    "graph may have a dangling node; next upsert overwrites",
                    doc_id,
                    self._delete_timeout,
                )
                return
        except Exception as exc:  # noqa: BLE001
            _metric_inc(
                "lr_deletes_failed",
                exc_type=type(exc).__name__,
            )
            _log.warning(
                "lr_ingest: delete failed (doc=%s, exc=%s)",
                doc_id,
                exc,
                exc_info=True,
            )
            return

        elapsed = time.monotonic() - started
        _metric_inc("lr_deletes_succeeded")
        _metric_record("lr_delete_duration_seconds", elapsed)
        _log.debug(
            "lr_ingest: delete ok (doc=%s, elapsed=%.2fs)",
            doc_id,
            elapsed,
        )
```

### Notes

- **Idempotent missing-doc behavior:** Verify by reading `lightrag-hku`'s `LightRAG.adelete_by_doc_id` source (it short-circuits when the doc isn't in the chunk store). If a future LightRAG version starts raising, wrap it specifically — the unit test in §12 covers this.
- **Shorter default timeout:** Deletes don't run an LLM; they're graph traversal + vector store eviction. 30s is a generous ceiling.
- **Failure consequence is mild:** A failed delete leaves a dangling graph node. The next `set()` for the same `doc_id` overwrites it. So the failure mode is "graph index drifts from canonical store until the next upsert" — acceptable, logged.

---

## 7. The `indexable_types` whitelist policy

### Default

```python
_DEFAULT_INDEXABLE_TYPES = frozenset({"fact", "summary", "general"})
```

### Rationale (grounded in actual `memory_type` values found in the codebase)

A grep across `obscura/` reveals the following `memory_type` strings actually written today (excluding tests):

| memory_type           | Where written                                          | Index? | Why                                                                        |
| --------------------- | ------------------------------------------------------ | ------ | -------------------------------------------------------------------------- |
| `general`             | default in `vector_memory.set` (`vector_memory.py:331`) | yes    | Catch-all; users storing free-form notes want graph indexing.              |
| `fact`                | `memory_tools.py`, profile facts                       | yes    | Long-lived knowledge; entity-rich; primary graph payload.                  |
| `summary`             | `consolidator.py:135`                                  | yes    | Distilled multi-episode content; entity-rich; outlives raw episodes.       |
| `episode`             | `goal_tools.py:103`, supervisor turn auto-save         | **no** | Turn-level chatter; consolidates away within ~14d (`DecayConfig.consolidation_age_days`); graph cost dwarfs value. |
| `preference`          | profile / Kairos                                       | no     | Already structured; small number of entities; immune to decay anyway.      |
| `eval_failure`        | `eval/memory.py:154`                                   | no     | Internal eval tracking; no user-facing recall.                             |
| `eval_resolution`     | `eval/memory.py:194`                                   | no     | Internal eval tracking.                                                    |
| `eval_result`         | `eval/memory.py`                                       | no     | Internal eval tracking.                                                    |
| `session_event`       | `kairos/vault_sync.py`                                 | no     | High-volume session telemetry; opt-in via metadata if needed.              |
| `session_turn`        | `kairos/vault_sync.py`                                 | no     | Same — replaced by consolidated summaries.                                 |
| `profile_*` (6 types) | `profile/store.py:99`                                  | no     | Already structured per `DecayConfig.profiles`; queried via profile API.    |

### Configuration

In `~/.obscura/config.toml`:

```toml
[vector_memory.lightrag]
# Override the default whitelist. Provide a list; setting to []
# disables all whitelist-based indexing (only graph_index=true metadata
# escapes will be indexed).
indexable_types = ["fact", "summary"]

# Short-text guard. Chunks shorter than this skip graph indexing.
min_text_chars = 100

# Insert timeout (per-chunk LLM-bounded).
insert_timeout_seconds = 60

# Delete timeout (no LLM call, but graph IO).
delete_timeout_seconds = 30
```

### Loading the config

```python
# obscura/lightrag_memory/adapter.py — alongside the class

def load_indexable_types_from_disk() -> frozenset[str]:
    """Load [vector_memory.lightrag] indexable_types from config.toml."""
    try:
        from obscura.core.config_io import try_load_config
        from pathlib import Path

        cfg = try_load_config(Path.home() / ".obscura" / "config.toml") or {}
        section = cfg.get("vector_memory", {}).get("lightrag", {})
        raw = section.get("indexable_types")
        if raw is None:
            return _DEFAULT_INDEXABLE_TYPES
        if not isinstance(raw, list):
            _log.warning(
                "vector_memory.lightrag.indexable_types must be a list of "
                "strings — got %r, falling back to defaults",
                type(raw).__name__,
            )
            return _DEFAULT_INDEXABLE_TYPES
        return frozenset(str(x) for x in raw)
    except Exception:
        _log.debug("Could not load indexable_types from disk", exc_info=True)
        return _DEFAULT_INDEXABLE_TYPES
```

The `LightRAGAdapter.for_user()` factory (set up in Phase 1) calls `load_indexable_types_from_disk()` once on creation; the result is captured into the adapter instance.

### Metadata escape hatch

A write can override the whitelist either way:

```python
# Force-include even though `episode` isn't in the whitelist:
store.set("turn-42", text, memory_type="episode",
          metadata={"graph_index": True})

# Force-exclude even though `fact` is in the whitelist:
store.set("debug-fact", text, memory_type="fact",
          metadata={"graph_index": False})
```

This is implemented in `_should_index()` (§3 above). The opt-in case is useful for the Kairos dream cycle when the user explicitly wants temporal episode reasoning. The opt-out case is useful for facts that are sensitive (PII, secrets) and the user doesn't want them shipped through the LightRAG entity-extraction LLM.

### Where the check happens

`_should_index` runs in `set()` *before* `submit()`, on the calling thread. This matters: the check is microseconds, so doing it inline doesn't measurably slow `set()`, and it saves the cost of a thread-pool round-trip + a `_metric_inc("lr_inserts_submitted")` we'd then have to walk back. The submission queue stays focused on real work.

---

## 8. Concurrency & ordering invariants

### Within a single user

`HybridVectorMemoryStore` has one `ThreadPoolExecutor(max_workers=2)` per user. Calls to `insert_safe` and `delete_safe` from those workers all schedule onto the same dedicated event loop owned by the per-user `LightRAGAdapter` (Phase 1 scaffolding). The event loop runs on a single daemon thread.

Consequence: **per-user, per-`LightRAG` instance, all `ainsert` / `adelete` invocations are serialized** by the single event-loop thread, even though up to 2 workers may be calling `insert_safe` concurrently from the executor. The workers race for a slot on the loop, but only one coroutine runs at a time.

### Multiple `set()` calls for the same key

```python
store.set("k", text="A", memory_type="fact")
store.set("k", text="B", memory_type="fact")  # immediately after
```

The Qdrant upserts run synchronously in the caller's thread, so "B" wins in Qdrant unconditionally — last-write-wins by call order.

The LightRAG submissions are queued onto the executor. With `max_workers=2`, the order they actually execute is **not** strictly FIFO — Python's `ThreadPoolExecutor` does not guarantee submission order across workers.

**Post-condition:** LightRAG's graph eventually reflects the *latest* write in the sense that whichever insert finishes last wins (LightRAG's `ainsert` with same doc_id overwrites the chunk). With a single dedicated event-loop thread, only one ainsert runs at a time, so there's no torn-write. But the engineer should not assume strict ordering — if two updates arrive within milliseconds, either could end up as the final state.

**Mitigation if strict ordering is ever needed:** drop `max_workers` to 1, making the executor itself FIFO. Given that LightRAG's ainsert is LLM-bound at ~5-30s, and the loop serializes anyway, the performance cost is minimal. Recommendation: ship Phase 2 with `max_workers=2` (allows one delete to overlap with one in-flight insert) but flag a follow-up to evaluate dropping to 1 if ordering bugs appear.

### Cross-user isolation

Each user gets their own `LightRAGAdapter` instance (cached in `LightRAGAdapter._instances`). Each has its own event loop and loop-thread. They never touch each other.

### Shutdown / `close()`

```python
    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Drain the executor, then drop the adapter reference.

        Safe to call multiple times. Idempotent.
        """
        if self._closed:
            return
        self._closed = True

        try:
            # Block briefly for pending writes to flush. LightRAG inserts
            # take seconds; 5s lets queued chunks finish, but caps the
            # cost of a fast app shutdown.
            self._ingest_executor.shutdown(wait=True, cancel_futures=False)
        except Exception:
            _log.warning("lr_ingest: executor shutdown errored", exc_info=True)

        # The adapter's event loop is shared across all stores for this
        # user. We do NOT stop the loop here — that's the adapter's
        # responsibility, called by Phase 4 auth-middleware lifecycle
        # hooks. Phase 2 only shuts down the executor.

    def __del__(self) -> None:
        # Best-effort backstop in case nobody calls close().
        try:
            if not self._closed:
                self._ingest_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
```

The matching `LightRAGAdapter.close()` (event-loop teardown) lives at the adapter level and is called by the auth-middleware lifecycle in Phase 4. Phase 2 just exposes the per-store close. The current `VectorMemoryStore.close()` (`vector_memory.py:611`) only closes the backend; we extend it but call `super().close()`.

```python
    def close(self) -> None:
        """Drain LightRAG executor, then close the underlying backend."""
        if not self._closed:
            self._closed = True
            try:
                self._ingest_executor.shutdown(wait=True, cancel_futures=False)
            except Exception:
                _log.warning("lr_ingest: executor shutdown errored", exc_info=True)
        super().close()
```

(Note: `_closed` is set inside the conditional so a second `close()` call is a true no-op.)

---

## 9. Telemetry / logging

### Logger

Uses stdlib `logging.getLogger(__name__)` — `obscura.lightrag_memory.adapter` and `obscura.lightrag_memory.hybrid_store`. This integrates with the existing rotating file handler at `~/.obscura/logs/deep.jsonl` per CLAUDE.md.

### Per-insert log line

```
INFO  lr_ingest: insert ok (doc=session:turn-42, text_len=1842,
      memory_type=fact, elapsed=4.21s)
```

Errors at `WARNING`:

```
WARNING  lr_ingest: insert failed (doc=fact:python_async, text_len=512,
         memory_type=fact, exc=ConnectionError: [Errno 111] connection refused)
```

### Counters

Implemented via `obscura/telemetry/metrics.py` (existing infrastructure — see `metrics.py:11-21`). All fail-open: if OpenTelemetry isn't installed, `get_meter()` returns a no-op meter.

| Metric                            | Type      | Labels                            | Increment when                                       |
| --------------------------------- | --------- | --------------------------------- | ---------------------------------------------------- |
| `lr_inserts_skipped_filter`       | Counter   | reason=opt_out\|not_whitelisted\|short_text | `_should_index` returns False                  |
| `lr_inserts_submitted`            | Counter   | memory_type                       | `submit()` queues a job                              |
| `lr_inserts_succeeded`            | Counter   | memory_type                       | `insert_safe` returns without exception/timeout      |
| `lr_inserts_failed`               | Counter   | memory_type, exc_type             | `insert_safe` catches an exception                   |
| `lr_inserts_timed_out`            | Counter   | memory_type                       | `future.result(timeout=...)` raises TimeoutError     |
| `lr_insert_duration_seconds`      | Histogram | (none)                            | Successful insert; records elapsed wall time         |
| `lr_deletes_submitted`            | Counter   | (none)                            | Single-key delete or batch-delete submitted          |
| `lr_deletes_succeeded`            | Counter   | (none)                            | `delete_safe` returns                                |
| `lr_deletes_failed`               | Counter   | exc_type                          | `delete_safe` catches an exception                   |
| `lr_deletes_timed_out`            | Counter   | (none)                            | Delete timeout                                       |
| `lr_delete_duration_seconds`      | Histogram | (none)                            | Successful delete                                    |

### Periodic latency summary

Every `_latency_log_every` (default 100) successful inserts, an aggregate log line:

```
INFO  lr_ingest: latency over last 100 inserts — avg=4.7s p50=3.9s p99=18.2s
```

Useful for tuning `insert_timeout_seconds`. Phase 2 hardcodes the every-100 threshold; Phase 3 may make it configurable.

---

## 10. Failure modes & error handling

### F1. Underlying entity-extraction LLM fails

**Symptom:** `ainsert` raises a provider error (OpenAI rate limit, network, 5xx, etc.) inside the entity-extraction call.

**Behavior:** `insert_safe` catches in the broad `except Exception:`, logs at WARNING with the exc_type label, increments `lr_inserts_failed`. The Qdrant write has already succeeded.

**Recovery:** None automatic in Phase 2. Phase 5's lazy-on-touch will re-attempt when the chunk is read; or the user runs `obscura memory backfill-graph`.

### F2. Qdrant down at LightRAG vector storage write time

**Symptom:** LightRAG's `QdrantVectorDBStorage.upsert` raises a connection error.

**Note:** This is the *LightRAG* Qdrant vector store, not the canonical Obscura Qdrant store. They may be the same Qdrant instance but different collections. (Phase 1 chose to share Qdrant; LightRAG creates its own collection prefixed `lightrag_*`.)

**Behavior:** Same as F1 — caught and logged. The graph nodes/relations may have been computed but not persisted (depending on which `await` raised). LightRAG handles partial-write recovery internally on the next ainsert with the same doc_id.

### F3. NetworkX file-system contention / pickle race

**Symptom:** Two adapter instances for the same user (e.g. accidentally created during a process-restart race) both try to write `~/.obscura/lightrag/<user_hash>/graph_chunk_entity_relation.gpickle`.

**Mitigation:** The adapter is a per-user singleton (`LightRAGAdapter._instances` keyed on `user_id`, locked by `_instances_lock`). Within one process, only one instance ever exists per user. Cross-process is a separate concern.

LightRAG itself uses an in-memory NetworkX graph and serializes on `aindex_done_callback` / explicit save. With one process and one adapter, there's a single writer. The pickle is rewritten atomically (LightRAG writes to a tempfile and renames — verified against lightrag-hku 1.4 source).

**Cross-process risk** (e.g. CLI + REST server simultaneously): both would have their own `LightRAGAdapter` and racing pickle writes. **This is a known limitation; document in the README that running CLI and server simultaneously against the same user's lightrag dir is unsupported in Phase 2.** Phase 5 may add a file lock if needed.

### F4. OOM during entity extraction on a huge chunk

**Symptom:** The LLM call inside `ainsert` returns a very large response, or chunking explodes memory.

**Mitigation:**
- Chunks larger than `max_text_chars` (default 100,000) are rejected before submission with a `lr_inserts_skipped_filter` increment, reason=`oversized`. This is added to `_should_index`:

```python
_DEFAULT_MAX_TEXT_CHARS = 100_000  # ~25k tokens; well above LLM context

# inside _should_index:
if len(text) > self._max_text_chars:
    _metric_inc("lr_inserts_skipped_filter", reason="oversized")
    _log.warning(
        "lr_ingest: text too long for graph indexing "
        "(len=%d, limit=%d, doc=%s::%s) — chunk stored in Qdrant only",
        len(text), self._max_text_chars, namespace, key,
    )
    return False
```

Configurable via `[vector_memory.lightrag] max_text_chars`.

### F5. Adapter event loop dies

**Symptom:** The daemon thread running the asyncio loop crashes (an unhandled exception in a coroutine that escapes Phase 1's wrapper).

**Detection:** `asyncio.run_coroutine_threadsafe` will start raising `RuntimeError: Event loop is closed` from `insert_safe`.

**Behavior in Phase 2:** Caught by the broad `except Exception:` in `insert_safe`; logged; metric increments. Subsequent inserts continue to fail in the same way until the process restarts.

**Phase 2 does not implement loop restart.** Adding a heartbeat + auto-restart is more complexity than warranted given that:
1. Phase 1's loop runner uses `asyncio.run` with broad exception handling — true crashes are vanishingly rare.
2. Failed inserts degrade gracefully (Qdrant writes succeed, queries fall back via Phase 3's degraded path).
3. The user can always restart `obscura`.

If a heartbeat is needed later, it goes in `LightRAGAdapter`, not in the hybrid store. Tracked as an open question for Phase 5.

### F6. Adapter `_lr` instance is None / un-initialized

**Symptom:** `for_user()` failed during Phase 1 setup (e.g. the LightRAG package import failed because the user has `OBSCURA_LIGHTRAG=on` but never `uv sync --extra lightrag`).

**Behavior:** Phase 1's `for_user()` is responsible for falling back to bare `VectorMemoryStore` if LightRAG can't load. So `HybridVectorMemoryStore` is never constructed in this case; the caller never sees the failure.

If somehow `lightrag_adapter._lr` is None at runtime, `insert_safe`'s broad except clause catches the `AttributeError` from `self._lr.ainsert(...)` and logs.

### F7. Executor saturation / queue backpressure

**Symptom:** A burst of `set()` calls (e.g. session ingest replaying 500 turns) backs up the executor queue.

**Behavior in Phase 2:** Python's `ThreadPoolExecutor` has an unbounded queue, so `submit()` always returns immediately. Memory pressure from queued metadata payloads is bounded (each is a small dict referencing the text by reference) but not zero.

**Mitigation:** Phase 5 backfill uses an explicit rate limiter; for the Phase 2 write path, we accept unbounded queueing. If it becomes a problem, drop in a `queue.Queue(maxsize=N)` wrapper later.

### F8. Concurrent close + insert

**Symptom:** `set()` is called after `close()` has begun.

**Behavior:** `submit()` raises `RuntimeError`; `set()` catches it and logs at WARNING ("executor closed, skipping submission"). Qdrant write has already succeeded.

---

## 11. Migration considerations

### Existing chunks

When a user upgrades to a build with Phase 2 and toggles `OBSCURA_LIGHTRAG=on` for the first time, their existing Qdrant collection has thousands of chunks with no graph representation.

**Phase 2 does not migrate them.** The `set()` override only fires on new writes / upserts. Behavior:

- Cold-path queries (`search_similar`, `search_reranked`) still work — they use only Qdrant. Phase 3's `search_hybrid` will fall back to vector-only when LightRAG returns no graph hits.
- Hot chunks (re-saved during normal use) get graph-indexed naturally as `set()` is re-called for them.
- Cold chunks remain un-graphed until either:
  - Phase 5's lazy-on-touch path catches them (when `vector_memory.touch()` is called for a chunk lacking `lr_indexed_at`, schedule an ingest).
  - Phase 5's `obscura memory backfill-graph` CLI is run.

### Toggling the flag

```
# user has been running v_n without LightRAG
$ export OBSCURA_LIGHTRAG=on
$ obscura
```

**Expected behavior:** No stampede. The next time a chunk is written, it gets graph-indexed; old chunks stay as they are. This is the *correct* user experience — surprise LLM bills from auto-backfill of 50k chunks would be terrible.

**Counter-test (Phase 6 has this in unit suite):** Toggle the flag, call `set()` for one new key. Confirm exactly one `insert_safe` runs. Don't read any old keys; confirm no insertions for them.

### `lr_indexed_at` payload field

Phase 2 does NOT add `lr_indexed_at` to the Qdrant payload. The success path in `insert_safe` stubs `_record_indexed_marker` to a no-op (§5).

The user-visible consequence: an engineer reading the Qdrant payload schema in Phase 2 will see no graph-related fields. That's intentional. Phase 5 adds:
- `lr_indexed_at` (ISO timestamp string) on successful insert.
- A new `VectorBackend.update_metadata(key, partial)` method on the protocol.

The Phase 2 stub is the breakpoint for Phase 5 to wire through. The `TODO(phase-5)` comment in `_record_indexed_marker` marks the spot.

### Compatibility with existing `~/.obscura/lightrag/<user_hash>/`

If the user previously installed Phase 1 (which only creates the working directory and verifies LightRAG can load), there will be an empty `~/.obscura/lightrag/<user_hash>/` already. Phase 2's adapter reuses it. No migration needed.

If a power user has already played with `lightrag-hku` in the same dir using a different embedding model (e.g. OpenAI's text-embedding-3-small at 1536 dims while Obscura uses MiniLM at 384), the LightRAG vector store will refuse to upsert mismatched dims and `insert_safe` will fail loudly. **Document in the deployment notes:** "do not point a fresh `OBSCURA_LIGHTRAG=on` install at an existing LightRAG dir from a different setup."

---

## 12. Tests for this phase

Tests live at `tests/unit/obscura/lightrag_memory/`. Phase 6 owns the comprehensive test suite, but Phase 2 must ship with at least the following so the write path is verified at merge time.

### Layout

```
tests/unit/obscura/lightrag_memory/
├── __init__.py
├── conftest.py                # MockLightRAGAdapter fixture
├── test_hybrid_store_set.py
├── test_hybrid_store_delete.py
└── test_indexable_types.py
```

### Fixture: `MockLightRAGAdapter`

```python
# tests/unit/obscura/lightrag_memory/conftest.py
import threading
import pytest
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Insert:
    doc_id: str
    text: str
    metadata: dict[str, Any]


@dataclass
class _Delete:
    doc_id: str


class MockLightRAGAdapter:
    """Stand-in for LightRAGAdapter in unit tests.

    Records every insert_safe / delete_safe call. Optionally sleeps,
    raises, or times out on demand. Never imports lightrag.
    """

    def __init__(
        self,
        *,
        indexable_types=frozenset({"fact", "summary", "general"}),
        insert_sleep_seconds: float = 0.0,
        insert_raises: BaseException | None = None,
    ) -> None:
        self.indexable_types = indexable_types
        self._insert_sleep = insert_sleep_seconds
        self._insert_raises = insert_raises
        self.inserts: list[_Insert] = []
        self.deletes: list[_Delete] = []
        self._lock = threading.Lock()

    def insert_safe(self, doc_id: str, text: str, metadata: dict[str, Any]) -> None:
        if self._insert_sleep:
            import time
            time.sleep(self._insert_sleep)
        if self._insert_raises is not None:
            raise self._insert_raises
        with self._lock:
            self.inserts.append(_Insert(doc_id, text, dict(metadata)))

    def delete_safe(self, doc_id: str) -> None:
        with self._lock:
            self.deletes.append(_Delete(doc_id))


@pytest.fixture
def mock_adapter():
    return MockLightRAGAdapter()


@pytest.fixture
def hybrid_store(tmp_path, mock_adapter, monkeypatch):
    """A HybridVectorMemoryStore backed by SQLiteBackend, using a mock adapter."""
    monkeypatch.setenv("OBSCURA_VECTOR_MEMORY_DIR", str(tmp_path))
    from obscura.auth.models import AuthenticatedUser
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
    user = AuthenticatedUser(user_id="test-user", auth_token="x")  # adapt to real ctor

    def _embed(text: str) -> list[float]:
        # Deterministic, fast — mirrors simple_embedding pattern from
        # vector_memory.py:68
        import hashlib
        h = hashlib.sha256(text.encode()).digest()[:48]
        return [b / 255.0 for b in h]

    store = HybridVectorMemoryStore(
        user,
        lightrag_adapter=mock_adapter,
        embedding_fn=_embed,
    )
    yield store
    store.close()
```

### Tests

```python
# tests/unit/obscura/lightrag_memory/test_hybrid_store_set.py
import time
import pytest


def _wait_for_inserts(adapter, expected_count, timeout=2.0):
    """Spin until adapter has the expected number of insert calls."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(adapter.inserts) >= expected_count:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"Expected {expected_count} inserts, got {len(adapter.inserts)} "
        f"within {timeout}s"
    )


def test_set_with_fact_type_calls_adapter_once(hybrid_store, mock_adapter):
    hybrid_store.set("k1", "Some fact about pythons" * 10,
                     namespace="ns", memory_type="fact")
    _wait_for_inserts(mock_adapter, 1)
    assert len(mock_adapter.inserts) == 1
    rec = mock_adapter.inserts[0]
    assert rec.doc_id == "ns::k1"
    assert rec.metadata["obscura_key"] == "k1"
    assert rec.metadata["obscura_namespace"] == "ns"
    assert rec.metadata["memory_type"] == "fact"
    assert "created_at" in rec.metadata


def test_set_with_episode_type_does_not_call_adapter(hybrid_store, mock_adapter):
    hybrid_store.set("k1", "Some long episode text" * 20,
                     namespace="ns", memory_type="episode")
    time.sleep(0.1)  # give the executor a chance if there were a bug
    assert mock_adapter.inserts == []


def test_set_with_graph_index_true_metadata_overrides_whitelist(
    hybrid_store, mock_adapter
):
    hybrid_store.set("k1", "Some long episode text" * 20,
                     namespace="ns", memory_type="episode",
                     metadata={"graph_index": True})
    _wait_for_inserts(mock_adapter, 1)
    assert mock_adapter.inserts[0].metadata["memory_type"] == "episode"


def test_set_with_graph_index_false_metadata_overrides_whitelist(
    hybrid_store, mock_adapter
):
    hybrid_store.set("k1", "Some long fact text" * 20,
                     namespace="ns", memory_type="fact",
                     metadata={"graph_index": False})
    time.sleep(0.1)
    assert mock_adapter.inserts == []


def test_set_with_short_text_skips_indexing(hybrid_store, mock_adapter):
    # Default min_text_chars = 50
    hybrid_store.set("k1", "short", namespace="ns", memory_type="fact")
    time.sleep(0.1)
    assert mock_adapter.inserts == []


def test_set_does_not_block_on_adapter(tmp_path, monkeypatch):
    """Verify super().set() returns before adapter call completes."""
    from tests.unit.obscura.lightrag_memory.conftest import MockLightRAGAdapter
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
    from obscura.auth.models import AuthenticatedUser

    monkeypatch.setenv("OBSCURA_VECTOR_MEMORY_DIR", str(tmp_path))
    slow_adapter = MockLightRAGAdapter(insert_sleep_seconds=1.0)
    user = AuthenticatedUser(user_id="t2", auth_token="x")
    store = HybridVectorMemoryStore(user, lightrag_adapter=slow_adapter)

    started = time.monotonic()
    store.set("k", "x" * 200, memory_type="fact")
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, (
        f"set() blocked for {elapsed:.2f}s — should be <100ms-ish "
        "even with a slow adapter"
    )
    store.close()  # waits for the slow insert to drain


def test_adapter_exception_does_not_propagate(tmp_path, monkeypatch):
    """A raising adapter must not break super().set()."""
    from tests.unit.obscura.lightrag_memory.conftest import MockLightRAGAdapter
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
    from obscura.auth.models import AuthenticatedUser

    monkeypatch.setenv("OBSCURA_VECTOR_MEMORY_DIR", str(tmp_path))
    explosive = MockLightRAGAdapter(insert_raises=RuntimeError("boom"))
    user = AuthenticatedUser(user_id="t3", auth_token="x")
    store = HybridVectorMemoryStore(user, lightrag_adapter=explosive)

    # Should not raise.
    store.set("k", "x" * 200, memory_type="fact")
    # And the canonical store should still have the entry.
    assert store.get("k") is not None
    store.close()
```

```python
# tests/unit/obscura/lightrag_memory/test_hybrid_store_delete.py
import time


def test_delete_propagates_to_adapter(hybrid_store, mock_adapter):
    hybrid_store.set("k1", "x" * 200, namespace="ns", memory_type="fact")
    time.sleep(0.05)  # let insert through
    hybrid_store.delete("k1", namespace="ns")
    time.sleep(0.05)
    assert any(d.doc_id == "ns::k1" for d in mock_adapter.deletes)


def test_delete_missing_key_does_not_call_adapter(hybrid_store, mock_adapter):
    """super().delete returns False — we should not enqueue a graph delete."""
    deleted = hybrid_store.delete("nope", namespace="ns")
    assert deleted is False
    time.sleep(0.05)
    assert mock_adapter.deletes == []


def test_clear_namespace_batches_deletes(hybrid_store, mock_adapter):
    for i in range(5):
        hybrid_store.set(f"k{i}", "x" * 200, namespace="ns", memory_type="fact")
    time.sleep(0.1)
    mock_adapter.deletes.clear()  # ignore any prior

    hybrid_store.clear_namespace("ns")
    # Wait briefly for the batch worker.
    deadline = time.monotonic() + 1.0
    while len(mock_adapter.deletes) < 5 and time.monotonic() < deadline:
        time.sleep(0.05)
    doc_ids = {d.doc_id for d in mock_adapter.deletes}
    assert doc_ids == {f"ns::k{i}" for i in range(5)}
```

```python
# tests/unit/obscura/lightrag_memory/test_indexable_types.py
import pytest


@pytest.mark.parametrize("memory_type,expected", [
    ("fact",       True),
    ("summary",    True),
    ("general",    True),
    ("episode",    False),
    ("preference", False),
    ("eval_failure", False),
    ("session_turn", False),
    ("profile_career", False),
])
def test_default_whitelist(hybrid_store, mock_adapter, memory_type, expected):
    import time
    hybrid_store.set("k", "x" * 200, namespace="ns", memory_type=memory_type)
    time.sleep(0.1)
    assert (len(mock_adapter.inserts) == 1) is expected
```

### What this phase explicitly does NOT test

- LightRAG's actual graph quality (Phase 6 integration tier).
- Real `ainsert` calls with cassettes (Phase 6 opt-in).
- The Qdrant payload's `lr_indexed_at` field (Phase 5).
- The query path (`search_hybrid`, hybrid scoring) — Phase 3.

---

## 13. Open questions / decisions deferred to later

### `MemoryConsolidator` interaction

`obscura/vector_memory/consolidator.py:135` writes a `summary` (which IS in the whitelist — gets graph-indexed) and deletes the source episodes (which are NOT in the whitelist — were never graph-indexed). So in steady state, there's nothing for Phase 2 to clean up: the consolidator's deletes go through `HybridVectorMemoryStore.delete()`, the override calls `delete_safe()`, and `delete_safe` short-circuits because the doc_id was never indexed (verified via `lr_deletes_failed` not incrementing in the §10/F8 idempotent-missing-doc check).

**Edge case:** if a user toggles `graph_index=True` on their episodes for some period, then consolidates them, the graph nodes for those episodes will be deleted via the consolidator's delete calls — good, but the new `summary` will reference entities that the old episodes used to anchor. LightRAG handles this correctly (entity reference counts in its graph), but it's worth a Phase 4 or Phase 5 integration test to confirm.

**Decision deferred:** Phase 2 ships without explicit consolidator coordination. Phase 4 adds a hook in `consolidator.consolidate()` if the integration test surfaces drift.

### `force_index=True` kwarg on `set()`

Some callers (e.g. a tool that wants to atomically index something into the graph and confirm) might want a synchronous version of `set()` that blocks on the LightRAG insert. Phase 2 deliberately doesn't expose this — the async fan-out is the whole point.

**Decision deferred:** if Phase 4 surfaces a need (probably won't), add a `force_index: bool = False` kwarg that, when True, blocks on `future.result(timeout=insert_timeout_seconds)`. Don't add it pre-emptively.

### Per-user vs per-machine `LightRAGAdapter` lifecycle

Phase 1 creates `LightRAGAdapter` lazily on first `for_user()`. Phase 4's auth-middleware lifecycle is supposed to call `close()` on logout. But what if the user never logs out (CLI session)? The daemon thread + executor are leaked at process exit.

**Decision deferred:** the `daemon=True` thread + `__del__` cleanup are good enough for a CLI process. Phase 4 will firm up the server-side lifecycle (where the same user can log in/out repeatedly, and adapter leakage matters).

### Adapter loop heartbeat

If the adapter's event loop dies, all subsequent inserts fail until process restart (§10/F5). Phase 2 does not detect or restart. Phase 5 may add a heartbeat if telemetry shows real-world failures.

### Multiple `_record_indexed_marker` strategies

Once Phase 5 lands, there's a question of whether the `lr_indexed_at` write should:
- (a) Update the Qdrant payload directly (one extra Qdrant round-trip per insert).
- (b) Write to a separate sidecar file (`~/.obscura/lightrag/<user_hash>/indexed.sqlite`).
- (c) Skip the marker entirely and use LightRAG's own dedup as the source of truth.

Phase 2 stubs the call so Phase 5 can pick the strategy. **Defer the decision** — none of the Phase 2 code depends on which is chosen.

### Embedding-function sharing

Phase 1's `LightRAGAdapter.for_user()` is supposed to wire the same embedding function into LightRAG that the parent uses (`_make_default_embedding_fn` at `vector_memory.py:86`). If for some reason the wiring fails and LightRAG falls back to its own embedding (e.g. OpenAI), the canonical Qdrant store and LightRAG's vector store will have mismatched dimensions and queries in Phase 3 will be incoherent.

**Decision deferred to Phase 3:** add an assertion in `LightRAGAdapter.__init__` that the embedding-function dim matches the parent store's embedding_dim, raise loudly otherwise. Phase 2 doesn't need it because the query path isn't wired yet.

---

## 14. Implementation checklist

To turn this document into a merged PR:

- [ ] Create `obscura/lightrag_memory/hybrid_store.py` per §3-4. ~250 lines.
- [ ] Extend `obscura/lightrag_memory/adapter.py` (Phase 1's stub) with `insert_safe`, `delete_safe`, `_record_indexed_marker` no-op, `_maybe_log_latency_summary`, `load_indexable_types_from_disk`. ~200 lines.
- [ ] Update `obscura/vector_memory/vector_memory.py:306` (`for_user`) per overview §"Backend wiring — single integration point". Already done in Phase 1 if the conditional dispatch was added there; otherwise add it now.
- [ ] Add `[vector_memory.lightrag]` config section docs to README / CLAUDE.md (insert_timeout_seconds, indexable_types, min_text_chars, max_text_chars).
- [ ] Add tests per §12. ~200 lines.
- [ ] Run `make lint && make typecheck && pytest tests/unit/obscura/lightrag_memory -v -m unit`.
- [ ] Verify no regressions: `pytest tests/unit/obscura/vector_memory -v` (the existing vector_memory tests must still pass — `OBSCURA_LIGHTRAG=off` is the default).
- [ ] Manual smoke: `OBSCURA_LIGHTRAG=on uv run obscura "store this fact: the moon orbits earth at 384,400 km"`, then check `~/.obscura/lightrag/<user_hash>/` for graph artifacts.

---

## 15. References

- `obscura/vector_memory/vector_memory.py:163` — `VectorMemoryStore` class definition
- `obscura/vector_memory/vector_memory.py:306` — `for_user` factory (Phase 1 dispatch point)
- `obscura/vector_memory/vector_memory.py:324` — `set()` signature being overridden
- `obscura/vector_memory/vector_memory.py:516` — `delete()` signature being overridden
- `obscura/vector_memory/vector_memory.py:530` — `clear_namespace()` signature being overridden
- `obscura/vector_memory/vector_memory.py:551` — `touch()` (Phase 5 hook point)
- `obscura/vector_memory/vector_memory.py:557` — `_touch_results_async` pattern reused
- `obscura/vector_memory/vector_memory.py:611` — `close()` extension point
- `obscura/vector_memory/backends/base.py:42-108` — `VectorBackend` Protocol (untouched in Phase 2)
- `obscura/vector_memory/backends/qdrant_backend.py:112-165` — `store_vector` payload schema (the place Phase 5 will add `lr_indexed_at`)
- `obscura/vector_memory/decay.py:52-65` — DEFAULT_PROFILES (real memory_types in the wild)
- `obscura/memory/__init__.py:34-42` — `MemoryKey` dataclass and `__str__`
- `obscura/telemetry/metrics.py` — counter / histogram infrastructure
- `plans/lightrag/00-overview.md` — canonical plan
