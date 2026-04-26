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
    ) -> None:
        self.user = user
        self.user_id = user.user_id
        self._embedding_fn = embedding_fn

        self._embedding_dim = len(embedding_fn("test"))

        self._working_dir = _working_dir(user.user_id)
        self._working_dir.mkdir(parents=True, exist_ok=True)

        self._collection = _qdrant_collection_name(user.user_id)

        self._loop, self._loop_thread = _start_loop_thread(
            name=f"lr-loop-{_user_hash(user.user_id)[:8]}",
        )

        self._lightrag: Any = self._build_lightrag()

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
    ) -> Future[Any]:
        """Schedule an async insert onto the dedicated loop and return.

        Phase 1 placeholder: this submits the coroutine and returns the
        ``concurrent.futures.Future``. Phase 2 will harden the contract so
        the worker thread blocks on the future with a timeout and logs +
        swallows any failures.
        """
        coro = self._ainsert(doc_id, text, metadata or {})
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._log_future_error("insert", doc_id))
        return future

    def delete_safe(self, doc_id: str) -> Future[Any]:
        """Schedule an async delete onto the dedicated loop and return."""
        coro = self._adelete(doc_id)
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self._loop)
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

    @staticmethod
    def _log_future_error(op: str, doc_id: str) -> Callable[[Future[Any]], None]:
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
