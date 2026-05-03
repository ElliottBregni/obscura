"""Data models for dynamic memory channels.

Memory channels define targeted semantic memory queries that activate
based on context signals (file paths, tool calls, keywords).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChannelTriggers:
    """Conditions that activate a memory channel.

    At least one trigger must match for the channel to fire.
    If *always* is ``True`` the channel fires regardless of signals.
    """

    file_globs: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    always: bool = False


@dataclass(frozen=True)
class MemoryChannel:
    """A named, targeted memory query with activation triggers.

    Parameters
    ----------
    name:
        Unique identifier, e.g. ``"workspace-architecture"``.
    namespace:
        Vector memory namespace to query.
    triggers:
        :class:`ChannelTriggers` that decide when this channel fires.
    query_template:
        Supports ``{query}``, ``{file_stem}``, ``{file_path}``, ``{tool_name}``.
    max_tokens:
        Token budget for this channel's context injection.
    injection:
        ``"system"`` (once at session start) or ``"turn"`` (per-turn when triggered).
    priority:
        Higher values inject first.
    enabled:
        Set to ``False`` to disable without removing.

    """

    name: str
    namespace: str
    triggers: ChannelTriggers = field(default_factory=ChannelTriggers)
    query_template: str = "{query}"
    max_tokens: int = 500
    injection: str = "turn"
    priority: int = 50
    enabled: bool = True


@dataclass
class ContextSignals:
    """Mutable per-session accumulator of context signals.

    *file_paths* and *tool_names* persist across turns.
    *keywords* and *current_query* are reset each turn.
    """

    file_paths: set[str] = field(default_factory=set[str])
    keywords: set[str] = field(default_factory=set[str])
    tool_names: set[str] = field(default_factory=set[str])
    current_query: str = ""

    def reset_turn(self) -> None:
        """Clear per-turn signals.  Session-level signals persist."""
        self.keywords.clear()
        self.current_query = ""
