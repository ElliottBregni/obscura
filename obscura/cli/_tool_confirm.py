"""obscura.cli._tool_confirm — Tool-call confirmation and file-change tracking.

Extracted from ``obscura/cli/__init__.py``.  These helpers are called from
``send_message`` and ``_repl_loop`` to gate tool execution and record file
diffs for ``/diff``.

Public API
----------
cli_confirm(ctx, tool_name, tool_input) -> bool
    Prompt the user via TUI widget; returns True to allow the call.

track_file_event(event_kind, ctx, ev) -> None
    Record before/after content for file-write tool calls.

maybe_parse_plan(response_text, ctx) -> None
    If in PLAN mode, parse a structured plan from the assistant response.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from obscura.core.types import AgentEventKind

if TYPE_CHECKING:
    from obscura.cli.commands import REPLContext

_log = logging.getLogger("obscura.cli")


async def cli_confirm(
    ctx: REPLContext,
    tool_name: str,
    tool_input: dict[str, Any],
) -> bool:
    """Prompt user to approve a tool call via TUI widget. Returns True to allow."""
    if tool_name in ctx.confirm_always:
        return True

    from obscura.cli.widgets import ToolConfirmRequest, confirm_tool

    result = await confirm_tool(
        ToolConfirmRequest(tool_name=tool_name, tool_input=tool_input),
    )

    if result.action == "always_allow":
        ctx.confirm_always.add(tool_name)
        return True
    return result.action == "allow"


def track_file_event(
    event: AgentEventKind,
    ctx: REPLContext,
    ev: Any,
    *,
    file_write_tools: frozenset[str] | None = None,
) -> None:
    """Track file modifications for /diff.

    Parameters
    ----------
    event:
        The ``AgentEventKind`` for the current event (kept for API compat).
    ctx:
        The active :class:`REPLContext` holding ``pending_file_reads`` and
        ``add_file_change``.
    ev:
        The raw event object with ``.kind``, ``.tool_name``, ``.tool_input``,
        ``.tool_use_id``, and ``.tool_result``.
    file_write_tools:
        Set of tool names that write files.  Falls back to
        ``_FILE_WRITE_TOOLS`` from ``obscura.cli.commands``.
    """
    if file_write_tools is None:
        try:
            from obscura.cli.commands import _FILE_WRITE_TOOLS  # pyright: ignore[reportPrivateUsage]

            file_write_tools = frozenset(_FILE_WRITE_TOOLS)
        except Exception:
            file_write_tools = frozenset()

    if ev.kind == AgentEventKind.TOOL_CALL and ev.tool_name in file_write_tools:
        path = ev.tool_input.get("path") or ev.tool_input.get("file_path", "")
        if path:
            try:
                before = Path(path).read_text()
            except (FileNotFoundError, OSError):
                before = ""
            ctx.pending_file_reads[ev.tool_use_id] = (path, before)

    elif (
        ev.kind == AgentEventKind.TOOL_RESULT
        and ev.tool_use_id in ctx.pending_file_reads
    ):
        path, before = ctx.pending_file_reads.pop(ev.tool_use_id)
        try:
            after = Path(path).read_text()
        except (FileNotFoundError, OSError):
            after = ""
        if after != before:
            ctx.add_file_change(path, before, after)
            # Record attribution.
            try:
                from obscura.core.commit_attribution import get_attribution_tracker

                added = len(after.splitlines()) - len(before.splitlines())
                if added >= 0:
                    get_attribution_tracker().record_agent_edit(path, lines_added=added)
                else:
                    get_attribution_tracker().record_agent_edit(
                        path,
                        lines_removed=abs(added),
                    )
            except Exception:
                pass
            # Record in file history.
            try:
                from obscura.tools.system.file_state import record_file_access

                record_file_access(Path(path), "edit")
            except Exception:
                pass


def maybe_parse_plan(response_text: str, ctx: REPLContext) -> None:
    """If in PLAN mode, attempt to parse a structured plan from the response."""
    mm = ctx.mode_manager
    if mm is None:
        return

    from obscura.cli.app.modes import TUIMode

    if mm.current != TUIMode.PLAN:
        return

    if not response_text.strip():
        return

    from obscura.cli.app.modes import Plan
    from obscura.cli.render import render_plan

    plan = Plan.parse(response_text)
    if plan.steps:
        mm.active_plan = plan
        render_plan(plan)
