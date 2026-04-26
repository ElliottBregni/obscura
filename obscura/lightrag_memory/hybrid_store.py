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
from typing import TYPE_CHECKING

from obscura.vector_memory import VectorMemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.auth.models import AuthenticatedUser
    from obscura.lightrag_memory.adapter import LightRAGAdapter
    from obscura.memory.events import EventSink
    from obscura.vector_memory.backends import VectorBackend
    from obscura.vector_memory.decay import DecayConfig

_log = logging.getLogger(__name__)


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
        _log.debug(
            "HybridVectorMemoryStore initialized for user=%s (Phase 1 stub: "
            "no fan-out wiring active yet)",
            user.user_id[:8],
        )
