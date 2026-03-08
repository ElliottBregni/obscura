"""obscura.cli.tool_summaries -- Human-readable one-liners for tool calls."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


def summarize_tool_call(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Return a human-readable one-liner for a tool call.

    Examples:
        read_text_file {"path": "/foo/bar.py"}  ->  "Reading bar.py"
        grep_files {"pattern": "TODO", "path": "."}  ->  "Searching for 'TODO'"
        run_shell {"script": "ls -la"}  ->  "$ ls -la"
        edit_text_file {"path": "x.py", ...}  ->  "Editing x.py"
    """
    fn = _SUMMARIES.get(tool_name)
    if fn is not None:
        try:
            return fn(tool_input)
        except Exception:
            pass
    return _fallback(tool_name, tool_input)


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
    return f"Appending to {_path_basename(a)}" if _path_basename(a) else "Appending to file"


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


def _run_npx(a: dict[str, Any]) -> str:
    cmd = str(a.get("command", ""))
    args = a.get("args", [])
    full = f"npx {cmd} {' '.join(str(x) for x in args[:3])}".strip()
    return f"$ {_trunc(full, 60)}" if full else "$ npx"


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


def _git_status(_a: dict[str, Any]) -> str:
    return "git status"


def _git_diff(a: dict[str, Any]) -> str:
    ref = a.get("ref", "")
    return f"git diff {ref}".strip() if ref else "git diff"


def _git_log(_a: dict[str, Any]) -> str:
    return "git log"


def _git_commit(a: dict[str, Any]) -> str:
    msg = str(a.get("message", ""))
    return f'git commit -m "{_trunc(msg, 40)}"' if msg else "git commit"


def _git_branch(_a: dict[str, Any]) -> str:
    return "git branch"


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
    return f"Creating directory {_path_basename(a)}" if _path_basename(a) else "Creating directory"


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


def _copilot_query(a: dict[str, Any]) -> str:
    q = str(a.get("prompt") or a.get("query", ""))
    return f"Asking Copilot: {_trunc(q, 40)}" if q else "Querying Copilot"


def _which(_a: dict[str, Any]) -> str:
    return "Resolving command path"


def _discover_cmds(_a: dict[str, Any]) -> str:
    return "Discovering commands"


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


def _security_lookup(a: dict[str, Any]) -> str:
    q = str(a.get("query", ""))
    return f"Security lookup: {_trunc(q, 40)}" if q else "Security lookup"


def _manage_crontab(a: dict[str, Any]) -> str:
    action = a.get("action", "list")
    return f"Crontab {action}"


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
    "run_npx": _run_npx,
    "run_python": _python,
    "run_python3": _python,
    "code_sandbox": _code_sandbox,
    # Web
    "web_fetch": _web_fetch,
    "web_search": _web_search,
    "download_file": _download,
    "http_request": _http_req,
    # Git
    "git_status": _git_status,
    "git_diff": _git_diff,
    "git_log": _git_log,
    "git_commit": _git_commit,
    "git_branch": _git_branch,
    # Agents / tasks
    "task": _task,
    "copilot_query": _copilot_query,
    # System
    "which_command": _which,
    "discover_all_commands": _discover_cmds,
    "get_environment": _get_env,
    "get_system_info": _sys_info,
    "list_processes": _list_procs,
    "signal_process": _signal_proc,
    "list_listening_ports": _list_ports,
    "list_unix_capabilities": _list_unix,
    "security_lookup": _security_lookup,
    "manage_crontab": _manage_crontab,
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
