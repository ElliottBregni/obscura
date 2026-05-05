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

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from obscura.core.types import ToolSpec


logger = logging.getLogger(__name__)


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


@contextmanager
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
        DISCOVERED_TOOLS.reset(token)


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
