"""obscura.lightrag_memory.adapter — Per-user LightRAG instance owner.

Bridges Obscura's sync write path to LightRAG's async API by running a
single dedicated event loop in a daemon thread per user. ``insert_safe`` /
``delete_safe`` schedule coroutines onto that loop and return without
waiting; ``aquery`` is async-native for the read path.

This module imports ``lightrag`` at top level. Anything that imports it
must be gated by ``_lightrag_enabled()`` from the package ``__init__``,
or it must be prepared to catch ``ImportError``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from concurrent.futures import Future, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    from lightrag import LightRAG, QueryParam  # noqa: F401  # LightRAG used by tests/type-checking
except ImportError as exc:  # pragma: no cover
    msg = (
        "obscura.lightrag_memory.adapter requires the 'lightrag' optional "
        "extra. Install with: uv sync --extra lightrag "
        "(or: pip install obscura[lightrag])"
    )
    raise ImportError(msg) from exc

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.auth.models import AuthenticatedUser

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GraphHit — placeholder return type for aquery (Phase 3 will populate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphHit:
    """A single retrieval hit from LightRAG, before Obscura hydration.

    The adapter populates this from LightRAG's per-mode response shape; the
    raw ``graph_relevance`` is mode-dependent and is normalized (min-max)
    inside :meth:`HybridVectorMemoryStore.search_hybrid` before scoring.
    """

    namespace: str
    key: str
    vector_sim: float
    graph_relevance: float
    text_excerpt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def graph_score(self) -> float:
        """Back-compat alias matching the Phase-3 plan field name."""
        return self.graph_relevance

    @property
    def text(self) -> str:
        """Back-compat alias for ``text_excerpt``."""
        return self.text_excerpt


_DEFAULT_INSERT_TIMEOUT_SECONDS = 60.0
_DEFAULT_DELETE_TIMEOUT_SECONDS = 30.0
_DEFAULT_INDEXABLE_TYPES: frozenset[str] = frozenset({"fact", "summary", "general"})


def load_indexable_types_from_disk() -> frozenset[str]:
    """Load ``[vector_memory.lightrag] indexable_types`` from config.toml."""
    try:
        from obscura.core.config_io import try_load_config

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


def _load_timeout_seconds(key: str, default: float) -> float:
    """Read a per-call timeout from config.toml."""
    try:
        from obscura.core.config_io import try_load_config

        cfg = try_load_config(Path.home() / ".obscura" / "config.toml") or {}
        section = cfg.get("vector_memory", {}).get("lightrag", {})
        return float(section.get(key, default))
    except Exception:
        return default


def _metric_inc(name: str, **labels: str) -> None:
    """Increment a counter via the telemetry meter, no-op if OTel absent."""
    try:
        from obscura.telemetry.metrics import get_meter

        meter = get_meter()
        ctr = meter.create_counter(name)
        ctr.add(1, attributes=labels)
    except Exception:
        pass


def _metric_record(name: str, value: float) -> None:
    """Record a value into a histogram, no-op if OTel absent."""
    try:
        from obscura.telemetry.metrics import get_meter

        meter = get_meter()
        h = meter.create_histogram(name, unit="s")
        h.record(value)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# LightRAGAdapter
# ---------------------------------------------------------------------------


def _user_hash(user_id: str) -> str:
    """16-char hex digest of the user_id, matching ``vector_memory.py:297``."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


def _working_dir(user_id: str) -> Path:
    """Per-user working dir. Lives next to ``~/.obscura/qdrant/``."""
    base = Path(
        os.environ.get(
            "OBSCURA_LIGHTRAG_WORKING_DIR_BASE",
            Path.home() / ".obscura" / "lightrag",
        ),
    )
    return base / _user_hash(user_id)


def _qdrant_collection_name(user_id: str) -> str:
    """LightRAG's Qdrant collection — namespaced separately from the main store.

    Existing store: ``user_<hash>`` (qdrant_backend.py:61).
    LightRAG store: ``obscura_lightrag_<hash>``.

    Keeping them in distinct collections means a botched LightRAG ingest
    can never corrupt the canonical vector memory.
    """
    return f"obscura_lightrag_{_user_hash(user_id)}"


def _qdrant_kwargs() -> dict[str, Any]:
    """Read Qdrant connection details from existing envvars.

    Mirrors the env-reading pattern at ``vector_memory.py:251-254`` so the
    adapter shares whatever Qdrant the user already configured.
    """
    mode = os.environ.get("OBSCURA_QDRANT_MODE", "local").lower()
    if mode == "memory":
        return {"location": ":memory:"}
    if mode == "cloud":
        return {
            "url": os.environ.get("OBSCURA_QDRANT_URL")
            or os.environ.get("QDRANT_URL", "http://localhost:6333"),
            "api_key": os.environ.get("OBSCURA_QDRANT_API_KEY")
            or os.environ.get("QDRANT_API_KEY"),
        }
    return {
        "path": str(
            Path(
                os.environ.get(
                    "OBSCURA_QDRANT_PATH",
                    Path.home() / ".obscura" / "qdrant",
                ),
            ),
        ),
    }


class LightRAGAdapter:
    """Per-user LightRAG instance + a dedicated event-loop thread.

    Singleton-per-user-id, mirroring :class:`VectorMemoryStore`'s pattern.
    Construction is fail-safe: if anything goes wrong (Qdrant unreachable,
    working_dir read-only, embedding-fn raises) the adapter raises and the
    caller in :func:`_lightrag_enabled` logs + falls back.
    """

    _instances: dict[str, LightRAGAdapter] = {}
    _lock = threading.Lock()

    def __init__(
        self,
        user: AuthenticatedUser,
        embedding_fn: Callable[[str], list[float]],
        *,
        indexable_types: frozenset[str] | None = None,
        insert_timeout_seconds: float | None = None,
        delete_timeout_seconds: float | None = None,
    ) -> None:
        self.user = user
        self.user_id = user.user_id
        self._embedding_fn = embedding_fn

        self._embedding_dim = len(embedding_fn("test"))

        self._working_dir = _working_dir(user.user_id)
        self._working_dir.mkdir(parents=True, exist_ok=True)

        self._collection = _qdrant_collection_name(user.user_id)

        self.indexable_types = (
            indexable_types
            if indexable_types is not None
            else load_indexable_types_from_disk()
        )
        self._insert_timeout = (
            insert_timeout_seconds
            if insert_timeout_seconds is not None
            else _load_timeout_seconds(
                "insert_timeout_seconds", _DEFAULT_INSERT_TIMEOUT_SECONDS
            )
        )
        self._delete_timeout = (
            delete_timeout_seconds
            if delete_timeout_seconds is not None
            else _load_timeout_seconds(
                "delete_timeout_seconds", _DEFAULT_DELETE_TIMEOUT_SECONDS
            )
        )

        # Bring up the dedicated loop BEFORE constructing LightRAG, because
        # LightRAG's __init__ may schedule coroutines.
        self._loop, self._loop_thread = _start_loop_thread(
            name=f"lr-loop-{_user_hash(user.user_id)[:8]}",
        )

        self._lightrag = self._build_lightrag()
        self._closed = False

        self._latency_samples: list[float] = []
        self._latency_lock = threading.Lock()
        self._latency_log_every = 100

    # -- public API ---------------------------------------------------------

    @classmethod
    def for_user(
        cls,
        user: AuthenticatedUser,
        embedding_fn: Callable[[str], list[float]],
    ) -> LightRAGAdapter:
        """Get-or-create the per-user adapter."""
        with cls._lock:
            if user.user_id not in cls._instances:
                cls._instances[user.user_id] = cls(user, embedding_fn)
            return cls._instances[user.user_id]

    @classmethod
    def reset_instances(cls) -> None:
        """Clear singleton cache. For testing only."""
        with cls._lock:
            for adapter in cls._instances.values():
                adapter.shutdown()
            cls._instances.clear()

    def insert_safe(
        self,
        doc_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Run LightRAG ``ainsert`` synchronously from a worker thread.

        Bridges to the adapter's dedicated event loop. Catches every
        exception and logs at WARNING; callers must not depend on the
        return value.
        """
        if self._closed:
            _log.debug("lr_ingest: adapter closed, skip insert for %s", doc_id)
            return

        md = metadata or {}
        text_len = len(text)
        memory_type = md.get("memory_type", "general")
        _metric_inc("lr_inserts_submitted", memory_type=memory_type)
        started = time.monotonic()

        try:
            coro = self._ainsert(doc_id, text, md)
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
        except Exception as exc:
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

        elapsed = time.monotonic() - started
        _metric_inc("lr_inserts_succeeded", memory_type=memory_type)
        _metric_record("lr_insert_duration_seconds", elapsed)
        self._maybe_log_latency_summary(elapsed)
        _log.info(
            "lr_ingest: insert ok (doc=%s, text_len=%d, memory_type=%s, elapsed=%.2fs)",
            doc_id,
            text_len,
            memory_type,
            elapsed,
        )
        self._record_indexed_marker(doc_id, started)

    def delete_safe(self, doc_id: str) -> None:
        """Run LightRAG ``adelete_by_doc_id`` synchronously from a worker thread.

        Idempotent: deleting an unknown doc_id is a no-op.
        """
        if self._closed:
            _log.debug("lr_ingest: adapter closed, skip delete for %s", doc_id)
            return

        _metric_inc("lr_deletes_submitted")
        started = time.monotonic()

        try:
            coro = self._adelete(doc_id)
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
        except Exception as exc:
            _metric_inc("lr_deletes_failed", exc_type=type(exc).__name__)
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
        _log.debug("lr_ingest: delete ok (doc=%s, elapsed=%.2fs)", doc_id, elapsed)

    async def aquery(
        self,
        query: str,
        mode: str = "hybrid",
        top_k: int = 20,
        *,
        namespace: str | None = None,
        only_need_context: bool = True,
    ) -> list[GraphHit]:
        """Run a hybrid retrieval against the LightRAG instance.

        Issues ``aquery`` with ``only_need_context=True`` to suppress the
        natural-language answer-synthesis pass. Parses the heterogeneous
        response shape into a list of :class:`GraphHit` joined back to
        Obscura's canonical store via the ``obscura_namespace`` /
        ``obscura_key`` metadata stamped during ingest.

        Raises any exception from the underlying LightRAG call so the
        hybrid store can apply its fallback policy.
        """
        param = QueryParam(
            mode=mode,
            top_k=top_k,
            only_need_context=only_need_context,
        )
        raw = await self._lightrag.aquery(query, param=param)
        return _parse_aquery_response(raw, namespace=namespace)

    def shutdown(self) -> None:
        """Stop the dedicated event loop. Idempotent."""
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=2.0)

    def close(self) -> None:
        """Stop the dedicated event loop and mark closed. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self.shutdown()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """Expose the dedicated event loop for sync->async bridging."""
        return self._loop

    def _record_indexed_marker(self, doc_id: str, started: float) -> None:
        """Phase 2 stub for the lr_indexed_at marker.

        Phase 5 wires this through ``VectorBackend.update_metadata`` once
        the protocol method exists. The lazy on-touch + backfill paths
        depend on the marker; the canonical Phase-2 write path tolerates
        re-indexing because LightRAG's ``ainsert`` is idempotent on doc_id.
        """
        return

    def _maybe_log_latency_summary(self, sample: float) -> None:
        """Log p50/p99 every ``_latency_log_every`` successful inserts."""
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

    # -- internals ----------------------------------------------------------

    def _build_lightrag(self) -> Any:
        """Construct the LightRAG instance bound to this user's storage."""
        from lightrag import LightRAG as _LightRAG
        from lightrag.utils import EmbeddingFunc

        async def _async_embed(texts: list[str]) -> list[list[float]]:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: [self._embedding_fn(t) for t in texts],
            )

        embedding_func = EmbeddingFunc(
            embedding_dim=self._embedding_dim,
            max_token_size=8192,
            func=_async_embed,
        )

        kw = _qdrant_kwargs()
        os.environ.setdefault("QDRANT_URL", kw.get("url", "") or "")
        os.environ.setdefault("QDRANT_API_KEY", kw.get("api_key", "") or "")

        return _LightRAG(
            working_dir=str(self._working_dir),
            embedding_func=embedding_func,
            vector_storage="QdrantVectorDBStorage",
            graph_storage="NetworkXStorage",
            kv_storage="JsonKVStorage",
            doc_status_storage="JsonDocStatusStorage",
            namespace_prefix=self._collection,
        )

    async def _ainsert(
        self,
        doc_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> None:
        """Async wrapper around LightRAG's ``ainsert``. Phase 2 will polish."""
        await self._lightrag.ainsert(
            input=[text],
            ids=[doc_id],
            file_paths=[metadata.get("source", "obscura")],
        )

    async def _adelete(self, doc_id: str) -> None:
        """Async wrapper around LightRAG's delete-by-doc-id."""
        await self._lightrag.adelete_by_doc_id(doc_id)

    @staticmethod
    def _log_future_error(op: str, doc_id: str):
        """Done-callback factory: log + swallow exceptions from the future."""

        def _cb(fut: Future[Any]) -> None:
            try:
                fut.result()
            except Exception:
                _log.warning(
                    "LightRAG %s failed for doc_id=%s; vector store write was "
                    "unaffected.",
                    op,
                    doc_id,
                    exc_info=True,
                )

        return _cb


# ---------------------------------------------------------------------------
# aquery response parsing
# ---------------------------------------------------------------------------


def _parse_aquery_response(
    raw: Any,
    *,
    namespace: str | None,
) -> list[GraphHit]:
    """Coerce LightRAG's heterogeneous response into a list of :class:`GraphHit`.

    LightRAG's ``aquery(only_need_context=True)`` may return a string, a
    dict with ``chunks`` / ``entities`` / ``relations`` keys, or a list of
    dicts depending on mode and version. We parse defensively. Anything we
    cannot identify becomes an empty list rather than raising.
    """
    hits: list[GraphHit] = []
    if raw is None:
        return hits

    items: list[dict[str, Any]] = []
    if isinstance(raw, list):
        items = [x for x in raw if isinstance(x, dict)]
    elif isinstance(raw, dict):
        for k in ("chunks", "context_chunks", "results", "hits"):
            v = raw.get(k)
            if isinstance(v, list):
                items = [x for x in v if isinstance(x, dict)]
                break

    if not items:
        return hits

    for item in items:
        md = item.get("metadata") or {}
        ns = md.get("obscura_namespace") or item.get("namespace")
        key = md.get("obscura_key") or item.get("key") or item.get("id")
        if ns is None or key is None:
            continue
        if namespace is not None and ns != namespace:
            continue
        sim = (
            item.get("score") or item.get("vdb_score") or item.get("similarity") or 0.0
        )
        graph_score = (
            item.get("graph_score")
            or item.get("rerank_score")
            or item.get("relevance")
            or 0.0
        )
        try:
            sim_f = float(sim)
        except (TypeError, ValueError):
            sim_f = 0.0
        try:
            graph_f = float(graph_score)
        except (TypeError, ValueError):
            graph_f = 0.0
        hits.append(
            GraphHit(
                namespace=str(ns),
                key=str(key),
                vector_sim=sim_f,
                graph_relevance=graph_f,
                text_excerpt=str(item.get("content") or item.get("text") or "")[:200],
                metadata=md if isinstance(md, dict) else {},
            ),
        )
    return hits


# ---------------------------------------------------------------------------
# Dedicated-loop helper
# ---------------------------------------------------------------------------


def _start_loop_thread(
    *,
    name: str,
) -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Spin up a daemon thread running its own asyncio event loop.

    Returns the (loop, thread) pair. The loop is ready to accept coroutines
    via :func:`asyncio.run_coroutine_threadsafe` once this function returns
    (we wait on a one-shot ``threading.Event`` to confirm the loop is up).

    The thread is a daemon so a crash on shutdown won't hang the host.
    """
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        try:
            loop.run_forever()
        finally:
            loop.close()

    thread = threading.Thread(target=_run, name=name, daemon=True)
    thread.start()
    ready.wait(timeout=5.0)
    if not ready.is_set():
        msg = f"asyncio loop thread {name!r} failed to start within 5s"
        raise RuntimeError(msg)
    return loop, thread
