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
from typing import TYPE_CHECKING, Any

from obscura.memory import MemoryKey
from obscura.vector_memory import VectorMemoryStore

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

    def search_hybrid(
        self,
        query: str,
        *,
        namespace: str | None = None,
        top_k: int = 5,
        mode: str = "hybrid",
        first_stage_k: int = 50,
        weights: Any | None = None,
    ) -> list[VectorEntry]:
        raise NotImplementedError("provided by Phase 3")

    async def _touch_and_count_async(
        self,
        entries: list[VectorEntry],
    ) -> None:
        raise NotImplementedError("provided by Phase 3")

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
