"""obscura.cli.tool_summaries -- Human-readable one-liners for tool calls."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal
import logging

logger = logging.getLogger(__name__)


# Categorical "kind" used by both the bordered REPL and the TUI to
# colour the tool-call indicator differently per source. Keeps native
# tools, shell calls, plugin tools, MCP shadows, and sub-agent
# delegations visually distinguishable in long sessions.
ToolKind = Literal["native", "shell", "mcp", "plugin", "delegation"]


def classify_tool(tool_name: str) -> ToolKind:
    """Bucket ``tool_name`` into a render-friendly category.

    The categories are visual hints, not semantic guarantees — the
    rule of thumb is "what colour should the ``⏺`` glyph be". Order
    matters: MCP and delegation are checked first because their names
    overlap with the plugin pattern (underscore-separated).
    """
    if tool_name.startswith("mcp__"):
        return "mcp"
    if tool_name in {"task", "spawn_agents", "spawn_subagent"}:
        return "delegation"
    if tool_name in {"run_shell", "run_command", "bash", "shell"}:
        return "shell"
    if tool_name in _SUMMARIES:
        return "native"
    # Plugin tools are conventionally namespaced as ``<plugin>_<tool>``
    # (e.g. ``fv-backend_fv_backend_call``, ``jira_jira_call``). The
    # underscore heuristic is fuzzy — anything unrecognised that has a
    # multi-segment name renders as "plugin"; everything else falls
    # back to "native" so the default colour applies.
    if "_" in tool_name and not tool_name.startswith("_"):
        return "plugin"
    return "native"


def summarize_tool_call(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Return a human-readable one-liner for a tool call.

    Examples:
        read_text_file {"path": "/foo/bar.py"}  ->  "Reading bar.py"
        grep_files {"pattern": "TODO", "path": "."}  ->  "Searching for 'TODO'"
        run_shell {"script": "ls -la"}  ->  "$ ls -la"
        edit_text_file {"path": "x.py", ...}  ->  "Editing x.py"
        mcp__supabase__query {"sql": "select 1"}  ->  "supabase.query — select 1"
        fv-backend_fv_backend_call {"path": "/api/x"}  ->  "fv-backend / fv_backend_call — /api/x"

    """
    fn = _SUMMARIES.get(tool_name)
    if fn is not None:
        try:
            return fn(tool_input)
        except Exception:
            logger.debug("suppressed exception in summarize_tool_call", exc_info=True)
    # MCP shadow names are ``mcp__<server>__<tool>``. Render that as
    # ``server.tool`` plus the first arg so the user can tell which
    # MCP fired without parsing the prefix every time.
    if tool_name.startswith("mcp__"):
        rest = tool_name[5:]
        parts = rest.split("__", 1)
        if len(parts) == 2:
            server, tool = parts
            arg_preview = _arg_preview(tool_input)
            label = f"{server}.{tool}"
            return f"{label} — {arg_preview}" if arg_preview else label
    # Plugin-style names are ``<plugin>_<tool>``. Common plugins
    # (jira, postman, fv-backend) repeat the plugin token in the tool
    # name (e.g. ``jira_jira_call``); strip that duplication for a
    # cleaner one-liner so the user sees ``jira / call — METHOD``
    # rather than ``jira_jira_call(method=METHOD)``.
    if "_" in tool_name and not tool_name.startswith("_"):
        plugin, _, tool = tool_name.partition("_")
        # Drop the duplicated ``<plugin>_`` prefix on the tool half.
        if tool.startswith(f"{plugin}_"):
            tool = tool[len(plugin) + 1 :]
        if plugin and tool:
            arg_preview = _arg_preview(tool_input)
            label = f"{plugin} / {tool}"
            return f"{label} — {arg_preview}" if arg_preview else label
    return _fallback(tool_name, tool_input)


def _arg_preview(args: dict[str, Any]) -> str:
    """Best-effort first-argument preview, ≤50 chars.

    Picks the first non-empty string-looking value; falls back to
    ``key=value`` for simpler types so the preview is always useful
    for at-a-glance scanning.
    """
    for k, v in args.items():
        if isinstance(v, str) and v:
            return _trunc(v.split("\n", 1)[0], 50)
        if isinstance(v, (int, float, bool)):
            return f"{k}={v}"
    return ""


def _path_basename(args: dict[str, Any], key: str = "path") -> str:
    raw = str(args.get(key) or args.get("file_path", ""))
    if not raw:
        return ""
    return PurePosixPath(raw).name


def _trunc(s: str, n: int = 50) -> str:
    if len(s) > n:
        return s[: n - 3] + "..."
    return s


def _fallback(name: str, args: dict[str, Any]) -> str:
    parts: list[str] = []
    for k, v in list(args.items())[:2]:
        sv = _trunc(str(v), 40)
        parts.append(f"{k}={sv}")
    return f"{name}({', '.join(parts)})" if parts else name


# ---- per-tool summary functions ----


def _read(a: dict[str, Any]) -> str:
    return f"Reading {_path_basename(a)}" if _path_basename(a) else "Reading file"


def _write(a: dict[str, Any]) -> str:
    return f"Writing {_path_basename(a)}" if _path_basename(a) else "Writing file"


def _edit(a: dict[str, Any]) -> str:
    return f"Editing {_path_basename(a)}" if _path_basename(a) else "Editing file"


def _append(a: dict[str, Any]) -> str:
    return (
        f"Appending to {_path_basename(a)}"
        if _path_basename(a)
        else "Appending to file"
    )


def _list_dir(a: dict[str, Any]) -> str:
    return f"Listing {_path_basename(a) or a.get('path', '.')}"


def _tree_dir(a: dict[str, Any]) -> str:
    return f"Tree {_path_basename(a) or a.get('path', '.')}"


def _grep(a: dict[str, Any]) -> str:
    pat = a.get("pattern", "")
    return f"Searching for '{pat}'" if pat else "Searching files"


def _find(a: dict[str, Any]) -> str:
    pat = a.get("pattern") or a.get("name", "")
    return f"Finding '{pat}'" if pat else "Finding files"


def _shell(a: dict[str, Any]) -> str:
    cmd = str(a.get("script") or a.get("command", ""))
    return f"$ {_trunc(cmd, 60)}" if cmd else "Running shell command"


def _run_cmd(a: dict[str, Any]) -> str:
    cmd = str(a.get("command", ""))
    args = a.get("args", [])
    full = f"{cmd} {' '.join(str(x) for x in args[:3])}".strip()
    return f"$ {_trunc(full, 60)}" if full else "Running command"


def _python(a: dict[str, Any]) -> str:
    code = str(a.get("code", ""))
    first = code.strip().split("\n")[0]
    return f"Running python: {_trunc(first, 50)}" if first else "Running python"


def _web_fetch(a: dict[str, Any]) -> str:
    url = str(a.get("url", ""))
    return f"Fetching {_trunc(url, 50)}" if url else "Fetching URL"


def _web_search(a: dict[str, Any]) -> str:
    q = str(a.get("query", ""))
    return f"Searching web for '{q}'" if q else "Searching web"


def _git(a: dict[str, Any]) -> str:
    sub = str(a.get("subcommand", "")).strip()
    if not sub:
        return "git"
    if sub == "commit":
        msg = str(a.get("message", ""))
        return f'git commit -m "{_trunc(msg, 40)}"' if msg else "git commit"
    if sub == "diff":
        ref = a.get("ref", "")
        return f"git diff {ref}".strip() if ref else "git diff"
    return f"git {sub}"


def _task(a: dict[str, Any]) -> str:
    prompt = str(a.get("prompt", ""))
    return f"Delegating: {_trunc(prompt, 50)}" if prompt else "Delegating task"


def _copy(a: dict[str, Any]) -> str:
    return f"Copying {_path_basename(a, 'source')}"


def _move(a: dict[str, Any]) -> str:
    return f"Moving {_path_basename(a, 'source')}"


def _remove(a: dict[str, Any]) -> str:
    return f"Removing {_path_basename(a)}" if _path_basename(a) else "Removing path"


def _mkdir(a: dict[str, Any]) -> str:
    return (
        f"Creating directory {_path_basename(a)}"
        if _path_basename(a)
        else "Creating directory"
    )


def _file_info(a: dict[str, Any]) -> str:
    return f"Inspecting {_path_basename(a)}" if _path_basename(a) else "Inspecting file"


def _diff_files(_a: dict[str, Any]) -> str:
    return "Diffing files"


def _download(a: dict[str, Any]) -> str:
    url = str(a.get("url", ""))
    return f"Downloading {_trunc(url, 40)}" if url else "Downloading file"


def _http_req(a: dict[str, Any]) -> str:
    method = str(a.get("method", "GET")).upper()
    url = str(a.get("url", ""))
    return f"{method} {_trunc(url, 40)}" if url else f"{method} request"


def _clipboard_read(_a: dict[str, Any]) -> str:
    return "Reading clipboard"


def _clipboard_write(_a: dict[str, Any]) -> str:
    return "Writing to clipboard"


def _context_status(_a: dict[str, Any]) -> str:
    return "Checking context window"


def _todo(_a: dict[str, Any]) -> str:
    return "Updating todos"


def _which(_a: dict[str, Any]) -> str:
    return "Resolving command path"


def _get_env(_a: dict[str, Any]) -> str:
    return "Reading environment"


def _sys_info(_a: dict[str, Any]) -> str:
    return "Getting system info"


def _list_procs(_a: dict[str, Any]) -> str:
    return "Listing processes"


def _signal_proc(a: dict[str, Any]) -> str:
    pid = a.get("pid", "?")
    sig = a.get("signal", "TERM")
    return f"Signaling process {pid} ({sig})"


def _list_ports(_a: dict[str, Any]) -> str:
    return "Listing listening ports"


def _json_query(a: dict[str, Any]) -> str:
    expr = str(a.get("expression", ""))
    return f"JSON query: {_trunc(expr, 40)}" if expr else "JSON query"


def _create_tool(a: dict[str, Any]) -> str:
    name = str(a.get("name", ""))
    return f"Creating tool: {name}" if name else "Creating dynamic tool"


def _call_dynamic(a: dict[str, Any]) -> str:
    name = str(a.get("name", ""))
    return f"Calling {name}" if name else "Calling dynamic tool"


def _list_dynamic(_a: dict[str, Any]) -> str:
    return "Listing dynamic tools"


def _code_sandbox(a: dict[str, Any]) -> str:
    lang = a.get("language", "python")
    return f"Running {lang} sandbox"


def _report_intent(a: dict[str, Any]) -> str:
    intent = _trunc(str(a.get("intent", "")), 50)
    return f"Intent: {intent}" if intent else "Reporting intent"


def _list_system_tools(_a: dict[str, Any]) -> str:
    return "Listing system tools"


def _ask_user(a: dict[str, Any]) -> str:
    q = _trunc(str(a.get("question", "")), 50)
    return f"Asking: {q}" if q else "Asking user"


def _list_unix(_a: dict[str, Any]) -> str:
    return "Listing unix capabilities"


_SUMMARIES: dict[str, Any] = {
    # File operations
    "read_text_file": _read,
    "write_text_file": _write,
    "edit_text_file": _edit,
    "append_text_file": _append,
    # Bash is the Copilot SDK's built-in shell tool; reuse the
    # ``run_shell`` summary so the rendered preview is identical.
    "bash": _shell,
    "shell": _shell,
    "list_directory": _list_dir,
    "tree_directory": _tree_dir,
    "make_directory": _mkdir,
    "remove_path": _remove,
    "copy_path": _copy,
    "move_path": _move,
    "file_info": _file_info,
    "diff_files": _diff_files,
    # Search
    "grep_files": _grep,
    "find_files": _find,
    # Execution
    "run_shell": _shell,
    "run_command": _run_cmd,
    "run_python3": _python,
    "code_sandbox": _code_sandbox,
    # Web
    "web_fetch": _web_fetch,
    "web_search": _web_search,
    "download_file": _download,
    "http_request": _http_req,
    # Git
    "git": _git,
    # Agents / tasks
    "task": _task,
    # System
    "which_command": _which,
    "get_environment": _get_env,
    "get_system_info": _sys_info,
    "list_processes": _list_procs,
    "signal_process": _signal_proc,
    "list_listening_ports": _list_ports,
    "list_unix_capabilities": _list_unix,
    # Clipboard
    "clipboard_read": _clipboard_read,
    "clipboard_write": _clipboard_write,
    # Context / meta
    "context_window_status": _context_status,
    "todo_write": _todo,
    "json_query": _json_query,
    "report_intent": _report_intent,
    "list_system_tools": _list_system_tools,
    "ask_user": _ask_user,
    # Dynamic tools
    "create_tool": _create_tool,
    "call_dynamic_tool": _call_dynamic,
    "list_dynamic_tools": _list_dynamic,
}
