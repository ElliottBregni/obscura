"""obscura.cli.renderer.modern.tool_renderers — Per-tool rendering registry.

Maps tool names to specialized component factories that produce richer
output than the generic one-line summary.  Falls back to a generic
``ToolCallComponent`` for tools without a dedicated renderer.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from obscura.cli.renderer.modern.components import (
    CodeBlockComponent,
    Component,
    DiffComponent,
    DiffLine,
    FileTreeComponent,
    SearchResultsComponent,
    ToolCallComponent,
)
from obscura.core.types import AgentEvent

# Type aliases for renderer functions
CallRendererFn = Callable[[AgentEvent], Component | None]
ResultRendererFn = Callable[[AgentEvent], Component | None]


def _sanitize(s: str) -> str:
    """Strip ANSI escapes."""
    if not s:
        return ""
    return re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", s)


def _path_basename(args: dict[str, Any], key: str = "path") -> str:
    """Extract filename from a path argument."""
    raw = args.get(key, args.get("file_path", ""))
    if not raw:
        return ""
    return str(raw).rsplit("/", 1)[-1]


def _trunc(s: str, n: int = 60) -> str:
    s = s.replace("\n", " ").strip()
    return s[:n] + "..." if len(s) > n else s


# ---------------------------------------------------------------------------
# Call renderers (tool invocation display)
# ---------------------------------------------------------------------------


def _edit_call(event: AgentEvent) -> Component | None:
    args = event.tool_input
    old = _trunc(args.get("old_string", ""), 40)
    new = _trunc(args.get("new_string", ""), 40)
    summary = f"Edit {_path_basename(args, 'file_path')}"
    if old:
        summary += f"  {old} → {new}"
    return ToolCallComponent(summary=_sanitize(summary), status="running")


def _read_call(event: AgentEvent) -> Component | None:
    args = event.tool_input
    name = _path_basename(args, "file_path")
    offset = args.get("offset", "")
    limit = args.get("limit", "")
    summary = f"Read {name}"
    if offset or limit:
        summary += f" [{offset}:{limit}]" if limit else f" [{offset}:]"
    return ToolCallComponent(summary=_sanitize(summary), status="running")


def _write_call(event: AgentEvent) -> Component | None:
    args = event.tool_input
    name = _path_basename(args, "file_path")
    return ToolCallComponent(summary=f"Write {_sanitize(name)}", status="running")


def _grep_call(event: AgentEvent) -> Component | None:
    args = event.tool_input
    pattern = _trunc(args.get("pattern", ""), 40)
    path = args.get("path", ".")
    return ToolCallComponent(
        summary=f"Grep /{_sanitize(pattern)}/ in {_sanitize(str(path))}",
        status="running",
    )


def _shell_call(event: AgentEvent) -> Component | None:
    args = event.tool_input
    cmd = _trunc(args.get("command", ""), 60)
    return ToolCallComponent(summary=f"$ {_sanitize(cmd)}", status="running")


def _git_status_call(event: AgentEvent) -> Component | None:
    return ToolCallComponent(summary="git status", status="running")


def _git_diff_call(event: AgentEvent) -> Component | None:
    args = event.tool_input
    ref = args.get("ref", "")
    summary = "git diff"
    if ref:
        summary += f" {ref}"
    return ToolCallComponent(summary=_sanitize(summary), status="running")


def _web_fetch_call(event: AgentEvent) -> Component | None:
    args = event.tool_input
    url = _trunc(args.get("url", ""), 50)
    return ToolCallComponent(summary=f"Fetch {_sanitize(url)}", status="running")


def _web_search_call(event: AgentEvent) -> Component | None:
    args = event.tool_input
    query = _trunc(args.get("query", ""), 50)
    return ToolCallComponent(summary=f"Search: {_sanitize(query)}", status="running")


# ---------------------------------------------------------------------------
# Result renderers (tool output display)
# ---------------------------------------------------------------------------


def _edit_result(event: AgentEvent) -> Component | None:
    """Parse edit result and display as a mini diff."""
    raw = event.tool_result or ""
    if event.is_error:
        return None  # let fallback handle errors

    # Try to parse structured diff from the result
    lines: list[DiffLine] = []
    for line in raw.split("\n")[:30]:  # cap at 30 lines
        stripped = line.strip()
        if stripped.startswith("+"):
            lines.append(DiffLine(tag="+", content=stripped[1:].strip()))
        elif stripped.startswith("-"):
            lines.append(DiffLine(tag="-", content=stripped[1:].strip()))
        elif stripped:
            lines.append(DiffLine(tag=" ", content=stripped))

    if lines:
        return DiffComponent(lines=lines)

    # Fallback: show as success text
    snippet = _sanitize(raw[:120]).replace("\n", " ")
    return ToolCallComponent(summary=snippet, status="done")


def _read_result(event: AgentEvent) -> Component | None:
    """Display file content with line numbers."""
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    # Detect language from content
    language = ""
    first_line = raw.split("\n", 1)[0].strip()
    if first_line.startswith(("def ", "class ", "import ", "from ", "async def ")):
        language = "python"
    elif first_line.startswith(
        ("const ", "let ", "var ", "function ", "export ", "import "),
    ):
        language = "javascript"
    elif first_line.startswith(("{", "[")):
        language = "json"

    # Truncate long output
    lines = raw.split("\n")
    truncated = len(lines) > 50
    display = "\n".join(lines[:50])
    if truncated:
        display += f"\n... ({len(lines) - 50} more lines)"

    return CodeBlockComponent(
        code=_sanitize(display),
        language=language,
        show_line_numbers=True,
    )


def _grep_result(event: AgentEvent) -> Component | None:
    """Parse grep output into grouped search results."""
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    # Parse grep-style output: "filepath:line:content"
    results: dict[str, list[tuple[int, str]]] = {}
    for line in raw.split("\n")[:100]:  # cap at 100 matches
        match = re.match(r"^(.+?):(\d+):(.*)$", line)
        if match:
            fpath, lineno, content = match.groups()
            results.setdefault(fpath, []).append(
                (int(lineno), _sanitize(content.strip())),
            )

    if results:
        result_list = list(results.items())
        return SearchResultsComponent(results=result_list)

    # Fallback: may be files-only output
    files = [_sanitize(line.strip()) for line in raw.split("\n") if line.strip()]
    if files:
        entries = [(0, f, False) for f in files[:30]]
        return FileTreeComponent(entries=entries)

    return None


def _list_dir_result(event: AgentEvent) -> Component | None:
    """Display directory listing as a file tree."""
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    entries: list[tuple[int, str, bool]] = []
    for line in raw.split("\n")[:50]:
        stripped = line.strip()
        if not stripped:
            continue
        # Detect directory entries (commonly end with /)
        is_dir = stripped.endswith("/")
        name = stripped.rstrip("/")
        # Estimate depth from leading whitespace
        depth = (len(line) - len(line.lstrip())) // 2
        entries.append((depth, _sanitize(name), is_dir))

    if entries:
        return FileTreeComponent(entries=entries)
    return None


def _shell_result(event: AgentEvent) -> Component | None:
    """Display shell output with syntax highlighting."""
    raw = event.tool_result or ""
    if event.is_error:
        return None  # let fallback handle

    if not raw.strip():
        return ToolCallComponent(summary="(no output)", status="done")

    # Truncate long output
    lines = raw.split("\n")
    truncated = len(lines) > 40
    display = "\n".join(lines[:40])
    if truncated:
        display += f"\n... ({len(lines) - 40} more lines)"

    return CodeBlockComponent(
        code=_sanitize(display),
        language="bash",
        show_line_numbers=False,
    )


def _git_diff_result(event: AgentEvent) -> Component | None:
    """Display git diff as colored diff component."""
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    diff_lines: list[DiffLine] = []
    for line in raw.split("\n")[:80]:
        if line.startswith("+") and not line.startswith("+++"):
            diff_lines.append(DiffLine(tag="+", content=_sanitize(line[1:])))
        elif line.startswith("-") and not line.startswith("---"):
            diff_lines.append(DiffLine(tag="-", content=_sanitize(line[1:])))
        elif line.startswith("@@") or not line.startswith(
            ("diff ", "index ", "--- ", "+++ "),
        ):
            diff_lines.append(DiffLine(tag=" ", content=_sanitize(line)))

    if diff_lines:
        return DiffComponent(lines=diff_lines)
    return None


def _git_status_result(event: AgentEvent) -> Component | None:
    """Display git status with colored indicators."""
    raw = event.tool_result or ""
    if event.is_error or not raw.strip():
        return None

    return CodeBlockComponent(
        code=_sanitize(raw[:2000]),
        language="",
        show_line_numbers=False,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRendererRegistry:
    """Registry mapping tool names to specialized component factories.

    Falls back to ``None`` (generic rendering) for tools without a
    dedicated renderer.
    """

    def __init__(self) -> None:
        self._call_renderers: dict[str, CallRendererFn] = {}
        self._result_renderers: dict[str, ResultRendererFn] = {}
        self._register_defaults()

    def register(
        self,
        tool_name: str,
        *,
        call_renderer: CallRendererFn | None = None,
        result_renderer: ResultRendererFn | None = None,
    ) -> None:
        if call_renderer is not None:
            self._call_renderers[tool_name] = call_renderer
        if result_renderer is not None:
            self._result_renderers[tool_name] = result_renderer

    def render_call(self, event: AgentEvent) -> Component | None:
        """Render a tool call event.  Returns None for generic fallback."""
        fn = self._call_renderers.get(event.tool_name)
        if fn is not None:
            try:
                return fn(event)
            except Exception:
                return None
        return None

    def render_result(self, event: AgentEvent) -> Component | None:
        """Render a tool result event.  Returns None for generic fallback."""
        fn = self._result_renderers.get(event.tool_name)
        if fn is not None:
            try:
                return fn(event)
            except Exception:
                return None
        return None

    def _register_defaults(self) -> None:
        """Wire up the built-in per-tool renderers."""
        # File operations
        self.register(
            "edit_text_file",
            call_renderer=_edit_call,
            result_renderer=_edit_result,
        )
        self.register(
            "read_text_file",
            call_renderer=_read_call,
            result_renderer=_read_result,
        )
        self.register("write_text_file", call_renderer=_write_call)

        # Search
        self.register(
            "grep_files",
            call_renderer=_grep_call,
            result_renderer=_grep_result,
        )
        self.register(
            "find_files",
            result_renderer=_grep_result,
        )  # similar output format

        # Directory listing
        self.register("list_directory", result_renderer=_list_dir_result)
        self.register("tree_directory", result_renderer=_list_dir_result)

        # Shell
        self.register(
            "run_shell",
            call_renderer=_shell_call,
            result_renderer=_shell_result,
        )
        self.register(
            "run_command",
            call_renderer=_shell_call,
            result_renderer=_shell_result,
        )

        # Git
        self.register(
            "git_status",
            call_renderer=_git_status_call,
            result_renderer=_git_status_result,
        )
        self.register(
            "git_diff",
            call_renderer=_git_diff_call,
            result_renderer=_git_diff_result,
        )

        # Web
        self.register("web_fetch", call_renderer=_web_fetch_call)
        self.register("web_search", call_renderer=_web_search_call)
