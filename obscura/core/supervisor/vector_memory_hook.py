"""obscura.core.supervisor.vector_memory_hook — Wire vector memory into supervisor context.

Registers a PRE_BUILD_CONTEXT hook on the supervisor that searches vector
memory for relevant context and injects it into the prompt assembly.

Usage::

    from obscura.core.supervisor.vector_memory_hook import register_vector_memory_hooks

    register_vector_memory_hooks(hooks, vector_store=store, session_id="sess-1")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from obscura.core.supervisor.types import SupervisorHookPoint

if TYPE_CHECKING:
    from obscura.core.supervisor.session_hooks import SessionHookManager
    from obscura.vector_memory.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)


def register_vector_memory_hooks(
    hooks: SessionHookManager,
    *,
    vector_store: VectorMemoryStore | None = None,
    session_id: str = "",
    top_k: int = 5,
    recency_weight: float = 0.3,
) -> None:
    """Register vector memory hooks on a supervisor SessionHookManager.

    Adds a PRE_BUILD_CONTEXT before-hook that:
    1. Searches vector memory for context relevant to the prompt
    2. Injects the results into the hook context for the prompt assembler

    Also adds a POST_MODEL_TURN after-hook that:
    1. Auto-saves significant agent turns to vector memory

    Parameters
    ----------
    hooks:
        The supervisor's SessionHookManager.
    vector_store:
        An initialized VectorMemoryStore. If None, hooks are no-ops.
    session_id:
        Current session ID for memory namespacing.
    top_k:
        Number of memories to retrieve per query.
    recency_weight:
        Weight for recency in reranking (0=ignore time, 1=most recent only).
    """
    if vector_store is None:
        logger.debug("No vector store provided — skipping vector memory hooks")
        return

    # -- PRE_BUILD_CONTEXT: inject relevant memories into prompt assembly -----

    async def _inject_memory_context(context: dict[str, Any]) -> None:
        """Search vector memory and attach results to the hook context."""
        prompt = context.get("prompt", "")
        if not prompt:
            return

        try:
            results = vector_store.search_reranked(
                query=prompt,
                namespace=None,  # search all namespaces
                top_k=top_k,
                recency_weight=recency_weight,
            )
            if not results:
                return

            # Format memory results as a context section
            lines = ["## Relevant Context (from vector memory)"]
            for r in results:
                score_str = f"{r.score:.2f}" if hasattr(r, "score") else ""
                text = getattr(r, "text", str(r))
                if len(text) > 500:
                    text = text[:500] + "..."
                lines.append(f"- [{score_str}] {text}")

            memory_context = "\n".join(lines)
            context["_vector_memory_context"] = memory_context

            logger.info(
                "Injected %d vector memories into build context (session=%s)",
                len(results),
                session_id,
            )
        except Exception:
            logger.debug("Vector memory search failed in hook", exc_info=True)

    hooks.register(
        SupervisorHookPoint.PRE_BUILD_CONTEXT,
        "before",
        "vector_memory_inject",
        _inject_memory_context,
        persist=False,
    )

    # -- POST_MODEL_TURN: auto-save significant turns to memory ---------------

    async def _save_turn_to_memory(context: dict[str, Any]) -> None:
        """Save significant model turns to vector memory for future recall."""
        try:
            agent_event = context.get("agent_event")
            if agent_event is None:
                return

            # Only save substantial text outputs
            text = getattr(agent_event, "text", "")
            if not text or len(text) < 50:
                return

            namespace = f"session:{session_id}" if session_id else "session:unknown"
            vector_store.set(
                key=f"turn-{context.get('run_id', 'unknown')}",
                text=text[:2000],  # cap at 2k chars
                namespace=namespace,
                metadata={"session_id": session_id, "source": "supervisor"},
            )
        except Exception:
            logger.debug("Failed to save turn to vector memory", exc_info=True)

    hooks.register(
        SupervisorHookPoint.POST_MODEL_TURN,
        "after",
        "vector_memory_save",
        _save_turn_to_memory,
        persist=False,
    )

    logger.info(
        "Registered vector memory hooks (top_k=%d, recency=%.1f)", top_k, recency_weight
    )


__all__ = ["register_vector_memory_hooks"]
