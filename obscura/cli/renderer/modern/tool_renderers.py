"""obscura.cli.renderer.modern.tool_renderers — Per-tool ANSI line renderers.

Each renderer returns ``list[str] | None`` — pre-formatted ANSI lines
that the ``ModernRenderer`` commits directly to the terminal.  Returns
``None`` to fall through to the generic one-line summary.
"""

from __future__ import annotations

import re
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

    lines: list[str] = []
    for raw_line in raw.split("\n")[:30]:
        stripped = raw_line.strip()
        if stripped.startswith("+"):
            lines.append(_styled(f"  + {_sanitize(stripped[1:])}"[:width], _S_ADD))
        elif stripped.startswith("-"):
            lines.append(_styled(f"  - {_sanitize(stripped[1:])}"[:width], _S_DEL))
        elif stripped:
            lines.append(_styled(f"    {_sanitize(stripped)}"[:width], _S_CTX))

    return lines if lines else None


# ---------------------------------------------------------------------------
# Read — code block with line numbers
# ---------------------------------------------------------------------------


def _read_result_lines(event: AgentEvent, width: int) -> list[str] | None:
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    raw_lines = raw.split("\n")
    truncated = len(raw_lines) > 50
    display_lines = raw_lines[:50]

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
        lines.append(_styled(f"  ... ({len(raw_lines) - 50} more lines)", _S_CTX))

    return lines if lines else None


# ---------------------------------------------------------------------------
# Grep — grouped search results
# ---------------------------------------------------------------------------


def _grep_result_lines(event: AgentEvent, width: int) -> list[str] | None:
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    # Parse "filepath:line:content" format
    groups: dict[str, list[tuple[int, str]]] = {}
    for raw_line in raw.split("\n")[:100]:
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
            return [_styled(f"  {f}"[:width], _S_FILE) for f in files[:30]]
        return None

    lines: list[str] = []
    for fpath, matches in groups.items():
        lines.append(_styled(f"  {fpath}", _S_FILE))
        for lineno, content in matches[:10]:
            prefix = _styled(f"    {lineno:>4}: ", _S_LINE)
            lines.append(f"{prefix}{content}"[:width])
        if len(matches) > 10:
            lines.append(_styled(f"    ... ({len(matches) - 10} more)", _S_CTX))

    return lines


# ---------------------------------------------------------------------------
# Directory listing — indented tree
# ---------------------------------------------------------------------------


def _dir_result_lines(event: AgentEvent, width: int) -> list[str] | None:
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    lines: list[str] = []
    for raw_line in raw.split("\n")[:50]:
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
    truncated = len(raw_lines) > 40
    display = raw_lines[:40]

    lines = [f"  {_sanitize(ln)}"[:width] for ln in display]
    if truncated:
        lines.append(_styled(f"  ... ({len(raw_lines) - 40} more lines)", _S_CTX))

    return lines


# ---------------------------------------------------------------------------
# Git diff — colored diff
# ---------------------------------------------------------------------------


def _git_diff_result_lines(event: AgentEvent, width: int) -> list[str] | None:
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    lines: list[str] = []
    for raw_line in raw.split("\n")[:80]:
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
    for raw_line in raw.split("\n")[:40]:
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
        self.register("read_text_file", _read_result_lines)
        self.register("grep_files", _grep_result_lines)
        self.register("find_files", _grep_result_lines)
        self.register("list_directory", _dir_result_lines)
        self.register("tree_directory", _dir_result_lines)
        self.register("run_shell", _shell_result_lines)
        self.register("run_command", _shell_result_lines)
        self.register("git", _git_result_lines)
