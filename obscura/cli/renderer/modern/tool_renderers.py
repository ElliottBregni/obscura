"""obscura.cli.renderer.modern.tool_renderers — Per-tool ANSI line renderers.

Each renderer returns ``list[str] | None`` — pre-formatted ANSI lines
that the ``ModernRenderer`` commits directly to the terminal.  Returns
``None`` to fall through to the generic one-line summary.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, cast
from collections.abc import Callable

from obscura.cli.renderer.modern.theme import (
    ACCENT,
    ERROR_COLOR,
    MUTED,
    OK_COLOR,
    RESET,
    Style,
)
from obscura.core.types import AgentEvent

# Renderer function type: event + terminal width → ANSI lines or None
ResultLinesFn = Callable[[AgentEvent, int], list[str] | None]

# Configurable output cap — set OBSCURA_TOOL_OUTPUT_MAX_LINES to override.
# 0 = unlimited.
_MAX_LINES = int(os.environ.get("OBSCURA_TOOL_OUTPUT_MAX_LINES", "80"))


def _sanitize(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", s)


def _styled(text: str, style: Style) -> str:
    if style.fg < 0 and not style.bold and not style.dim and not style.italic:
        return text
    return f"{style.ansi()}{text}{RESET}"


_S_ADD = Style(fg=OK_COLOR)
_S_DEL = Style(fg=ERROR_COLOR)
_S_CTX = Style(fg=MUTED, dim=True)
_S_FILE = Style(fg=ACCENT, bold=True)
_S_LINE = Style(fg=MUTED, dim=True)
_S_CODE = Style(fg=250)  # light gray


# ---------------------------------------------------------------------------
# Edit — colored diff
# ---------------------------------------------------------------------------


def _edit_result_lines(event: AgentEvent, width: int) -> list[str] | None:
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    # Tool returns JSON with structured diff — parse it.
    try:
        data_raw: Any = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data_raw, dict):
        return None
    data = cast(dict[str, Any], data_raw)
    if not data.get("ok"):
        return None

    diff_raw: Any = data.get("diff")
    if not isinstance(diff_raw, dict):
        return None
    diff = cast(dict[str, Any], diff_raw)

    path = str(data.get("path", ""))
    summary = str(diff.get("summary", ""))
    hunks_raw: Any = diff.get("hunks", [])
    hunks: list[dict[str, Any]] = (
        [cast(dict[str, Any], h) for h in cast(list[Any], hunks_raw) if isinstance(h, dict)]
        if isinstance(hunks_raw, list)
        else []
    )

    lines: list[str] = []

    # File header
    if path:
        header = f"  {path}"
        if summary:
            header += f"  ({summary})"
        lines.append(_styled(header, _S_FILE))

    # Render each hunk
    cap = _MAX_LINES or float("inf")
    total_lines = 0
    for hunk in hunks:
        hunk_header = str(hunk.get("header", ""))
        if hunk_header:
            lines.append(_styled(f"  {hunk_header}", _S_CTX))
        hunk_lines_raw: Any = hunk.get("lines", [])
        hunk_lines: list[str] = (
            [str(line) for line in cast(list[Any], hunk_lines_raw)]
            if isinstance(hunk_lines_raw, list)
            else []
        )
        for diff_line in hunk_lines:
            if total_lines >= cap:
                remaining = (
                    sum(
                        len(cast(list[Any], h.get("lines", [])))
                        if isinstance(h.get("lines"), list)
                        else 0
                        for h in hunks
                    )
                    - total_lines
                )
                if remaining > 0:
                    lines.append(_styled(f"  ... ({remaining} more lines)", _S_CTX))
                break
            if diff_line.startswith("+"):
                lines.append(_styled(f"  + {_sanitize(diff_line[1:])}"[:width], _S_ADD))
            elif diff_line.startswith("-"):
                lines.append(_styled(f"  - {_sanitize(diff_line[1:])}"[:width], _S_DEL))
            else:
                lines.append(_styled(f"    {_sanitize(diff_line)}"[:width], _S_CTX))
            total_lines += 1
        else:
            continue
        break  # hit the 60-line cap

    return lines if lines else None


# ---------------------------------------------------------------------------
# Read — code block with line numbers
# ---------------------------------------------------------------------------


def _read_result_lines(event: AgentEvent, width: int) -> list[str] | None:
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    raw_lines = raw.split("\n")
    cap = _MAX_LINES or len(raw_lines)
    truncated = len(raw_lines) > cap
    display_lines = raw_lines[:cap]

    # Gutter width
    max_ln = len(display_lines)
    gutter_w = len(str(max_ln)) + 2

    lines: list[str] = []
    for i, content in enumerate(display_lines, 1):
        ln = str(i).rjust(gutter_w - 2)
        gutter = _styled(f"{ln}: ", _S_LINE)
        code = _sanitize(content)[: width - gutter_w]
        lines.append(f"  {gutter}{code}")

    if truncated:
        lines.append(_styled(f"  ... ({len(raw_lines) - cap} more lines)", _S_CTX))

    return lines if lines else None


# ---------------------------------------------------------------------------
# Grep — grouped search results
# ---------------------------------------------------------------------------


def _grep_result_lines(event: AgentEvent, width: int) -> list[str] | None:
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    # Parse "filepath:line:content" format
    cap = _MAX_LINES or 500
    groups: dict[str, list[tuple[int, str]]] = {}
    for raw_line in raw.split("\n")[:cap]:
        m = re.match(r"^(.+?):(\d+):(.*)$", raw_line)
        if m:
            fpath, lineno, content = m.groups()
            groups.setdefault(fpath, []).append(
                (int(lineno), _sanitize(content.strip()))
            )

    if not groups:
        # Files-only output
        files = [_sanitize(ln.strip()) for ln in raw.split("\n") if ln.strip()]
        if files:
            return [_styled(f"  {f}"[:width], _S_FILE) for f in files[:cap]]
        return None

    per_file = max(cap // max(len(groups), 1), 5)
    lines: list[str] = []
    for fpath, matches in groups.items():
        lines.append(_styled(f"  {fpath}", _S_FILE))
        for lineno, content in matches[:per_file]:
            prefix = _styled(f"    {lineno:>4}: ", _S_LINE)
            lines.append(f"{prefix}{content}"[:width])
        if len(matches) > per_file:
            lines.append(_styled(f"    ... ({len(matches) - per_file} more)", _S_CTX))

    return lines


# ---------------------------------------------------------------------------
# Directory listing — indented tree
# ---------------------------------------------------------------------------


def _dir_result_lines(event: AgentEvent, width: int) -> list[str] | None:
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    lines: list[str] = []
    cap = _MAX_LINES or 500
    for raw_line in raw.split("\n")[:cap]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        is_dir = stripped.endswith("/")
        name = stripped.rstrip("/")
        depth = (len(raw_line) - len(raw_line.lstrip())) // 2
        indent = "  " * depth
        if is_dir:
            lines.append(_styled(f"  {indent}{name}/", _S_FILE))
        else:
            lines.append(f"  {indent}{name}"[:width])

    return lines if lines else None


# ---------------------------------------------------------------------------
# Shell — output block
# ---------------------------------------------------------------------------


def _shell_result_lines(event: AgentEvent, width: int) -> list[str] | None:
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    raw_lines = raw.split("\n")
    cap = _MAX_LINES or len(raw_lines)
    truncated = len(raw_lines) > cap
    display = raw_lines[:cap]

    lines = [f"  {_sanitize(ln)}"[:width] for ln in display]
    if truncated:
        lines.append(_styled(f"  ... ({len(raw_lines) - cap} more lines)", _S_CTX))

    return lines


# ---------------------------------------------------------------------------
# Git diff — colored diff
# ---------------------------------------------------------------------------


def _git_diff_result_lines(event: AgentEvent, width: int) -> list[str] | None:
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    lines: list[str] = []
    cap = _MAX_LINES or 500
    for raw_line in raw.split("\n")[:cap]:
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            lines.append(_styled(f"  {_sanitize(raw_line)}"[:width], _S_ADD))
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            lines.append(_styled(f"  {_sanitize(raw_line)}"[:width], _S_DEL))
        elif raw_line.startswith("@@"):
            lines.append(_styled(f"  {_sanitize(raw_line)}"[:width], Style(fg=ACCENT)))
        elif not raw_line.startswith(("diff ", "index ", "--- ", "+++ ")):
            lines.append(f"  {_sanitize(raw_line)}"[:width])

    return lines if lines else None


# ---------------------------------------------------------------------------
# Git status
# ---------------------------------------------------------------------------


def _git_status_result_lines(event: AgentEvent, width: int) -> list[str] | None:
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    lines: list[str] = []
    cap = _MAX_LINES or 500
    for raw_line in raw.split("\n")[:cap]:
        sanitized = _sanitize(raw_line)
        if sanitized.startswith("M ") or sanitized.startswith(" M"):
            lines.append(_styled(f"  {sanitized}"[:width], Style(fg=OK_COLOR)))
        elif sanitized.startswith("??"):
            lines.append(_styled(f"  {sanitized}"[:width], Style(fg=MUTED)))
        elif sanitized.startswith("D "):
            lines.append(_styled(f"  {sanitized}"[:width], _S_DEL))
        elif sanitized.startswith("A "):
            lines.append(_styled(f"  {sanitized}"[:width], _S_ADD))
        else:
            lines.append(f"  {sanitized}"[:width])

    return lines if lines else None


# ---------------------------------------------------------------------------
# Unified git dispatcher
# ---------------------------------------------------------------------------


def _git_result_lines(event: AgentEvent, width: int) -> list[str] | None:
    action = event.tool_input.get("action", "")
    if action == "diff":
        return _git_diff_result_lines(event, width)
    if action == "status":
        return _git_status_result_lines(event, width)
    return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRendererRegistry:
    """Maps tool names to line-based result renderers."""

    def __init__(self) -> None:
        self._renderers: dict[str, ResultLinesFn] = {}
        self._register_defaults()

    def register(self, tool_name: str, renderer: ResultLinesFn) -> None:
        self._renderers[tool_name] = renderer

    def render_result_lines(
        self,
        event: AgentEvent,
        width: int,
    ) -> list[str] | None:
        """Render a tool result.  Returns None for generic fallback."""
        fn = self._renderers.get(event.tool_name)
        if fn is not None:
            try:
                return fn(event, width)
            except Exception:
                return None
        return None

    def _register_defaults(self) -> None:
        self.register("edit_text_file", _edit_result_lines)
        self.register("write_text_file", _edit_result_lines)
        self.register("append_text_file", _edit_result_lines)
        self.register("read_text_file", _read_result_lines)
        self.register("grep_files", _grep_result_lines)
        self.register("find_files", _grep_result_lines)
        self.register("list_directory", _dir_result_lines)
        self.register("tree_directory", _dir_result_lines)
        self.register("run_shell", _shell_result_lines)
        self.register("run_command", _shell_result_lines)
        self.register("git", _git_result_lines)
