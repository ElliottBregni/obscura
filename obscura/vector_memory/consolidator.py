"""Memory consolidation — compress old episodes into summaries.

Groups old episode memories by session, summarizes each group (via LLM
or fallback), stores the summaries as ``"summary"`` type, and deletes
the originals.

Usage::

    from obscura.vector_memory.consolidator import MemoryConsolidator

    consolidator = MemoryConsolidator(store=store, config=decay_config)
    episodes_deleted, summaries_created = consolidator.consolidate()
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from obscura.core.auth import AuthConfig
from obscura.providers.copilot import CopilotBackend

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.vector_memory.backends.base import VectorEntry
    from obscura.vector_memory.decay import DecayConfig
    from obscura.vector_memory.vector_memory import VectorMemoryStore

_log = logging.getLogger(__name__)

# Minimum group size to trigger consolidation.
_MIN_GROUP_SIZE = 3

# Prompt template for LLM-based summarization.
_SUMMARIZE_PROMPT = """\
Summarize the following conversation excerpts into a concise paragraph.
Preserve key facts, decisions, and outcomes.  Drop greetings and filler.

---
{text}
---

Summary (1-2 paragraphs, factual, third-person):"""


class MemoryConsolidator:
    """Consolidates old episode memories into summaries.

    Parameters
    ----------
    store:
        The :class:`VectorMemoryStore` instance to read/write memories.
    config:
        :class:`DecayConfig` controlling ``consolidation_age_days`` and
        ``consolidation_batch_size``.
    summarize_fn:
        Optional callable ``(texts: list[str]) -> str``.  When ``None``
        the consolidator tries to use the active LLM backend, falling
        back to simple concatenation.

    """

    def __init__(
        self,
        store: VectorMemoryStore,
        config: DecayConfig,
        summarize_fn: Callable[[list[str]], str] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.summarize_fn = summarize_fn or self._make_llm_summarizer()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def consolidate(self, namespace: str | None = None) -> tuple[int, int]:
        """Run one consolidation pass.

        Parameters
        ----------
        namespace:
            If provided, only consolidate episodes in this namespace.
            ``None`` consolidates across all namespaces.

        Returns ``(episodes_deleted, summaries_created)``.

        """
        cutoff = datetime.now(UTC) - timedelta(days=self.config.consolidation_age_days)
        limit = self.config.consolidation_batch_size * _MIN_GROUP_SIZE

        episodes = self.store.backend.list_by_type(
            "episode",
            older_than=cutoff,
            limit=limit,
        )
        # Filter by namespace if specified
        if namespace is not None:
            episodes = [e for e in episodes if e.key.namespace == namespace]
        if not episodes:
            return 0, 0

        groups = self._group_by_session(episodes)
        deleted = 0
        created = 0

        for session_id, entries in groups.items():
            if len(entries) < _MIN_GROUP_SIZE:
                continue  # too small — let natural decay handle them

            texts = [e.text for e in entries]
            try:
                summary_text = self.summarize_fn(texts)
            except Exception:
                _log.debug(
                    "summarize_fn failed for session %s, using fallback",
                    session_id,
                    exc_info=True,
                )
                summary_text = self._fallback_summarize(texts)

            # Store the summary
            ts = datetime.now(UTC).isoformat()
            summary_key = f"summary_{session_id}_{ts}"
            # Inherit metadata from first entry
            meta = dict(entries[0].metadata)
            meta["consolidated_from"] = len(entries)
            meta["original_session_id"] = session_id

            self.store.set(
                key=summary_key,
                text=summary_text,
                metadata=meta,
                namespace=entries[0].key.namespace,
                memory_type="summary",
            )
            created += 1

            # Delete originals
            for e in entries:
                try:
                    self.store.backend.delete_vector(e.key)
                    deleted += 1
                except Exception:
                    _log.debug("Failed to delete episode %s", e.key, exc_info=True)

        return deleted, created

    # ------------------------------------------------------------------
    # Grouping
    # ------------------------------------------------------------------

    @staticmethod
    def _group_by_session(episodes: list[VectorEntry]) -> dict[str, list[VectorEntry]]:
        """Group episodes by ``session_id`` in metadata."""
        groups: dict[str, list[VectorEntry]] = defaultdict(list)
        for ep in episodes:
            sid = ep.metadata.get("session_id", "unknown")
            groups[sid].append(ep)
        return dict(groups)

    # ------------------------------------------------------------------
    # Summarizers
    # ------------------------------------------------------------------

    def _make_llm_summarizer(self) -> Callable[[list[str]], str]:
        """Try to build an LLM-based summarizer.  Falls back to simple concat."""

        def _summarize(texts: list[str]) -> str:
            combined = "\n---\n".join(texts)
            prompt = _SUMMARIZE_PROMPT.format(text=combined[:4000])

            # Try Copilot backend first (most common workplace model)
            try:
                return self._call_llm(prompt)
            except Exception:
                _log.debug("LLM summarizer failed, using fallback", exc_info=True)
                return self._fallback_summarize(texts)

        return _summarize

    def _call_llm(self, prompt: str) -> str:
        """Call the active LLM backend for summarization.

        Uses a lightweight synchronous approach — imports the backend
        and makes a single completion call.
        """
        import asyncio

        auth = AuthConfig()
        backend = CopilotBackend(
            auth,
            model="gpt-4o-mini",
            system_prompt="You are a concise summarizer.",
        )

        async def _run() -> str:
            await backend.start()
            try:
                response = await backend.send(prompt, options={})
                return response.text if hasattr(response, "text") else str(response)
            finally:
                await backend.stop()

        # Only safe to call asyncio.run() when no loop is running
        has_loop = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            has_loop = False

        if has_loop:
            msg = "Cannot summarize from async context"
            raise RuntimeError(msg)
        return asyncio.run(_run())

    @staticmethod
    def _fallback_summarize(texts: list[str]) -> str:
        """Simple concatenation + truncation fallback."""
        combined = "\n---\n".join(texts)
        if len(combined) > 2000:
            combined = combined[:2000] + "..."
        return combined
