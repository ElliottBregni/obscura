"""Tool presentation tiering — keep the system prompt focused.

Obscura registers 100+ tools in a fully-loaded session (system tools,
plugin integrations, MCP-discovered shadows). Listing every tool's full
description in the system prompt overloads the model — gpt-5.3-codex
in particular shows worse tool-selection accuracy when the prompt
exceeds ~80 tools, and pays a real token cost for every spec.

Tiering splits tools into two presentation classes:

* **core** — the small set the model uses constantly (filesystem, shell,
  web fetch/search, delegation, planning). Full schemas appear in the
  system prompt. ~20 tools.
* **deferred** — everything else (plugin/integration tools, niche system
  tools, MCP shadows). Names + 1-line descriptions appear in a
  "Discoverable via ``tool_search``" section. The model finds them by
  calling :func:`tool_search`, which returns full schemas on demand.

This module also exposes a per-task **discovery set**
(:data:`DISCOVERED_TOOLS`) so backends that send the tool list with
every API request (OpenAI Responses, agent_loop_v2-driven backends)
can drop deferred tools from each per-turn payload until the model
explicitly surfaces one via ``tool_search``. Once a tool is marked
discovered, every subsequent turn in the same task includes it, so
the model can call it freely.

Limitation: backends whose SDK commits the tool list at session start
(GitHub Copilot, Claude Agent SDK) cannot benefit from per-turn
filtering without a session recreate. For those backends every
registered tool stays callable regardless of tier; the system-prompt
presentation is still tiered. Phase 3 would add session-recreate-on-
discovery for those SDKs.

To customise: edit :data:`CORE_TOOL_NAMES`. Names not in the set are
deferred.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from obscura.core.types import ToolSpec


logger = logging.getLogger(__name__)


# Truthy tokens accepted by env-var gates — kept in one place so every
# Phase-3 callsite agrees on what "on" means.
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


# Tools that always get full schemas in the system prompt.
# Add a name here when the model needs to use it without a discovery step.
CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # Filesystem — read
        "list_directory",
        "read_text_file",
        # Filesystem — write
        "write_text_file",
        "append_text_file",
        "make_directory",
        "remove_path",
        # Search
        "grep_files",
        # Shell + binary discovery
        "run_shell",
        "run_command",
        "which_command",
        # Web
        "web_fetch",
        "web_search",
        # Delegation
        "task",
        "spawn_agents",
        "spawn_subagent",
        # Tool discovery (MUST be core — it's the doorway to deferred tools)
        "tool_search",
        "list_system_tools",
        # User interaction
        "ask_user",
        "user_interact",
        # Planning / todos (frequently-used context)
        "todo_write",
        "todo_read",
        # Provider notifications
        "file_change",
    },
)


def is_core(tool_name: str) -> bool:
    """Return True if the tool should appear with full schema in the prompt."""
    return tool_name in CORE_TOOL_NAMES


# ---------------------------------------------------------------------------
# Phase-3 SDK-tier env helpers
# ---------------------------------------------------------------------------


def is_phase3_active() -> bool:
    """Return True if ``OBSCURA_PHASE3_SDK_TIER`` is set truthy.

    When on, Copilot/Claude session-time filters drop deferred tools
    from the SDK config. ``tool_search`` uses this to warn the model
    that surfaced deferred tools may be uncallable until the session
    is recreated with the tool added to ``OBSCURA_PHASE3_EXTRA_CORE``.
    """
    return os.environ.get("OBSCURA_PHASE3_SDK_TIER", "").strip().lower() in _TRUTHY


def parse_extra_core_patterns() -> tuple[str, ...]:
    """Parse ``OBSCURA_PHASE3_EXTRA_CORE`` into a tuple of patterns.

    Comma-separated. Each entry is either an exact tool name or a
    :mod:`fnmatch` glob (``jira_*``, ``mcp__obs__*``). Empty / unset →
    empty tuple.
    """
    raw = os.environ.get("OBSCURA_PHASE3_EXTRA_CORE", "").strip()
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def effective_core_names(
    all_tool_names: Iterable[str] | None = None,
) -> frozenset[str]:
    """Return the effective core set: CORE plus any extras parsed from env.

    When extras include glob patterns, ``all_tool_names`` is needed to
    expand them. If a glob pattern is supplied without a tool universe
    (e.g. for a quick is-this-name-core check) it's silently dropped —
    callers that need glob support pass the universe explicitly.

    Exact-name extras (no glob characters) are always included even
    without ``all_tool_names``.
    """
    extras = parse_extra_core_patterns()
    if not extras:
        return CORE_TOOL_NAMES
    matched: set[str] = set(CORE_TOOL_NAMES)
    universe: list[str] = list(all_tool_names) if all_tool_names is not None else []
    for pattern in extras:
        if any(c in pattern for c in "*?["):
            if universe:
                matched.update(fnmatch.filter(universe, pattern))
        else:
            matched.add(pattern)
    return frozenset(matched)


def is_effectively_core(
    tool_name: str, all_tool_names: Iterable[str] | None = None
) -> bool:
    """Return True if ``tool_name`` is core OR matches an EXTRA_CORE pattern."""
    if tool_name in CORE_TOOL_NAMES:
        return True
    extras = parse_extra_core_patterns()
    if not extras:
        return False
    universe = list(all_tool_names) if all_tool_names is not None else [tool_name]
    if tool_name not in universe:
        universe = [*universe, tool_name]
    for pattern in extras:
        if any(c in pattern for c in "*?["):
            if fnmatch.fnmatch(tool_name, pattern):
                return True
        elif tool_name == pattern:
            return True
    return False


def split_by_tier(
    tools: list[ToolSpec],
) -> tuple[list[ToolSpec], list[ToolSpec]]:
    """Partition tools into ``(core, deferred)`` lists, preserving order.

    Order preservation matters for deterministic prompt generation —
    routers and tests rely on stable tool ordering.
    """
    core: list[ToolSpec] = []
    deferred: list[ToolSpec] = []
    for spec in tools:
        (core if spec.name in CORE_TOOL_NAMES else deferred).append(spec)
    return core, deferred


# ---------------------------------------------------------------------------
# Per-task discovery tracking
# ---------------------------------------------------------------------------

# Tools the model has explicitly surfaced this task via ``tool_search``.
# Backends that filter their per-turn tool list combine this with
# :data:`CORE_TOOL_NAMES` to decide what to send.
DISCOVERED_TOOLS: ContextVar[set[str] | None] = ContextVar(
    "obscura_discovered_tools",
    default=None,
)


def is_visible(tool_name: str) -> bool:
    """Return True if a tool should appear in the per-turn tool list.

    Visible = ``core``, OR explicitly discovered via ``tool_search``
    earlier in the same task. When no discovery context is bound (tools
    invoked outside an agent-loop run, direct test calls, etc.), falls
    back to True so nothing is filtered out.
    """
    if tool_name in CORE_TOOL_NAMES:
        return True
    discovered = DISCOVERED_TOOLS.get()
    if discovered is None:
        return True  # No task context — treat everything as visible.
    return tool_name in discovered


def mark_discovered(tool_name: str) -> None:
    """Record that the model has surfaced ``tool_name`` for this task.

    Mutates the active discovery set if one is bound; no-op otherwise.
    """
    discovered = DISCOVERED_TOOLS.get()
    if discovered is None:
        return
    if tool_name not in discovered:
        logger.debug("tool_tiering: marking %r as discovered", tool_name)
    discovered.add(tool_name)


@contextmanager  # pyright: ignore[reportDeprecated]
def bind_discovered_tools() -> Iterator[set[str]]:
    """Bind a fresh per-task discovery set for the duration of the with-block.

    Usage::

        with bind_discovered_tools():
            # tool_search → mark_discovered → backend filtering all see
            # the same per-task set.
            ...
    """
    discovered: set[str] = set()
    token = DISCOVERED_TOOLS.set(discovered)
    try:
        yield discovered
    finally:
        # Async generators that yield while a ContextVar is bound can be
        # finalised in a different asyncio Context than the one that
        # entered this manager (e.g. when the consumer task is cancelled
        # and Python invokes ``aclose`` on the generator from a cleanup
        # context). ``ContextVar.reset`` raises ValueError in that case.
        # The token simply doesn't apply in the closing context, so it's
        # safe to swallow — the original context's binding is gone with
        # its frame anyway.
        try:
            DISCOVERED_TOOLS.reset(token)
        except ValueError:
            logger.debug(
                "DISCOVERED_TOOLS.reset: token not from this context — "
                "the with-block's frame is already gone, no-op",
                exc_info=True,
            )


def filter_visible(tools: list[ToolSpec]) -> list[ToolSpec]:
    """Return only tools that should be in the per-turn payload.

    Preserves order. Used by backends that send the tool list with every
    API call (OpenAI Responses, etc.) to drop deferred-and-undiscovered
    tools from each request, while keeping core + discovered ones.
    """
    return [t for t in tools if is_visible(t.name)]


def deferred_listing(deferred: list[ToolSpec], max_per_line: int = 120) -> str:
    """Render a compact name + short-description listing for deferred tools.

    Format::

        - tool_name — short description (one line, truncated)

    The leading instruction line tells the model to use ``tool_search``
    to retrieve full schemas before calling these tools.
    """
    if not deferred:
        return ""
    lines = [
        "## Discoverable Tools (use `tool_search` to load schemas)",
        "",
        (
            "These tools are available but NOT loaded into your prompt to "
            "save context. To use any of them, first call "
            "`tool_search(query='keyword')` or `tool_search(query='select:tool_name')` "
            "— it returns the full JSON schema. Then call the tool by its "
            "exact name."
        ),
        "",
    ]
    for spec in deferred:
        first_line = (spec.description or "").split("\n", 1)[0]
        if len(first_line) > max_per_line:
            first_line = first_line[: max_per_line - 1].rstrip() + "…"
        lines.append(f"- `{spec.name}` — {first_line}")
    return "\n".join(lines)
