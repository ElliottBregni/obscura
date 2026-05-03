"""obscura.tools.memory_tools — Memory and vector storage tools for agents.

Provides agents with persistent memory capabilities through MemoryStore
and VectorMemoryStore APIs.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from obscura.core.types import ToolSpec
from obscura.memory import MemoryStore
from obscura.vector_memory import VectorMemoryEntry, VectorMemoryStore
import logging

logger = logging.getLogger(__name__)


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
        logger.debug("suppressed exception in _project_namespace", exc_info=True)
        return "default"


def build_channels_prompt_section(channels: list[MemoryChannel]) -> str:
    """Build a system prompt section describing available memory channels.

    Returns ``""`` if no channels are configured.
    """
    if not channels:
        return ""

    lines = [
        "## Memory Channels",
        "",
        "Context is automatically injected from these channels based on what you're working on.",
        "You can also explicitly store/search memories in channel namespaces using `store_searchable` and `semantic_search`.",
        "",
    ]

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
                "value_keys": list(value.keys()),
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
        """Search memory using semantic similarity, optionally in a specific namespace."""
        store = VectorMemoryStore.for_user(user)
        # When no namespace is specified, search the project namespace first
        # for relevance, then fall back to searching all namespaces.
        search_ns = namespace if namespace is not None else None
        results: list[VectorMemoryEntry]
        if search_ns is None:
            proj_ns = _project_namespace()
            proj_results = store.search_similar(query, namespace=proj_ns, top_k=top_k)
            global_results = store.search_similar(query, namespace=None, top_k=top_k)
            # Merge: project results first (higher priority), deduplicate by key.
            seen_keys: set[str] = set()
            merged: list[VectorMemoryEntry] = []
            for r in proj_results + global_results:
                rk = str(r.key)
                if rk not in seen_keys:
                    seen_keys.add(rk)
                    merged.append(r)
            results = merged[:top_k]
        else:
            results = store.search_similar(query, namespace=search_ns, top_k=top_k)
        items: list[dict[str, Any]] = [
            {
                "key": str(r.key),
                "namespace": r.key.namespace,
                "score": round(r.score, 3),
                "final_score": round(r.final_score, 3),
                "text": r.text,
                "memory_type": r.memory_type,
                "metadata": r.metadata,
            }
            for r in results
        ]
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

    return [
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
