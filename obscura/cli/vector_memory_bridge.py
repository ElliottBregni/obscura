"""obscura.cli.vector_memory_bridge — Vector memory integration for the CLI REPL.

Provides helpers for:
- Session-start memory retrieval
- Pre-message context injection (search before each user turn)
- Post-message auto-save (store conversation turns)
- Formatting vector results into system prompt sections
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser
    from obscura.vector_memory.vector_memory import VectorMemoryStore

_logger = logging.getLogger(__name__)

# Namespace used for all CLI auto-saved memories
CLI_NAMESPACE = "cli:conversation"


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


def is_vector_memory_enabled() -> bool:
    """Check if vector memory is enabled via env var.

    Defaults to True. Set OBSCURA_VECTOR_MEMORY=off to disable.
    """
    val = os.environ.get("OBSCURA_VECTOR_MEMORY", "on").strip().lower()
    return val not in ("off", "false", "0", "no")


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def init_vector_store(user: AuthenticatedUser) -> VectorMemoryStore | None:
    """Initialize a VectorMemoryStore for the CLI session.

    Returns None if vector memory is disabled or initialization fails.
    """
    if not is_vector_memory_enabled():
        return None
    try:
        from obscura.vector_memory import VectorMemoryStore

        return VectorMemoryStore.for_user(user)
    except Exception as e:
        _logger.warning(f"Could not initialize vector memory: {e}")
        return None


# ---------------------------------------------------------------------------
# Session-start context retrieval
# ---------------------------------------------------------------------------


def load_startup_memories(
    store: VectorMemoryStore,
    session_id: str,
    top_k: int = 3,
) -> str:
    """Search vector memory for recent/relevant context at session start.

    Uses a broad query to find recent important memories.
    Returns formatted string for injection into system prompt, or "".
    """
    try:
        results = store.search_reranked(
            query="recent conversation context and important information",
            namespace=CLI_NAMESPACE,
            top_k=top_k,
            recency_weight=0.5,
        )
        if not results:
            return ""
        return _format_memories_section(
            results, header="## Recalled Memories (from previous sessions)"
        )
    except Exception as e:
        _logger.warning(f"Could not load startup memories: {e}")
        return ""


# ---------------------------------------------------------------------------
# Pre-message search (RAG-style context injection)
# ---------------------------------------------------------------------------


def search_relevant_context(
    store: VectorMemoryStore,
    query: str,
    top_k: int = 3,
    threshold: float = 0.1,
) -> str:
    """Search vector memory for context relevant to the user's message.

    Returns a formatted context block to prepend to the user message,
    or "" if no relevant memories found.
    """
    try:
        results = store.search_reranked(
            query=query,
            namespace=None,
            top_k=top_k,
            recency_weight=0.2,
        )
        results = [r for r in results if r.score > threshold]
        if not results:
            return ""
        return _format_memories_section(
            results, header="[Relevant context from memory]"
        )
    except Exception as e:
        _logger.debug(f"Vector memory search failed: {e}")
        return ""


# ---------------------------------------------------------------------------
# Post-message auto-save
# ---------------------------------------------------------------------------


def auto_save_turn(
    store: VectorMemoryStore,
    session_id: str,
    user_text: str,
    assistant_text: str,
    turn_number: int,
) -> None:
    """Save a conversation turn to vector memory in a background thread.

    Saves a combined summary of the user message and assistant response.
    Runs in a daemon thread so it does not block the REPL.
    """

    def _save() -> None:
        try:
            # Skip persisting transport/debug noise from MCP server logs.
            if _is_mcp_noise_turn(user_text, assistant_text):
                return

            timestamp = datetime.now(UTC).isoformat()
            key = f"turn_{session_id}_{turn_number}_{timestamp}"

            user_snippet = user_text[:500]
            assistant_snippet = assistant_text[:1000]

            combined = f"User: {user_snippet}\nAssistant: {assistant_snippet}"

            store.set(
                key=key,
                text=combined,
                metadata={
                    "session_id": session_id,
                    "turn": turn_number,
                    "timestamp": timestamp,
                    "user_message_preview": user_text[:100],
                },
                namespace=CLI_NAMESPACE,
                memory_type="episode",
            )
        except Exception as e:
            _logger.debug(f"Auto-save to vector memory failed: {e}")

    thread = threading.Thread(target=_save, daemon=True)
    thread.start()


def clear_mcp_noise_memories(store: VectorMemoryStore) -> int:
    """Delete only MCP-log-like memories from the CLI namespace."""
    removed = 0
    try:
        keys = store.list_keys(namespace=CLI_NAMESPACE)
    except Exception:
        return 0

    for key in keys:
        try:
            entry = store.get(key)
            if entry is None:
                continue
            if _is_mcp_noise_text(entry.text):
                if store.delete(key):
                    removed += 1
        except Exception:
            continue
    return removed


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_memories_section(
    entries: list[Any],
    header: str = "## Relevant Memories",
    max_text_len: int = 300,
) -> str:
    """Format vector search results into a readable context section."""
    lines = [header, ""]
    for i, entry in enumerate(entries, 1):
        text = entry.text
        if len(text) > max_text_len:
            text = text[:max_text_len] + "..."
        score_str = f"{entry.score:.2f}"
        lines.append(f"{i}. (score: {score_str}) {text}")
        lines.append("")
    return "\n".join(lines)


def _is_mcp_noise_turn(user_text: str, assistant_text: str) -> bool:
    combined = f"{user_text}\n{assistant_text}"
    return _is_mcp_noise_text(combined)


def _is_mcp_noise_text(text: str) -> bool:
    s = text.lower()
    markers = (
        "mcp server",
        "/mcp ",
        "mcp:",
        "jsonrpc",
        "tool_use_start",
        "tool_use_delta",
        "tool_result",
        "invalid_request_body",
        "stdio transport",
        "anthropic.tools.beta.messages",
    )
    hit_count = sum(1 for marker in markers if marker in s)
    return hit_count >= 2
