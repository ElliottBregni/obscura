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
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    import lightrag as _lightrag_pkg  # noqa: F401  # pyright: ignore[reportMissingImports, reportUnusedImport]
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


@dataclass(frozen=True)
class GraphExplanation:
    """Result of a graph neighborhood lookup for a single chunk.

    Returned by :meth:`LightRAGAdapter.get_neighbors`. Read by the
    ``memory_graph_explain`` tool.
    """

    entities: list[dict[str, Any]]
    """{"name", "type", "description"} dicts extracted from the chunk."""

    relations: list[dict[str, Any]]
    """{"source", "target", "description"} dicts the chunk participates in."""

    neighbors: list[str]
    """doc_ids that share entities/relations with this chunk."""


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

    indexable_types: frozenset[str] = frozenset({"fact", "summary", "general"})

    def __init__(
        self,
        user: AuthenticatedUser,
        embedding_fn: Callable[[str], list[float]],
    ) -> None:
        self.user = user
        self.user_id = user.user_id
        self._embedding_fn = embedding_fn
        self._closed = False

        self._embedding_dim = len(embedding_fn("test"))

        self._working_dir = _working_dir(user.user_id)
        self._working_dir.mkdir(parents=True, exist_ok=True)

        self._collection = _qdrant_collection_name(user.user_id)

        # Bring up the dedicated loop BEFORE constructing LightRAG, because
        # LightRAG's __init__ may schedule coroutines.
        self._loop, self._loop_thread = _start_loop_thread(
            name=f"lr-loop-{_user_hash(user.user_id)[:8]}",
        )

        self._lightrag = self._build_lightrag()

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
                try:
                    adapter.close()
                except Exception:
                    _log.exception("LightRAGAdapter close failed during reset")
            cls._instances.clear()

    @classmethod
    def close_all(cls) -> None:
        """Close every cached adapter. Safe to call multiple times.

        Wired as the ``atexit`` handler in
        :mod:`obscura.lightrag_memory` so process shutdown drains every
        per-user adapter before the interpreter tears down.
        """
        with cls._lock:
            for adapter in list(cls._instances.values()):
                try:
                    adapter.close()
                except Exception:
                    _log.exception(
                        "LightRAGAdapter close failed during close_all",
                    )
            cls._instances.clear()

    def insert_safe(
        self,
        doc_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> Future[Any]:
        """Schedule an async insert onto the dedicated loop and return.

        Phase 1 placeholder: this submits the coroutine and returns the
        ``concurrent.futures.Future``. Phase 2 will wire this from
        ``HybridVectorMemoryStore.set()``. Errors are logged + swallowed
        in the future's done-callback so they never propagate to the
        caller's write path.
        """
        coro = self._ainsert(doc_id, text, metadata or {})
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._log_future_error("insert", doc_id))
        return future

    def delete_safe(self, doc_id: str) -> Future[Any]:
        """Schedule an async delete onto the dedicated loop and return."""
        coro = self._adelete(doc_id)
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._log_future_error("delete", doc_id))
        return future

    async def aquery(
        self,
        query: str,
        mode: str = "hybrid",
        top_k: int = 20,
    ) -> list[GraphHit]:
        """Run a hybrid retrieval against the LightRAG instance.

        **Phase 1 placeholder.** Returns ``[]`` unconditionally. Phase 3
        populates real GraphHits.
        """
        _log.debug(
            "LightRAGAdapter.aquery is a Phase-1 placeholder; returning []. "
            "query=%r mode=%r top_k=%d",
            query[:80],
            mode,
            top_k,
        )
        return []

    def get_neighbors(self, doc_id: str, depth: int = 1) -> GraphExplanation:
        """Read entities and neighbors for a chunk from the local NetworkX graph.

        Does not run any LLM call. Does not query Qdrant. Reads the
        pickled NetworkX graph LightRAG maintains in working_dir and
        traverses up to ``depth`` hops out from the chunk's entities.

        Phase 4 introduces this hook on the adapter; the full traversal
        body lands alongside Phase 2/3 graph wiring (the methods this
        relies on don't exist on the LightRAG instance until then).

        Raises:
            KeyError: if doc_id is not present in the graph (chunk never
                indexed, or graph file missing).
        """
        raise NotImplementedError("provided by Phase 2/3")

    def close(self) -> None:
        """Stop the dedicated event loop. Idempotent.

        Safe to call from atexit. Errors are logged but not raised.
        """
        if self._closed:
            return
        try:
            if not self._loop.is_closed():
                if self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._loop.stop)
                self._loop_thread.join(timeout=5)
        except Exception:
            _log.exception("LightRAGAdapter event loop teardown failed")
        self._closed = True

    def shutdown(self) -> None:
        """Backwards-compatible alias for :meth:`close`."""
        self.close()

    # -- internals ----------------------------------------------------------

    def _build_lightrag(self) -> Any:
        """Construct the LightRAG instance bound to this user's storage."""
        from lightrag import LightRAG as _LightRAG  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
        from lightrag.utils import EmbeddingFunc  # pyright: ignore[reportMissingImports, reportUnknownVariableType]

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

        kw = _qdrant_kwargs()
        os.environ.setdefault("QDRANT_URL", kw.get("url", "") or "")
        os.environ.setdefault("QDRANT_API_KEY", kw.get("api_key", "") or "")

        return _LightRAG(  # pyright: ignore[reportUnknownVariableType]
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
