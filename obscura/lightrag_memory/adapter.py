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
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    from lightrag import LightRAG  # type: ignore[import-not-found]
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

_DEFAULT_INSERT_TIMEOUT_SECONDS = 60.0
_DEFAULT_DELETE_TIMEOUT_SECONDS = 30.0
_DEFAULT_INDEXABLE_TYPES: frozenset[str] = frozenset({"fact", "summary", "general"})


@dataclass(frozen=True)
class GraphHit:
    """A single retrieval hit from LightRAG, before Obscura hydration.

    Phase 1 returns an empty list from :meth:`LightRAGAdapter.aquery` —
    Phase 3 will populate this from LightRAG's actual response shape.
    Documented here so the read path's downstream consumers (Phase 3)
    can be type-checked against this contract.
    """

    namespace: str
    key: str
    vector_sim: float
    graph_relevance: float
    text: str
    metadata: dict[str, Any]


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


def load_indexable_types_from_disk() -> frozenset[str]:
    """Load ``[vector_memory.lightrag] indexable_types`` from config.toml."""
    try:
        from obscura.core.config_io import try_load_config

        cfg = try_load_config(Path.home() / ".obscura" / "config.toml") or {}
        section = cfg.get("vector_memory", {}).get("lightrag", {})
        raw: Any = section.get("indexable_types")
        if raw is None:
            return _DEFAULT_INDEXABLE_TYPES
        if not isinstance(raw, list):
            _log.warning(
                "vector_memory.lightrag.indexable_types must be a list of "
                "strings — got %r, falling back to defaults",
                type(raw).__name__,
            )
            return _DEFAULT_INDEXABLE_TYPES
        items: list[str] = [str(x) for x in raw]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        return frozenset(items)
    except Exception:
        _log.debug("Could not load indexable_types from disk", exc_info=True)
        return _DEFAULT_INDEXABLE_TYPES


class LightRAGAdapter:
    """Per-user LightRAG instance + a dedicated event-loop thread.

    Singleton-per-user-id, mirroring :class:`VectorMemoryStore`'s pattern.
    Construction is fail-safe: if anything goes wrong (Qdrant unreachable,
    working_dir read-only, embedding-fn raises) the adapter raises and the
    caller in :func:`_lightrag_enabled` logs + falls back.
    """

    _instances: dict[str, LightRAGAdapter] = {}
    _lock = threading.Lock()

    indexable_types: frozenset[str] = _DEFAULT_INDEXABLE_TYPES

    def __init__(
        self,
        user: AuthenticatedUser,
        embedding_fn: Callable[[str], list[float]],
        *,
        indexable_types: frozenset[str] | None = None,
        insert_timeout_seconds: float = _DEFAULT_INSERT_TIMEOUT_SECONDS,
        delete_timeout_seconds: float = _DEFAULT_DELETE_TIMEOUT_SECONDS,
    ) -> None:
        self.user = user
        self.user_id = user.user_id
        self._embedding_fn = embedding_fn
        self.indexable_types = (
            indexable_types
            if indexable_types is not None
            else load_indexable_types_from_disk()
        )
        self._insert_timeout = insert_timeout_seconds
        self._delete_timeout = delete_timeout_seconds
        self._closed = False

        self._embedding_dim = len(embedding_fn("test"))

        self._working_dir = _working_dir(user.user_id)
        self._working_dir.mkdir(parents=True, exist_ok=True)

        self._collection = _qdrant_collection_name(user.user_id)

        self._loop, self._loop_thread = _start_loop_thread(
            name=f"lr-loop-{_user_hash(user.user_id)[:8]}",
        )

        self._lightrag: Any = self._build_lightrag()

        self._latency_samples: list[float] = []
        self._latency_lock = threading.Lock()
        self._latency_log_every = 100

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

        meta = metadata or {}
        text_len = len(text)
        memory_type = meta.get("memory_type", "general")
        _metric_inc("lr_inserts_submitted", memory_type=memory_type)
        started = time.monotonic()

        try:
            coro = self._ainsert(doc_id, text, meta)
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
        except Exception as exc:  # noqa: BLE001
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

    async def aquery(
        self,
        query: str,
        mode: str = "hybrid",
        top_k: int = 20,
    ) -> list[GraphHit]:
        """Run a hybrid retrieval against the LightRAG instance.

        **Phase 1 placeholder.** Returns ``[]`` unconditionally. Phase 3
        will:

        1. Build a :class:`QueryParam` with ``mode`` and ``top_k`` and
           ``only_need_context=True`` to suppress LLM answer synthesis.
        2. Call ``await self._lightrag.aquery(query, param=param)``.
        3. Parse the response into a list of :class:`GraphHit` carrying
           ``obscura_namespace`` / ``obscura_key`` from the metadata
           that Phase 2 stamped into the doc.
        """
        _log.debug(
            "LightRAGAdapter.aquery is a Phase-1 placeholder; returning []. "
            "query=%r mode=%r top_k=%d",
            query[:80],
            mode,
            top_k,
        )
        return []

    def shutdown(self) -> None:
        """Stop the dedicated event loop. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=2.0)

    def _build_lightrag(self) -> Any:
        """Construct the LightRAG instance bound to this user's storage.

        Wraps the user-supplied embedding_fn into LightRAG's expected
        ``embedding_func`` shape (``EmbeddingFunc`` with ``func`` async).
        """
        from lightrag.utils import EmbeddingFunc  # type: ignore[import-not-found]

        async def _async_embed(texts: list[str]) -> list[list[float]]:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: [self._embedding_fn(t) for t in texts],
            )

        embedding_func = EmbeddingFunc(  # pyright: ignore[reportUnknownVariableType]
            embedding_dim=self._embedding_dim,
            max_token_size=8192,
            func=_async_embed,
        )

        qdrant_kwargs = _qdrant_kwargs()
        url = qdrant_kwargs.get("url")
        api_key = qdrant_kwargs.get("api_key")
        if url:
            os.environ.setdefault("QDRANT_URL", url)
        if api_key:
            os.environ.setdefault("QDRANT_API_KEY", api_key)

        return LightRAG(  # pyright: ignore[reportUnknownVariableType]
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
        """Async wrapper around LightRAG's ``ainsert``."""
        await self._lightrag.ainsert(
            input=text,
            ids=[doc_id],
            file_paths=[metadata.get("source", "obscura")],
        )

    async def _adelete(self, doc_id: str) -> None:
        """Async wrapper around LightRAG's delete-by-doc-id."""
        await self._lightrag.adelete_by_doc_id(doc_id)

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


def _start_loop_thread(
    *, name: str
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


def _metric_inc(name: str, **labels: str) -> None:
    """Increment a counter, falling back to stdlib logging when OTel is absent.

    The phase plan calls out OTel-backed counters; the existing
    :mod:`obscura.telemetry.metrics` exposes pre-declared metric handles
    rather than a dynamic ``get_meter`` API, so we log a structured DEBUG
    line and rely on Phase 3+ to upgrade to a typed handle when LightRAG-
    specific metrics are added to :class:`ObscuraMetrics`.
    """
    try:
        if labels:
            _log.debug("lr_metric: %s %s", name, labels)
        else:
            _log.debug("lr_metric: %s", name)
    except Exception:
        pass


def _metric_record(name: str, value: float) -> None:
    """Record a histogram sample as a structured DEBUG log line."""
    try:
        _log.debug("lr_metric: %s value=%.4f", name, value)
    except Exception:
        pass
