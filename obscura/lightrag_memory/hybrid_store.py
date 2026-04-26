"""obscura.lightrag_memory.hybrid_store — Drop-in subclass of VectorMemoryStore.

Phase 1: empty subclass. The fan-out logic on ``set()`` / ``delete()`` and
the new ``search_hybrid()`` method land in Phases 2-3.

The constructor signature is locked here so callers (and tests) can refer
to ``HybridVectorMemoryStore(user, lightrag_adapter=...)`` from Phase 1
onwards even though the overrides aren't filled in yet.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from obscura.vector_memory import VectorMemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import timedelta

    from obscura.auth.models import AuthenticatedUser
    from obscura.lightrag_memory.adapter import LightRAGAdapter
    from obscura.memory import MemoryKey
    from obscura.memory.events import EventSink
    from obscura.vector_memory.backends import VectorBackend, VectorEntry
    from obscura.vector_memory.decay import DecayConfig

logger = logging.getLogger(__name__)


class HybridVectorMemoryStore(VectorMemoryStore):
    """Vector memory store with LightRAG fan-out.

    Phase 1 stub. Inherits the entire :class:`VectorMemoryStore` API
    unchanged. Phases 2-3 will override:

    - :meth:`set` — fan out to ``self._lr.insert_safe`` after super().set
    - :meth:`delete` — fan out to ``self._lr.delete_safe``
    - :meth:`search_hybrid` — new method, returns rerank-via-graph results
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
    ) -> None:
        super().__init__(
            user,
            backend=backend,
            embedding_fn=embedding_fn,
            decay_config=decay_config,
            event_sink=event_sink,
        )
        self._lr = lightrag_adapter
        self._ingest_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix=f"lr-ingest-{user.user_id[:8]}",
        )
        logger.debug(
            "HybridVectorMemoryStore initialized for user=%s "
            "(Phase 1 stub: no fan-out wiring active yet)",
            user.user_id[:8],
        )

    # ------------------------------------------------------------------
    # Phase 2/3 placeholders.
    #
    # These overrides keep the public surface stable while the parallel
    # branches that implement them land. Once Phase 2 + Phase 3 merge,
    # these stubs are replaced with the real fan-out and search logic.
    # ------------------------------------------------------------------

    def set(  # type: ignore[override]
        self,
        key: str | MemoryKey,
        text: str,
        metadata: dict[str, Any] | None = None,
        namespace: str = "default",
        ttl: timedelta | None = None,
        memory_type: str = "general",
    ) -> None:
        raise NotImplementedError("provided by Phase 2/3")

    def delete(self, key: str | MemoryKey, namespace: str = "default") -> bool:  # type: ignore[override]
        raise NotImplementedError("provided by Phase 2/3")

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
        raise NotImplementedError("provided by Phase 2/3")

    async def _touch_and_count_async(
        self,
        entries: list[VectorEntry],
    ) -> None:
        raise NotImplementedError("provided by Phase 2/3")

    # ------------------------------------------------------------------
    # Phase 4 — explicit lifecycle.
    # ------------------------------------------------------------------

    def close(self) -> None:  # type: ignore[override]
        """Drain pending ingest jobs and stop the LightRAG adapter.

        Called explicitly at logout or process exit. Idempotent.
        Errors are logged but not raised — shutdown must complete.
        Always invokes ``super().close()`` so the underlying vector backend
        connection is closed too.
        """
        try:
            self._ingest_executor.shutdown(wait=True, cancel_futures=False)
        except Exception:
            logger.exception(
                "Failed to drain ingest executor for user %s",
                self.user_id[:8],
            )
        try:
            self._lr.close()
        except Exception:
            logger.exception(
                "Failed to close LightRAG adapter for user %s",
                self.user_id[:8],
            )
        try:
            super().close()
        except Exception:
            logger.exception(
                "Failed to close vector backend for user %s",
                self.user_id[:8],
            )
