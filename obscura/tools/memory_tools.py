"""obscura.tools.memory_tools — Memory and vector storage tools for agents.

Provides agents with persistent memory capabilities through MemoryStore
and VectorMemoryStore APIs.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from obscura.core.types import ToolSpec
from obscura.memory import MemoryStore
from obscura.vector_memory import VectorMemoryStore

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser
    from obscura.memory_channels.models import MemoryChannel


def _project_namespace() -> str:
    """Derive a memory namespace from the current working directory.

    Returns ``project:<basename>`` so memories are automatically scoped
    to the active project without the agent needing to specify a namespace.
    Falls back to ``"default"`` if cwd cannot be read.
    """
    import os

    try:
        return f"project:{os.path.basename(os.getcwd())}"
    except Exception:
        return "default"


def _json_error(code: str, **details: Any) -> str:
    """Standard error envelope returned by graph tools.

    The model can branch cheaply on ``error`` (machine code); ``hint`` and
    other fields carry human-readable context. Stable contract — keep the
    code values stable across releases.
    """
    payload: dict[str, Any] = {"ok": False, "error": code}
    payload.update(details)
    return json.dumps(payload)


def _is_user_graph_enabled(user: AuthenticatedUser) -> bool:
    """Return True iff this user's vector memory is graph-aware.

    Centralizes the lazy-import + isinstance check so callers don't have
    to handle the ImportError case themselves. Without this helper every
    call site would need its own ``try / except ImportError`` around the
    HybridVectorMemoryStore import — which only resolves when the
    optional ``lightrag`` extra is installed.
    """
    try:
        from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
    except ImportError:
        return False
    return isinstance(VectorMemoryStore.for_user(user), HybridVectorMemoryStore)


def build_channels_prompt_section(
    channels: list[MemoryChannel],
    is_graph_enabled: bool = False,
) -> str:
    """Build a system prompt section describing available memory channels.

    Returns ``""`` if no channels are configured AND graph mode is off.

    Args:
        channels: Active memory channels for this user.
        is_graph_enabled: When True, append a "## Graph-aware memory" block
            describing memory_graph_query / memory_graph_explain. Should
            mirror whether ``VectorMemoryStore.for_user(user)`` returned a
            ``HybridVectorMemoryStore``. Pass False (default) for plain stores.
    """
    if not channels and not is_graph_enabled:
        return ""

    lines: list[str] = []

    if channels:
        lines.extend(
            [
                "## Memory Channels",
                "",
                "Context is automatically injected from these channels based on what you're working on.",
                "You can also explicitly store/search memories in channel namespaces using `store_searchable` and `semantic_search`.",
                "",
            ],
        )

        for ch in sorted(channels, key=lambda c: c.priority, reverse=True):
            trigger_parts: list[str] = []
            if ch.triggers.always:
                trigger_parts.append("always active")
            if ch.triggers.file_globs:
                trigger_parts.append(f"files: {', '.join(ch.triggers.file_globs)}")
            if ch.triggers.keywords:
                trigger_parts.append(f"keywords: {', '.join(ch.triggers.keywords)}")
            if ch.triggers.tool_names:
                trigger_parts.append(f"tools: {', '.join(ch.triggers.tool_names)}")

            trigger_str = "; ".join(trigger_parts) if trigger_parts else "manual"
            injection = "system prompt" if ch.injection == "system" else "per-turn"

            lines.append(
                f"- **{ch.name}** → namespace `{ch.namespace}` "
                f"({injection}, {trigger_str})",
            )

        lines.append("")
        lines.append(
            'To store a memory: `store_searchable(key, text, namespace="<namespace>", memory_type="fact")`',
        )
        lines.append(
            'To search a channel: `semantic_search(query, namespace="<namespace>")`',
        )

    if is_graph_enabled:
        if lines:
            lines.append("")
        lines.extend(
            [
                "## Graph-aware memory",
                "",
                "`semantic_search` results are ranked by combined vector similarity, "
                "graph relevance (entities and relations the query overlaps with), "
                "recency decay, and access frequency. Use it for plain lookups.",
                "",
                "`memory_graph_query(query, mode=...)` adds explicit mode control: "
                "`local` for focused entity neighborhoods, `global` for community-level "
                "summaries, `hybrid` (default) for multi-hop reasoning across entities.",
                "",
                "`memory_graph_explain(key)` shows which entities and relations a "
                "stored memory participates in. Cheap (no LLM call); use for debugging "
                "or to plan a follow-up `memory_graph_query`.",
            ],
        )

    return "\n".join(lines)


def make_memory_tool_specs(user: AuthenticatedUser) -> list[ToolSpec]:
    """Create memory tool specs bound to a user."""

    def store_memory_impl(namespace: str, key: str, value: dict[str, Any]) -> str:
        """Store key-value data in agent memory."""
        store = MemoryStore.for_user(user)
        store.set(namespace=namespace, key=key, value=value)
        return json.dumps(
            {
                "ok": True,
                "action": "store",
                "namespace": namespace,
                "key": key,
                "value_keys": list(value.keys()) if isinstance(value, dict) else None,
            },
        )

    def recall_memory_impl(namespace: str, key: str) -> str:
        """Retrieve data from agent memory."""
        store = MemoryStore.for_user(user)
        result = store.get(namespace=namespace, key=key)
        if result is None:
            return json.dumps(
                {
                    "ok": True,
                    "found": False,
                    "namespace": namespace,
                    "key": key,
                    "value": None,
                },
            )
        return json.dumps(
            {
                "ok": True,
                "found": True,
                "namespace": namespace,
                "key": key,
                "value": result,
            },
        )

    def semantic_search_impl(
        query: str,
        top_k: int = 5,
        namespace: str | None = None,
    ) -> str:
        """Search memory using semantic similarity, optionally in a specific namespace.

        When the user's store is a :class:`HybridVectorMemoryStore`, routes
        through ``search_hybrid()`` (mode="hybrid") for graph-aware ranking.
        Otherwise behaves exactly as before — plain vector similarity.
        """
        from obscura.vector_memory.backends import VectorEntry

        try:
            from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
        except ImportError:
            HybridVectorMemoryStore = None  # type: ignore[assignment, misc]

        store = VectorMemoryStore.for_user(user)
        is_hybrid = HybridVectorMemoryStore is not None and isinstance(
            store, HybridVectorMemoryStore
        )

        def _do_search(ns: str | None) -> list[VectorEntry]:
            if is_hybrid:
                return store.search_hybrid(  # type: ignore[attr-defined]
                    query,
                    mode="hybrid",
                    top_k=top_k,
                    namespace=ns,
                )
            return store.search_similar(query, namespace=ns, top_k=top_k)

        if namespace is None:
            proj_ns = _project_namespace()
            proj_results = _do_search(proj_ns)
            global_results = _do_search(None)
            seen_keys: set[str] = set()
            results = []
            for r in proj_results + global_results:
                rk = str(getattr(r, "key", r))
                if rk not in seen_keys:
                    seen_keys.add(rk)
                    results.append(r)
            results = results[:top_k]
        else:
            results = _do_search(namespace)

        items: list[dict[str, Any]] = []
        for r in results:
            item: dict[str, Any] = {
                "key": str(r.key),
                "namespace": r.key.namespace if hasattr(r.key, "namespace") else "",
                "score": round(r.score, 3),
                "final_score": round(r.final_score, 3),
                "text": r.text,
                "memory_type": r.memory_type,
                "metadata": r.metadata,
            }
            if is_hybrid:
                item["graph_relevance"] = round(r.rerank_score, 3)
            items.append(item)

        return json.dumps(
            {
                "ok": True,
                "query": query,
                "namespace": namespace,
                "count": len(items),
                "results": items,
            },
        )

    def store_searchable_impl(
        key: str,
        text: str,
        namespace: str = "",
        memory_type: str = "general",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store text with vector embedding for semantic search in a specific namespace."""
        resolved_ns = namespace or _project_namespace()
        store = VectorMemoryStore.for_user(user)
        store.set(
            key=key,
            text=text,
            namespace=resolved_ns,
            memory_type=memory_type,
            metadata=metadata or {},
        )
        return json.dumps(
            {
                "ok": True,
                "action": "store_searchable",
                "namespace": resolved_ns,
                "key": key,
                "memory_type": memory_type,
                "text_length": len(text),
            },
        )

    def memory_graph_query_impl(
        query: str,
        mode: str = "hybrid",
        top_k: int = 5,
        namespace: str | None = None,
    ) -> str:
        """Search memory with knowledge-graph awareness.

        Routes through :meth:`HybridVectorMemoryStore.search_hybrid`. Returns
        chunks ranked by combined vector + graph + recency + usage signals.
        """
        from obscura.core.tool_context import current_tool_context

        try:
            from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
        except ImportError:
            return _json_error(
                "graph_unavailable",
                hint=(
                    "Graph-aware retrieval is not active for this user. "
                    "Use semantic_search instead, or enable OBSCURA_LIGHTRAG=on "
                    "and install the lightrag extra."
                ),
            )

        ctx = current_tool_context()
        active_user = ctx.user if ctx is not None and ctx.user is not None else user
        if active_user is None:
            return _json_error(
                "no_context",
                hint="Tool invoked without a bound user. Caller bug.",
            )

        valid_modes = {"naive", "local", "global", "hybrid", "mix"}
        if mode not in valid_modes:
            return _json_error(
                "invalid_mode",
                valid=sorted(valid_modes),
                given=mode,
            )

        store = VectorMemoryStore.for_user(active_user)
        if not isinstance(store, HybridVectorMemoryStore):
            return _json_error(
                "graph_unavailable",
                hint=(
                    "Graph-aware retrieval is not active for this user. "
                    "Use semantic_search instead, or enable OBSCURA_LIGHTRAG=on "
                    "and ensure the lightrag extra is installed."
                ),
            )

        resolved_ns = namespace if namespace is not None else _project_namespace()

        try:
            results = store.search_hybrid(
                query,
                mode=mode,
                top_k=top_k,
                namespace=resolved_ns,
            )
        except Exception as exc:
            return _json_error("search_failed", hint=str(exc)[:200])

        items: list[dict[str, Any]] = []
        for r in results:
            ns = r.key.namespace if hasattr(r.key, "namespace") else resolved_ns
            items.append(
                {
                    "key": str(r.key),
                    "namespace": ns,
                    "text": r.text,
                    "score": round(r.score, 3),
                    "graph_relevance": round(r.rerank_score, 3),
                    "final_score": round(r.final_score, 3),
                    "memory_type": r.memory_type,
                    "created_at": (r.created_at.isoformat() if r.created_at else None),
                    "metadata": r.metadata,
                },
            )
        return json.dumps(
            {
                "ok": True,
                "query": query,
                "mode": mode,
                "namespace": resolved_ns,
                "top_k": top_k,
                "count": len(items),
                "results": items,
            },
        )

    def memory_graph_explain_impl(
        key: str,
        namespace: str = "",
        depth: int = 1,
    ) -> str:
        """Inspect entities and graph neighbors for a stored memory.

        Reads from LightRAG's NetworkX graph directly. No LLM call, no
        embedding lookup — fast and free.
        """
        from obscura.core.tool_context import current_tool_context

        try:
            from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
        except ImportError:
            return _json_error(
                "graph_unavailable",
                hint=(
                    "Graph-aware retrieval is not active. "
                    "Cannot explain entities for a chunk that was never indexed."
                ),
            )

        ctx = current_tool_context()
        active_user = ctx.user if ctx is not None and ctx.user is not None else user
        if active_user is None:
            return _json_error("no_context")

        store = VectorMemoryStore.for_user(active_user)
        if not isinstance(store, HybridVectorMemoryStore):
            return _json_error(
                "graph_unavailable",
                hint=(
                    "Graph-aware retrieval is not active. "
                    "Cannot explain entities for a chunk that was never indexed."
                ),
            )

        clamped_depth = max(1, min(depth, 3))
        resolved_ns = namespace or _project_namespace()
        doc_id = f"{resolved_ns}::{key}"

        try:
            explanation = store._lr.get_neighbors(  # pyright: ignore[reportPrivateUsage]
                doc_id=doc_id,
                depth=clamped_depth,
            )
        except KeyError:
            return _json_error(
                "key_not_found",
                namespace=resolved_ns,
                key=key,
                hint=(
                    "This memory may not be graph-indexed yet. Save it via "
                    "store_searchable to trigger indexing, or run the "
                    "`obscura memory backfill-graph` CLI to index existing "
                    "chunks."
                ),
            )
        except Exception as exc:
            return _json_error("explain_failed", hint=str(exc)[:200])

        return json.dumps(
            {
                "ok": True,
                "key": key,
                "namespace": resolved_ns,
                "depth": clamped_depth,
                "entities": explanation.entities,
                "relations": explanation.relations,
                "neighbor_chunks": explanation.neighbors[:10],
            },
        )

    specs: list[ToolSpec] = [
        ToolSpec(
            name="store_memory",
            description="Store key-value data in agent memory",
            parameters={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": (
                            "Logical grouping (e.g., 'session', 'project')"
                        ),
                    },
                    "key": {"type": "string", "description": "Memory key"},
                    "value": {
                        "type": "object",
                        "description": "JSON-serializable value to store",
                    },
                },
                "required": ["namespace", "key", "value"],
            },
            handler=store_memory_impl,
        ),
        ToolSpec(
            name="recall_memory",
            description="Retrieve data from agent memory",
            parameters={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": (
                            "Logical grouping (e.g., 'session', 'project')"
                        ),
                    },
                    "key": {
                        "type": "string",
                        "description": "Memory key to retrieve",
                    },
                },
                "required": ["namespace", "key"],
            },
            handler=recall_memory_impl,
        ),
        ToolSpec(
            name="semantic_search",
            description=(
                "Search vector memory using semantic similarity. "
                "Use namespace to search a specific memory channel "
                "(e.g. 'workspace:architecture', 'project:jira'). "
                "Omit namespace to search all."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query text",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results (default: 5)",
                        "default": 5,
                    },
                    "namespace": {
                        "type": "string",
                        "description": (
                            "Memory channel namespace to search "
                            "(e.g. 'workspace:architecture', 'project:jira'). "
                            "Omit to search all namespaces."
                        ),
                    },
                },
                "required": ["query"],
            },
            handler=semantic_search_impl,
        ),
        ToolSpec(
            name="store_searchable",
            description=(
                "Store text with vector embedding for semantic search. "
                "Use namespace to store in a specific memory channel "
                "(e.g. 'workspace:architecture', 'project:jira', 'user:preferences')."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Unique key for the content",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to store and embed",
                    },
                    "namespace": {
                        "type": "string",
                        "description": (
                            "Memory channel namespace "
                            "(e.g. 'workspace:architecture', 'project:jira'). "
                            "Defaults to project:<cwd-basename> when omitted."
                        ),
                        "default": "",
                    },
                    "memory_type": {
                        "type": "string",
                        "description": (
                            "Memory type: 'fact', 'episode', 'summary', "
                            "'preference', or 'general' (default)"
                        ),
                        "default": "general",
                        "enum": ["general", "fact", "episode", "summary", "preference"],
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Optional metadata (JSON object)",
                    },
                },
                "required": ["key", "text"],
            },
            handler=store_searchable_impl,
        ),
    ]

    # Graph-aware tools — only registered when LightRAG is active for this user.
    # Reuses the same default capability tier as semantic_search; both are
    # enhanced retrieval, not a new operation class.
    if _is_user_graph_enabled(user):
        specs.extend(
            [
                ToolSpec(
                    name="memory_graph_query",
                    description=(
                        "Search memory with knowledge-graph awareness. Combines vector "
                        "similarity, graph relevance (entity/relation overlap with the "
                        "query), recency decay, and access frequency into a single ranking. "
                        "Use mode='local' for focused entity neighborhoods, 'global' for "
                        "community-level summaries, or 'hybrid' (default) for multi-hop "
                        "reasoning. Slightly more expensive than semantic_search "
                        "(~200-500ms vs ~20-50ms) — prefer semantic_search for plain "
                        "similarity lookups."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search query text.",
                            },
                            "mode": {
                                "type": "string",
                                "description": (
                                    "Retrieval mode: 'naive' (vector only), 'local' "
                                    "(single-entity neighborhood), 'global' (community "
                                    "summary), 'hybrid' (default, multi-hop), or 'mix' "
                                    "(combined hybrid + naive)."
                                ),
                                "enum": [
                                    "naive",
                                    "local",
                                    "global",
                                    "hybrid",
                                    "mix",
                                ],
                                "default": "hybrid",
                            },
                            "top_k": {
                                "type": "integer",
                                "description": "Number of results to return (default: 5).",
                                "default": 5,
                            },
                            "namespace": {
                                "type": "string",
                                "description": (
                                    "Memory channel namespace to search "
                                    "(e.g. 'project:obscura', 'workspace:architecture'). "
                                    "Defaults to project:<cwd-basename> when omitted."
                                ),
                            },
                        },
                        "required": ["query"],
                    },
                    handler=memory_graph_query_impl,
                ),
                ToolSpec(
                    name="memory_graph_explain",
                    description=(
                        "Inspect the entities and relations associated with a stored "
                        "memory. Returns the entities extracted from the chunk and its "
                        "1-hop graph neighbors (or up to 3 hops via `depth`). Useful for "
                        "understanding how a piece of memory connects to other memories. "
                        "Cheap: no LLM call, reads the local graph directly."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": "The memory key (as returned by store_searchable).",
                            },
                            "namespace": {
                                "type": "string",
                                "description": (
                                    "Namespace the memory was stored in. Defaults to "
                                    "project:<cwd-basename> when omitted."
                                ),
                                "default": "",
                            },
                            "depth": {
                                "type": "integer",
                                "description": (
                                    "Hop distance to traverse from the chunk's entities. "
                                    "Clamped to [1, 3]. Default: 1."
                                ),
                                "default": 1,
                            },
                        },
                        "required": ["key"],
                    },
                    handler=memory_graph_explain_impl,
                ),
            ],
        )

    return specs
