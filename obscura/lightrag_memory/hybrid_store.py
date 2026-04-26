"""obscura.lightrag_memory.hybrid_store — Drop-in subclass of VectorMemoryStore.

Phase 2 ingest path: overrides ``set`` / ``delete`` / ``clear_namespace`` to
fan out async writes onto a per-user LightRAG instance via a bounded thread
pool.

Phase 3 query path: adds ``search_hybrid`` blending vector + graph + decay
+ usage signals, and ``_touch_and_count_async`` to finally wire usage
tracking that the base class never connected.

The canonical vector store remains the source of truth: a LightRAG fan-out
or query failure is logged but never propagates to the caller — Phase 3
falls back to ``search_reranked`` whenever the graph path can't deliver.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
)
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from obscura.lightrag_memory.scoring import (
    HybridWeights,
    hybrid_score,
    load_hybrid_weights_from_disk,
)
from obscura.memory import MemoryKey
from obscura.vector_memory import VectorMemoryStore
from obscura.vector_memory.decay import compute_decay

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.auth.models import AuthenticatedUser
    from obscura.lightrag_memory.adapter import LightRAGAdapter
    from obscura.memory.events import EventSink
    from obscura.vector_memory.backends import VectorBackend, VectorEntry
    from obscura.vector_memory.decay import DecayConfig

logger = logging.getLogger(__name__)

_DEFAULT_MIN_TEXT_CHARS = 50
_DEFAULT_MAX_TEXT_CHARS = 100_000


def _metric_inc(name: str, **labels: str) -> None:
    """Increment a counter via the telemetry meter, no-op if OTel absent."""
    try:
        from obscura.telemetry.metrics import get_meter

        meter = get_meter()
        ctr = meter.create_counter(name)
        ctr.add(1, attributes=labels)
    except Exception:
        pass


def _log_future_failure(future: Future[Any], doc_id: str) -> None:
    """Log any exception that escaped insert_safe / delete_safe."""
    try:
        exc = future.exception()
    except Exception:
        return
    if exc is not None:
        logger.warning(
            "lr_ingest: unexpected exception escaped safe call (doc=%s): %s",
            doc_id,
            exc,
        )


class HybridVectorMemoryStore(VectorMemoryStore):
    """Drop-in subclass that fans writes out to LightRAG.

    Inherits the entire existing API; overrides only set/delete/clear_namespace
    and adds close(). The query path (search_similar, search_reranked) is
    unchanged in Phase 2; Phase 3 adds search_hybrid.
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
        max_text_chars: int = _DEFAULT_MAX_TEXT_CHARS,
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
        self._max_text_chars = max_text_chars
        self._ingest_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix=f"lr-ingest-{user.user_id[:8]}",
        )
        self._closed = False

    @staticmethod
    def _make_doc_id(mkey: MemoryKey) -> str:
        """Stable encoding of (namespace, key) for LightRAG doc_id.

        Uses ``::`` as the separator so it remains unambiguous when a
        namespace itself contains a colon (e.g. ``project:jira``).
        """
        return f"{mkey.namespace}::{mkey.key}"

    def _should_index(
        self,
        memory_type: str,
        metadata: dict[str, Any] | None,
        text: str,
        namespace: str,
        key: str,
    ) -> bool:
        """Decide whether a write should be graph-indexed."""
        if metadata is not None and "graph_index" in metadata:
            opt = bool(metadata["graph_index"])
            if not opt:
                _metric_inc("lr_inserts_skipped_filter", reason="opt_out")
                return False
        else:
            if memory_type not in self._lr.indexable_types:
                _metric_inc("lr_inserts_skipped_filter", reason="not_whitelisted")
                return False

        text_len = len(text)
        if text_len < self._min_text_chars:
            _metric_inc("lr_inserts_skipped_filter", reason="short_text")
            return False
        if text_len > self._max_text_chars:
            _metric_inc("lr_inserts_skipped_filter", reason="oversized")
            logger.warning(
                "lr_ingest: text too long for graph indexing "
                "(len=%d, limit=%d, doc=%s::%s) — chunk stored in canonical "
                "store only",
                text_len,
                self._max_text_chars,
                namespace,
                key,
            )
            return False

        return True

    def set(  # type: ignore[override]
        self,
        key: str | MemoryKey,
        text: str,
        metadata: dict[str, Any] | None = None,
        namespace: str = "default",
        ttl: timedelta | None = None,
        memory_type: str = "general",
    ) -> None:
        """Synchronously write to canonical store, asynchronously fan out to LightRAG."""
        if isinstance(key, str):
            mkey = MemoryKey(namespace=namespace, key=key)
        else:
            mkey = key
            namespace = mkey.namespace

        super().set(
            key=mkey,
            text=text,
            metadata=metadata,
            namespace=namespace,
            ttl=ttl,
            memory_type=memory_type,
        )

        if not self._should_index(
            memory_type, metadata, text, mkey.namespace, mkey.key
        ):
            return

        doc_id = self._make_doc_id(mkey)
        lr_metadata: dict[str, Any] = {
            **(metadata or {}),
            "obscura_key": mkey.key,
            "obscura_namespace": mkey.namespace,
            "memory_type": memory_type,
            "created_at": datetime.now(UTC).isoformat(),
        }

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
            logger.warning(
                "lr_ingest: executor closed, skipping submission for %s",
                doc_id,
            )

    def delete(self, key: str | MemoryKey, namespace: str = "default") -> bool:  # type: ignore[override]
        """Synchronously delete from canonical store, asynchronously delete from graph."""
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
                logger.warning(
                    "lr_ingest: executor closed, skipping delete for %s",
                    doc_id,
                )

        return existed

    def clear_namespace(self, namespace: str) -> int:
        """Wipe a namespace from both canonical store and LightRAG."""
        keys_to_delete: list[MemoryKey] = []
        if not self._closed:
            try:
                keys_to_delete = list(self.list_keys(namespace=namespace))
            except Exception:
                logger.debug(
                    "lr_ingest: list_keys failed before clear_namespace, "
                    "graph may end up with orphans",
                    exc_info=True,
                )

        cleared = super().clear_namespace(namespace)

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
                logger.warning(
                    "lr_ingest: executor closed during clear_namespace(%s), "
                    "%d graph entries may be orphaned",
                    namespace,
                    len(doc_ids),
                )

        return cleared

    def _delete_batch_safe(self, doc_ids: list[str]) -> None:
        """Sequentially delete a batch of doc_ids from LightRAG."""
        for doc_id in doc_ids:
            try:
                self._lr.delete_safe(doc_id)
            except Exception:
                logger.warning(
                    "lr_ingest: delete failed during batch (doc=%s)",
                    doc_id,
                    exc_info=True,
                )

    def _resolve_weights(self, weights: HybridWeights | None) -> HybridWeights:
        if weights is not None:
            return weights
        if getattr(self, "_cached_weights", None) is None:
            self._cached_weights = load_hybrid_weights_from_disk()
        return self._cached_weights

    def _lr_default_timeout_ms(self) -> int:
        """Default query timeout (ms) from env > config.toml > 400."""
        import os as _os

        env_val = _os.environ.get("OBSCURA_LIGHTRAG_TIMEOUT_MS")
        if env_val:
            try:
                return int(env_val)
            except ValueError:
                pass
        try:
            from obscura.core.config_io import try_load_config
            from pathlib import Path as _Path

            cfg = try_load_config(_Path.home() / ".obscura" / "config.toml") or {}
            return int(
                cfg.get("vector_memory", {})
                .get("lightrag", {})
                .get("query_timeout_ms", 400)
            )
        except Exception:
            return 400

    def _emit_metric(self, name: str, value: int = 1, **tags: Any) -> None:
        """Best-effort metric emit; folds into module-level _metric_inc."""
        try:
            _metric_inc(f"vector_memory.lightrag.{name}", **tags)
        except Exception:
            logger.debug("metric emit failed: %s", name, exc_info=True)
        if value not in (0, 1):
            logger.debug("metric %s reported value=%d (counter is unit)", name, value)

    def _run_aquery_blocking(
        self,
        *,
        query: str,
        namespace: str | None,
        mode: str,
        top_k: int,
        timeout_ms: int | None,
    ) -> list[Any]:
        """Blocking wrapper around the adapter's async ``aquery`` with timeout."""
        coro = self._lr.aquery(
            query=query,
            mode=mode,
            top_k=top_k,
            namespace=namespace,
            only_need_context=True,
        )
        timeout_s = (timeout_ms / 1000.0) if timeout_ms else None
        future = asyncio.run_coroutine_threadsafe(coro, self._lr.loop)
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeoutError:
            future.cancel()
            raise asyncio.TimeoutError("LightRAG aquery timed out")

    def _fallback_to_reranked(
        self,
        query: str,
        namespace: str | None,
        top_k: int,
        memory_types: list[str] | None,
        *,
        reason: str,
    ) -> list[VectorEntry]:
        """Vector-only fallback path with hybrid-frame final_score."""
        self._emit_metric("hybrid_fallback", 1, reason=reason)
        results = super().search_reranked(
            query=query,
            namespace=namespace,
            top_k=top_k,
            memory_types=memory_types,
        )
        weights = self._resolve_weights(None)
        for e in results:
            decay_mult = compute_decay(
                e.memory_type,
                e.created_at,
                e.accessed_at,
                self.decay_config,
            )
            usage_count = int(e.metadata.get("access_count") or 0)
            e.rerank_score = 0.0
            e.final_score = hybrid_score(
                vector_sim=max(0.0, min(1.0, e.score or 0.0)),
                graph_relevance=0.0,
                decay_multiplier=decay_mult,
                usage_count=usage_count,
                weights=weights,
            )
        results.sort(key=lambda x: x.final_score, reverse=True)
        self._touch_and_count_async(results)
        return results[:top_k]

    def search_hybrid(  # type: ignore[override]
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
    ) -> list[VectorEntry]:
        """Hybrid retrieval blending vector + graph + decay + usage.

        Falls back to ``super().search_reranked()`` on:
        - LightRAG empty result
        - LightRAG raises
        - LightRAG exceeds ``timeout_ms``
        - all hits drift (chunk in graph but absent from canonical store).

        Returns the same shape as ``search_reranked`` for caller compatibility.
        """
        if top_k <= 0:
            return []
        if first_stage_k <= 0:
            first_stage_k = max(top_k * 5, 20)

        t_start = time.monotonic()
        weights = self._resolve_weights(weights)
        if timeout_ms is None:
            timeout_ms = self._lr_default_timeout_ms()

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

        from obscura.vector_memory.backends.base import VectorEntry as _VEntry

        hydrated: list[tuple[_VEntry, float, float]] = []
        drift_count = 0
        for hit in lr_hits:
            if namespace is not None and hit.namespace != namespace:
                continue
            entry = self.backend.get_vector(MemoryKey(hit.namespace, hit.key))
            if entry is None:
                drift_count += 1
                continue
            if memory_types is not None and entry.memory_type not in memory_types:
                continue
            hydrated.append((entry, hit.vector_sim, hit.graph_relevance))

        if not hydrated:
            self._emit_metric("hybrid_query_all_drift", 1, mode=mode)
            return self._fallback_to_reranked(
                query, namespace, top_k, memory_types, reason="hydration_empty"
            )

        if drift_count:
            logger.info(
                "search_hybrid: dropped %d/%d drift hits "
                "(graph references absent from backend)",
                drift_count,
                len(lr_hits),
            )
            self._emit_metric("hybrid_drift_drops", drift_count, mode=mode)

        graph_raw = [g for (_, _, g) in hydrated]
        g_min = min(graph_raw)
        g_max = max(graph_raw)
        g_range = g_max - g_min

        def normalize_g(raw: float) -> float:
            if g_range <= 0:
                return 0.5
            return (raw - g_min) / g_range

        scored: list[_VEntry] = []
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

        scored.sort(key=lambda e: e.final_score, reverse=True)
        results = scored[:top_k]

        self._touch_and_count_async(results)

        self._emit_metric("hybrid_query_count", 1, mode=mode)
        self._emit_metric(
            "hybrid_query_latency_ms",
            int((time.monotonic() - t_start) * 1000),
            mode=mode,
        )
        logger.info(
            "hybrid_query: mode=%s top_k=%d returned=%d hits=%d "
            "drift=%d t_lr_ms=%.1f t_total_ms=%.1f",
            mode,
            top_k,
            len(results),
            len(lr_hits),
            drift_count,
            t_lr_ms,
            (time.monotonic() - t_start) * 1000,
        )

        return results

    def _touch_and_count_async(
        self,
        entries: list[VectorEntry],
    ) -> None:
        """Background-touch each entry: bump ``accessed_at`` + ``access_count``.

        Fire-and-forget; lost updates on shutdown are tolerable because
        ``access_count`` is advisory. Concurrent calls on the same key may
        lose an increment under race — see Phase 3 §5.6.
        """
        if not entries:
            return

        snapshots = [(e.key, int(e.metadata.get("access_count") or 0)) for e in entries]
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

        t = threading.Thread(target=_do, daemon=True)
        t.start()

    def close(self) -> None:
        """Drain the LightRAG executor and close the underlying backend."""
        if not self._closed:
            self._closed = True
            try:
                self._ingest_executor.shutdown(wait=True, cancel_futures=False)
            except Exception:
                logger.warning("lr_ingest: executor shutdown errored", exc_info=True)
        super().close()

    def __del__(self) -> None:
        try:
            if not getattr(self, "_closed", True):
                self._ingest_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
