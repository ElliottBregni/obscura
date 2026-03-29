"""Context-aware memory channel router.

Watches agent events, extracts signals (file paths, tool names, keywords),
and queries only the memory channels whose triggers match.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from collections import defaultdict
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from obscura.memory_channels.models import ContextSignals, MemoryChannel

if TYPE_CHECKING:
    from obscura.vector_memory.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

# Regex for extracting file paths from text (same pattern as lifecycle.py)
_FILE_PATH_RE = re.compile(r"[\w/.~-]+\.(?:py|toml|yaml|yml|json|md|ts|tsx|js|jsx|rs|go|sh)")

# Global token budget across all channels per turn (chars ≈ tokens * 4)
_GLOBAL_BUDGET_TOKENS = 2000
_CHARS_PER_TOKEN = 4


class ContextRouter:
    """Routes memory queries to the right channels based on context signals.

    Parameters
    ----------
    channels:
        List of :class:`MemoryChannel` definitions.
    store:
        :class:`VectorMemoryStore` for querying memories.
    """

    def __init__(
        self,
        channels: list[MemoryChannel],
        store: VectorMemoryStore,
    ) -> None:
        self._channels = [c for c in channels if c.enabled]
        self._store = store
        self._signals = ContextSignals()

    # ------------------------------------------------------------------
    # Signal extraction
    # ------------------------------------------------------------------

    def update_signals_from_text(self, text: str) -> None:
        """Extract signals from user text (TURN_START).

        Resets per-turn signals, extracts file paths and keywords,
        stores current_query.
        """
        self._signals.reset_turn()
        self._signals.current_query = text

        # Extract file paths
        for match in _FILE_PATH_RE.findall(text):
            self._signals.file_paths.add(match)

        # Extract keywords (words length >= 3, lowercased)
        words = set(re.findall(r"\b\w{3,}\b", text.lower()))
        self._signals.keywords = words

    def update_signals_from_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
    ) -> None:
        """Extract signals from a TOOL_CALL event."""
        self._signals.tool_names.add(tool_name)

        if tool_input:
            path = tool_input.get("path") or tool_input.get("file_path") or ""
            if path:
                self._signals.file_paths.add(str(path))

    def update_signals_from_event(self, event: Any) -> None:
        """Extract signals from any AgentEvent (auto-dispatches by kind)."""
        kind_name = getattr(event, "kind", None)
        if kind_name is None:
            return

        kind_val = kind_name.value if hasattr(kind_name, "value") else str(kind_name)

        if kind_val == "turn_start":
            text = getattr(event, "text", None)
            if text:
                self.update_signals_from_text(text)
        elif kind_val == "tool_call":
            self.update_signals_from_tool_call(
                getattr(event, "tool_name", None) or "",
                getattr(event, "tool_input", None) or {},
            )

    # ------------------------------------------------------------------
    # Channel matching & querying
    # ------------------------------------------------------------------

    def query_active_channels(self, query: str | None = None) -> str:
        """Match channels against current signals, query each, format results.

        Parameters
        ----------
        query:
            Override query text.  Defaults to ``signals.current_query``.

        Returns
        -------
        str
            Formatted context block, or ``""`` if no channels matched.
        """
        if query is not None:
            self._signals.current_query = query

        turn_channels = [c for c in self._channels if c.injection == "turn"]
        return self._query_matched(turn_channels)

    def get_system_channels(self) -> str:
        """Query channels with ``injection="system"`` (called once at startup)."""
        system_channels = [c for c in self._channels if c.injection == "system"]
        return self._query_matched(system_channels)

    def _query_matched(self, channels: list[MemoryChannel]) -> str:
        """Core: match, query, format within budget."""
        # Sort by priority (highest first)
        sorted_channels = sorted(channels, key=lambda c: c.priority, reverse=True)

        sections: list[str] = []
        total_chars = 0
        budget_chars = _GLOBAL_BUDGET_TOKENS * _CHARS_PER_TOKEN

        for channel in sorted_channels:
            if total_chars >= budget_chars:
                break

            if not self._matches(channel):
                continue

            rendered_query = self._render_template(channel)
            if not rendered_query:
                continue

            try:
                results = self._store.search_reranked(
                    query=rendered_query,
                    namespace=channel.namespace,
                    top_k=3,
                    recency_weight=0.4,
                )
            except Exception:
                logger.debug("Channel %s query failed", channel.name, exc_info=True)
                continue

            if not results:
                continue

            # Format results within channel budget
            channel_budget_chars = channel.max_tokens * _CHARS_PER_TOKEN
            trigger_reason = self._describe_trigger(channel)
            section_lines = [f"**[{channel.name}]** _{trigger_reason}_"]
            section_chars = 0

            for entry in results:
                text = entry.text
                if section_chars + len(text) > channel_budget_chars:
                    remaining = channel_budget_chars - section_chars
                    if remaining > 100:
                        text = text[:remaining] + "..."
                    else:
                        break
                section_lines.append(f"- {text}")
                section_chars += len(text)

            if len(section_lines) > 1:  # has actual results beyond header
                section = "\n".join(section_lines)
                sections.append(section)
                total_chars += len(section)

        if not sections:
            return ""

        return "[Memory context]\n\n" + "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Trigger matching
    # ------------------------------------------------------------------

    def _matches(self, channel: MemoryChannel) -> bool:
        """Check if a channel's triggers match current signals."""
        triggers = channel.triggers

        if triggers.always:
            return True

        # File glob matching
        if triggers.file_globs:
            for glob_pattern in triggers.file_globs:
                for fp in self._signals.file_paths:
                    if fnmatch.fnmatch(fp, glob_pattern):
                        return True

        # Keyword matching (case-insensitive substring in current query)
        if triggers.keywords:
            query_lower = self._signals.current_query.lower()
            for kw in triggers.keywords:
                if kw.lower() in query_lower:
                    return True

        # Tool name matching
        if triggers.tool_names:
            for tn in triggers.tool_names:
                if tn in self._signals.tool_names:
                    return True

        return False

    def _describe_trigger(self, channel: MemoryChannel) -> str:
        """Return a short human-readable reason why this channel was triggered."""
        triggers = channel.triggers
        if triggers.always:
            return "always active"

        reasons: list[str] = []
        if triggers.file_globs:
            matched = [
                fp for glob_pat in triggers.file_globs
                for fp in self._signals.file_paths
                if fnmatch.fnmatch(fp, glob_pat)
            ]
            if matched:
                reasons.append(f"file: {matched[0]}")

        if triggers.keywords:
            query_lower = self._signals.current_query.lower()
            matched_kw = [kw for kw in triggers.keywords if kw.lower() in query_lower]
            if matched_kw:
                reasons.append(f"keyword: {matched_kw[0]}")

        if triggers.tool_names:
            matched_tools = [tn for tn in triggers.tool_names if tn in self._signals.tool_names]
            if matched_tools:
                reasons.append(f"tool: {matched_tools[0]}")

        return ", ".join(reasons) if reasons else "matched"

    # ------------------------------------------------------------------
    # Template rendering
    # ------------------------------------------------------------------

    def _render_template(self, channel: MemoryChannel) -> str:
        """Render a channel's query_template with available signal variables."""
        # Find the first matching file path for this channel
        first_file = ""
        if channel.triggers.file_globs:
            for glob_pattern in channel.triggers.file_globs:
                for fp in self._signals.file_paths:
                    if fnmatch.fnmatch(fp, glob_pattern):
                        first_file = fp
                        break
                if first_file:
                    break
        if not first_file and self._signals.file_paths:
            first_file = next(iter(self._signals.file_paths))

        first_tool = ""
        if channel.triggers.tool_names:
            for tn in channel.triggers.tool_names:
                if tn in self._signals.tool_names:
                    first_tool = tn
                    break

        file_stem = PurePosixPath(first_file).stem if first_file else ""

        # Use defaultdict so missing keys render as empty string
        vars_map: dict[str, str] = defaultdict(
            str,
            query=self._signals.current_query[:500],
            file_path=first_file,
            file_stem=file_stem,
            tool_name=first_tool,
        )

        try:
            return channel.query_template.format_map(vars_map)
        except Exception:
            return self._signals.current_query[:500]

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def channels(self) -> list[MemoryChannel]:
        """Active channels."""
        return list(self._channels)

    @property
    def signals(self) -> ContextSignals:
        """Current signal state (read-only view)."""
        return self._signals
