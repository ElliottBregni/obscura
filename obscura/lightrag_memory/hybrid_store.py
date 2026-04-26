"""obscura.lightrag_memory.hybrid_store — Drop-in subclass of VectorMemoryStore.

Phase 2 ingest path: overrides ``set`` / ``delete`` / ``clear_namespace`` to
fan out async writes onto a per-user LightRAG instance via a bounded thread
pool. Inherits the entire read path from :class:`VectorMemoryStore` — Phase 3
will add ``search_hybrid``.

The canonical vector store remains the source of truth: a LightRAG fan-out
failure is logged but never propagates to the caller of ``set()`` /
``delete()``.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, override

from obscura.memory import MemoryKey
from obscura.vector_memory import VectorMemoryStore

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
_DEFAULT_MAX_TEXT_CHARS = 100_000


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

    @override
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

        if not self._should_index(memory_type, metadata, text, mkey):
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
            _log.warning(
                "lr_ingest: executor closed, skipping submission for %s",
                doc_id,
            )

    @override
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

    @override
    def clear_namespace(self, namespace: str) -> int:
        """Wipe a namespace from both Qdrant and LightRAG.

        Capture keys *before* the parent clears, then submit graph deletes
        in a single batch job. Doing 1000 individual ``submit()`` calls would
        exhaust the executor; one batch lets the worker page through them.
        """
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
                _log.warning(
                    "lr_ingest: executor closed during clear_namespace(%s), "
                    "%d graph entries may be orphaned",
                    namespace,
                    len(doc_ids),
                )

        return cleared

    @override
    def close(self) -> None:
        """Drain LightRAG executor, then close the underlying backend.

        Safe to call multiple times. Idempotent.
        """
        if not self._closed:
            self._closed = True
            try:
                self._ingest_executor.shutdown(wait=True, cancel_futures=False)
            except Exception:
                _log.warning("lr_ingest: executor shutdown errored", exc_info=True)
        super().close()

    def __del__(self) -> None:
        try:
            if not self._closed:
                self._ingest_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

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
        mkey: MemoryKey,
    ) -> bool:
        """Decide whether a write should be graph-indexed.

        Order of evaluation (escape hatches first):
        1. Explicit opt-out: metadata={"graph_index": False} — never index.
        2. Explicit opt-in:  metadata={"graph_index": True} — always index
           (subject to the short-text guard and oversized guard).
        3. memory_type whitelist (default: fact, summary, general).
        4. Short-text guard.
        5. Oversized guard.
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

        if len(text) > self._max_text_chars:
            _metric_inc("lr_inserts_skipped_filter", reason="oversized")
            _log.warning(
                "lr_ingest: text too long for graph indexing "
                "(len=%d, limit=%d, doc=%s::%s) — chunk stored in Qdrant only",
                len(text),
                self._max_text_chars,
                mkey.namespace,
                mkey.key,
            )
            return False

        return True

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
            except Exception:  # noqa: BLE001
                _log.warning(
                    "lr_ingest: delete failed during batch (doc=%s)",
                    doc_id,
                    exc_info=True,
                )


def _log_future_failure(future: Future[Any], doc_id: str) -> None:
    """Log any exception that escaped insert_safe.

    insert_safe is supposed to swallow everything — this is defense in
    depth in case a bug there raises.
    """
    try:
        exc = future.exception()
    except Exception:  # noqa: BLE001
        return
    if exc is not None:
        _log.warning(
            "lr_ingest: unexpected exception escaped insert_safe (doc=%s): %s",
            doc_id,
            exc,
        )


def _metric_inc(name: str, **labels: str) -> None:
    """Increment a counter, falling back to stdlib logging when OTel is absent.

    Mirrors the helper in :mod:`obscura.lightrag_memory.adapter`; see the
    note there for why we currently log instead of using ``get_meter()``.
    """
    try:
        if labels:
            _log.debug("lr_metric: %s %s", name, labels)
        else:
            _log.debug("lr_metric: %s", name)
    except Exception:
        pass
