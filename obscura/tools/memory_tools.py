"""obscura.tools.memory_tools — Memory and vector storage tools for agents.

Provides agents with persistent memory capabilities through MemoryStore
and VectorMemoryStore APIs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from obscura.core.types import ToolSpec
from obscura.memory import MemoryStore
from obscura.vector_memory import VectorMemoryStore

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser


def make_memory_tool_specs(user: AuthenticatedUser) -> list[ToolSpec]:
    """Create memory tool specs bound to a user."""

    def store_memory_impl(namespace: str, key: str, value: dict[str, Any]) -> str:
        """Store key-value data in agent memory."""
        store = MemoryStore.for_user(user)
        store.set(namespace=namespace, key=key, value=value)
        return f"✅ Stored {key} in namespace {namespace}"

    def recall_memory_impl(namespace: str, key: str) -> dict[str, Any] | None:
        """Retrieve data from agent memory."""
        store = MemoryStore.for_user(user)
        result = store.get(namespace=namespace, key=key)
        return result if result is not None else None

    def semantic_search_impl(query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Search memory using semantic similarity."""
        store = VectorMemoryStore.for_user(user)
        results = store.search_similar(query, top_k=top_k)
        return [
            {
                "key": str(r.key),
                "score": r.score,
                "text": r.text,
                "metadata": r.metadata,
            }
            for r in results
        ]

    def store_searchable_impl(
        key: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store text with vector embedding for semantic search."""
        store = VectorMemoryStore.for_user(user)
        store.set(key=key, text=text, metadata=metadata or {})
        return f"✅ Stored searchable content: {key}"

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
            description="Search memory using semantic similarity",
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
                },
                "required": ["query"],
            },
            handler=semantic_search_impl,
        ),
        ToolSpec(
            name="store_searchable",
            description="Store text with vector embedding for semantic search",
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
