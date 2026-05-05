"""Tool presentation tiering — keep the system prompt focused.

Obscura registers 100+ tools in a fully-loaded session (system tools,
plugin integrations, MCP-discovered shadows). Listing every tool's full
description in the system prompt overloads the model — gpt-5.3-codex
in particular shows worse tool-selection accuracy when the prompt
exceeds ~80 tools, and pays a real token cost for every spec.

Tiering splits tools into two presentation classes:

* **core** — the small set the model uses constantly (filesystem, shell,
  web fetch/search, delegation, planning). Full schemas appear in the
  system prompt. ~15 tools.
* **deferred** — everything else (plugin/integration tools, niche system
  tools, MCP shadows). Names + 1-line descriptions appear in a
  "Discoverable via tool_search" section. The model finds them by
  calling :func:`tool_search`, which returns full schemas on demand.

This is a **presentation-only** change — every registered tool is still
callable. The tier just controls how prominently the model sees it.

To customise: edit :data:`CORE_TOOL_NAMES`. Names not in the set are
deferred. Backends call :func:`split_by_tier` from their
``_build_tool_listing`` methods.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec


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
