"""obscura.cli.tool_collapse — Collapse consecutive tool calls into summaries.

Bundles consecutive read/grep/glob/search tool calls into single
collapsible summary lines to reduce output noise.

Example output::

    > Read 5 files, Grep 3 searches  (collapsed)
    ✓ read_text_file: src/main.py
    ✓ read_text_file: src/utils.py
    ...

Pattern from claude-code's ``collapseReadSearch.ts``.
"""

from __future__ import annotations

from typing import Any

# Tools that can be collapsed into summary groups.
COLLAPSIBLE_TOOLS: frozenset[str] = frozenset(
    {
        "read_text_file",
        "grep_files",
        "find_files",
        "list_directory",
        "tree_directory",
        "file_info",
        "git_status",
        "git_log",
        "git_diff",
        "web_search",
        "web_fetch",
        "tool_search",
        "which_command",
    },
)

# How to categorize tools for the summary line.
TOOL_CATEGORIES: dict[str, str] = {
    "read_text_file": "Read",
    "grep_files": "Grep",
    "find_files": "Find",
    "list_directory": "List",
    "tree_directory": "Tree",
    "file_info": "Info",
    "git_status": "Git",
    "git_log": "Git",
    "git_diff": "Git",
    "web_search": "Search",
    "web_fetch": "Fetch",
    "tool_search": "Search",
    "which_command": "Which",
}


class ToolCollapser:
    """Tracks consecutive tool calls and generates collapse summaries.

    Usage::

        collapser = ToolCollapser()

        for event in agent_events:
            if event.kind == TOOL_CALL:
                collapser.record(event.tool_name, event.tool_input)
            elif event.kind == TEXT_DELTA:
                if collapser.pending:
                    print(collapser.flush_summary())
                print(event.text)
    """

    def __init__(self) -> None:
        self._group: list[tuple[str, str]] = []  # [(tool_name, detail)]

    def record(self, tool_name: str, tool_input: dict[str, Any] | None = None) -> bool:
        """Record a tool call. Returns True if it was collapsed."""
        if tool_name not in COLLAPSIBLE_TOOLS:
            return False
        detail = _extract_detail(tool_name, tool_input or {})
        self._group.append((tool_name, detail))
        return True

    @property
    def pending(self) -> bool:
        """True if there are collapsed tool calls waiting to be flushed."""
        return len(self._group) > 0

    @property
    def count(self) -> int:
        return len(self._group)

    def flush_summary(self) -> str:
        """Generate a summary line and clear the group.

        Returns a string like: "Read 3 files, Grep 2 searches"
        """
        if not self._group:
            return ""

        # Count by category.
        counts: dict[str, int] = {}
        details: list[str] = []
        for tool_name, detail in self._group:
            cat = TOOL_CATEGORIES.get(tool_name, tool_name)
            counts[cat] = counts.get(cat, 0) + 1
            if detail:
                details.append(f"  {tool_name}: {detail}")

        # Build summary line.
        parts: list[str] = []
        for cat, count in counts.items():
            if count == 1:
                parts.append(cat)
            else:
                parts.append(f"{cat} x{count}")
        summary = ", ".join(parts)

        self._group.clear()
        return summary

    def flush_details(self) -> list[str]:
        """Return individual detail lines for expanded view."""
        lines = [f"  {tool}: {detail}" for tool, detail in self._group]
        self._group.clear()
        return lines

    def reset(self) -> None:
        """Clear without generating summary."""
        self._group.clear()


def _extract_detail(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Extract a short detail string from tool input for display."""
    if tool_name == "read_text_file":
        return str(tool_input.get("path", ""))
    if tool_name in ("grep_files", "find_files"):
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"{pattern} in {path}" if path else str(pattern)
    if tool_name in ("list_directory", "tree_directory"):
        return str(tool_input.get("path", ""))
    if tool_name == "web_search":
        return str(tool_input.get("query", ""))
    if tool_name == "web_fetch":
        return str(tool_input.get("url", ""))[:60]
    if tool_name in ("git_status", "git_log", "git_diff"):
        return ""
    return ""
