"""Lazy memory-recall tools backed by SQLite FTS5.

Complements ``obscura.vector_memory`` (semantic, always-on) with a cheap,
on-demand keyword recall path. The agent calls ``recall_memory`` only
when it has reason to believe prior session context is relevant —
nothing runs eagerly. Use ``remember_memory`` from the agent loop to
write a short note for future sessions.

Storage: single SQLite file at ``~/.obscura/memories.db`` with an FTS5
virtual table over the content. See :mod:`obscura.memory.sqlite_fts`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from obscura.core.tools import tool
from obscura.data.keyword_memory import KeywordMemoryRepo, get_keyword_memory_repo

logger = logging.getLogger(__name__)


# Reuse a single repo instance across calls. The repo is stateless
# beyond schema-init, but constructing one re-runs the schema check —
# cache so we only pay that once.
_store: KeywordMemoryRepo | None = None


def _get_store() -> KeywordMemoryRepo:
    global _store
    if _store is None:
        _store = get_keyword_memory_repo()
    return _store


@tool(
    "recall_memory",
    (
        "Search the lazy keyword-memory store (SQLite FTS5) for prior notes "
        "that match `query`. Use this when the user references something from "
        "an earlier session, or when you suspect prior context is relevant. "
        "Phrase the query as keywords ('auth refactor', not 'what was the "
        "auth refactor about'). For semantic similarity instead of keyword "
        "match, the heavier vector_memory path runs separately on the user's "
        "message — you don't need to invoke that yourself."
    ),
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "FTS5 keyword query. Supports phrase matching with quotes "
                    '(e.g. "auth bug") and AND/OR/NOT operators.'
                ),
            },
            "namespace": {
                "type": "string",
                "description": (
                    "Optional namespace filter (e.g. 'cli', 'agent:foo'). "
                    "Omit to search across all namespaces."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "Max results to return (default 5, max 20).",
            },
        },
        "required": ["query"],
    },
)
async def recall_memory(
    query: str,
    namespace: str = "",
    top_k: int = 5,
) -> str:
    if not query or not query.strip():
        return json.dumps({"ok": False, "error": "empty_query"})
    try:
        cap = max(1, min(int(top_k), 20))
    except (TypeError, ValueError):
        logger.debug("invalid top_k for recall_memory: %r", top_k, exc_info=True)
        cap = 5
    ns = namespace.strip() or None
    try:
        store = _get_store()
        results = store.recall(query, namespace=ns, top_k=cap)
    except Exception as exc:
        logger.exception("recall_memory failed")
        return json.dumps({"ok": False, "error": "recall_failed", "detail": str(exc)})
    return json.dumps(
        {
            "ok": True,
            "query": query,
            "namespace": ns,
            "count": len(results),
            "results": [m.to_dict() for m in results],
        },
    )


@tool(
    "remember_memory",
    (
        "Persist a short note to the lazy keyword-memory store so it can be "
        "recalled in future sessions via `recall_memory`. Use sparingly — "
        "only for facts/decisions/preferences that are durable and "
        "non-obvious. Day-to-day work belongs in plans/code, not memories."
    ),
    {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The note to remember (1-3 sentences ideal).",
            },
            "namespace": {
                "type": "string",
                "description": (
                    "Bucket for organization (e.g. 'cli', 'project:obscura'). "
                    "Defaults to 'default'."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Optional structured metadata (tags, source URL, etc)."
                ),
            },
        },
        "required": ["content"],
    },
)
async def remember_memory(
    content: str,
    namespace: str = "default",
    metadata: dict[str, Any] | None = None,
) -> str:
    if not content or not content.strip():
        return json.dumps({"ok": False, "error": "empty_content"})
    try:
        store = _get_store()
        new_id = store.remember(
            content,
            namespace=namespace.strip() or "default",
            metadata=metadata or {},
        )
    except Exception as exc:
        logger.exception("remember_memory failed")
        return json.dumps(
            {"ok": False, "error": "remember_failed", "detail": str(exc)},
        )
    return json.dumps(
        {"ok": True, "id": new_id, "namespace": namespace.strip() or "default"},
    )


@tool(
    "vector_health",
    (
        "Probe the configured vector backend (Qdrant by default; pgvector "
        "or sqlite-vss when OBSCURA_VECTOR_BACKEND is set). Returns "
        "{ok, backend, enabled, latency_ms, error}. Cheap (~1 round-trip). "
        "Use before relying on `recall_semantic` if you suspect the store "
        "is misconfigured or unreachable."
    ),
    {"type": "object", "properties": {}},
)
async def vector_health() -> str:
    from obscura.data.vector_memory import vector_healthcheck

    return json.dumps(vector_healthcheck())


@tool(
    "list_memory_namespaces",
    (
        "List the namespaces in the lazy keyword-memory store along with "
        "the number of memories in each. Use to discover what's been "
        "remembered before crafting a `recall_memory` query."
    ),
    {"type": "object", "properties": {}},
)
async def list_memory_namespaces() -> str:
    try:
        store = _get_store()
        stats = store.stats()
    except Exception as exc:
        logger.exception("list_memory_namespaces failed")
        return json.dumps({"ok": False, "error": "stats_failed", "detail": str(exc)})
    return json.dumps({"ok": True, **stats})


# ---------------------------------------------------------------------------
# Semantic recall — on-demand path through the data layer.
# Routes through obscura.data.vector_memory so it gets fail-loud config
# checks, retry/backoff, and structured errors. Reranking (BM25/recency)
# is still a legacy-bridge concern — Phase 5 will fold it.
# ---------------------------------------------------------------------------


_DEFAULT_EMBED_DIM = 384


def _embed(query: str) -> list[float]:
    """Build a query embedding using the project's default hashing embedder."""
    from obscura.vector_memory.vector_memory import simple_embedding

    return simple_embedding(query, dim=_DEFAULT_EMBED_DIM)


@tool(
    "recall_semantic",
    (
        "Semantic search over vector memory (Qdrant by default; pgvector "
        "or sqlite-vss when OBSCURA_VECTOR_BACKEND is set). Use when "
        "keyword recall (`recall_memory`) misses conceptually-related "
        "content — e.g. searching 'login regression' should find a memory "
        "titled 'auth bug'. Pays one embedding round-trip per call. "
        "Returns structured errors on backend failure (`vector_disabled`, "
        "`vector_unavailable`, `recall_failed`) — call `vector_health` "
        "first if you suspect the store is down."
    ),
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Natural-language query (a question or topic, not just "
                    "keywords). Embedded for similarity match."
                ),
            },
            "namespace": {
                "type": "string",
                "description": (
                    "Optional namespace filter (e.g. 'user:profile'). "
                    "Omit to search all namespaces."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "Max results to return (default 5, max 20).",
            },
        },
        "required": ["query"],
    },
)
async def recall_semantic(
    query: str,
    namespace: str = "",
    top_k: int = 5,
) -> str:
    if not query or not query.strip():
        return json.dumps({"ok": False, "error": "empty_query"})
    try:
        cap = max(1, min(int(top_k), 20))
    except (TypeError, ValueError):
        logger.debug("invalid top_k for recall_semantic: %r", top_k, exc_info=True)
        cap = 5

    from obscura.auth.cli_user import current_cli_user
    from obscura.data.vector_memory import (
        VectorBackendUnavailable,
        VectorMemoryDisabled,
        VectorMemoryError,
        get_vector_memory_repo,
    )

    user = current_cli_user()
    user_id = getattr(user, "user_id", "default") if user else "default"

    repo = None
    try:
        repo = get_vector_memory_repo(
            user_id=user_id,
            embedding_dim=_DEFAULT_EMBED_DIM,
        )
        results = repo.search(
            _embed(query),
            namespace=namespace.strip() or None,
            top_k=cap,
        )
    except VectorMemoryDisabled as exc:
        logger.debug("recall_semantic: vector memory disabled", exc_info=True)
        return json.dumps(
            {"ok": False, "error": "vector_disabled", "detail": str(exc)},
        )
    except VectorBackendUnavailable as exc:
        logger.debug("recall_semantic: backend unavailable", exc_info=True)
        return json.dumps(
            {
                "ok": False,
                "error": "vector_unavailable",
                "backend": exc.backend,
                "detail": str(exc),
            },
        )
    except VectorMemoryError as exc:
        logger.debug("recall_semantic: vector memory error", exc_info=True)
        return json.dumps(
            {"ok": False, "error": "recall_failed", "detail": str(exc)},
        )
    except Exception as exc:
        logger.exception("unexpected recall_semantic failure")
        return json.dumps(
            {"ok": False, "error": "recall_failed", "detail": str(exc)},
        )
    finally:
        if repo is not None:
            repo.close()

    return json.dumps(
        {
            "ok": True,
            "query": query,
            "namespace": namespace.strip() or None,
            "count": len(results),
            "results": [r.to_dict() for r in results],
        },
    )
