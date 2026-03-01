# pyright: reportMissingImports=false
"""
obscura.internal.tools — Unified tool definitions for both backends.

Provides a ``@tool`` decorator that creates a ``ToolSpec`` which can be
registered with either the Copilot or Claude backend. Includes basic
JSON Schema inference from function type hints.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

from obscura.core.types import ToolSpec


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Central registry for tool specs. Backends read from this at start()."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._alias_targets: dict[str, str] = {
            # shell
            "bash": "run_shell",
            "shell": "run_shell",
            "terminal": "run_shell",
            "runbash": "run_shell",
            "run_bash": "run_shell",
            "execute_shell": "run_shell",
            "run_script": "run_shell",
            # python — multiple naming conventions used by different LLMs
            "python": "run_python3",
            "run_python": "run_python3",
            "execute_python": "run_python3",
            "execute_code": "run_python3",
            "run_code": "run_python3",
            "code": "run_python3",
            # file write — LLMs often use Claude Code names
            "write": "write_text_file",
            "write_file": "write_text_file",
            "writefile": "write_text_file",
            "create_file": "write_text_file",
            "save_file": "write_text_file",
            "filesystem_write_file": "write_text_file",
            "filesystem_write_text_file": "write_text_file",
            "filesystem_create_file": "write_text_file",
            # file read
            "read": "read_text_file",
            "read_file": "read_text_file",
            "readfile": "read_text_file",
            "cat": "read_text_file",
            "view_file": "read_text_file",
            "open_file": "read_text_file",
            "filesystem_read_file": "read_text_file",
            "filesystem_read_text_file": "read_text_file",
            # file append
            "append": "append_text_file",
            "append_file": "append_text_file",
            "filesystem_append_file": "append_text_file",
            # edit — now maps to surgical edit_text_file
            "edit": "edit_text_file",
            "edit_file": "edit_text_file",
            "editfile": "edit_text_file",
            "modify_file": "edit_text_file",
            "patch_file": "edit_text_file",
            "replace_in_file": "edit_text_file",
            "find_replace": "edit_text_file",
            "filesystem_edit_file": "edit_text_file",
            "filesystem_edit_text_file": "edit_text_file",
            # directory
            "ls": "list_directory",
            "list_dir": "list_directory",
            "listdir": "list_directory",
            "dir": "list_directory",
            "filesystem_list_directory": "list_directory",
            "mkdir": "make_directory",
            "makedirectory": "make_directory",
            "filesystem_make_directory": "make_directory",
            # grep / search files
            "grep": "grep_files",
            "rg": "grep_files",
            "ripgrep": "grep_files",
            "search_files": "grep_files",
            "search_code": "grep_files",
            "searchfiles": "grep_files",
            "filesystem_search": "grep_files",
            # find files
            "find": "find_files",
            "glob": "find_files",
            "locate": "find_files",
            "find_file": "find_files",
            "filesystem_find": "find_files",
            "filesystem_glob": "find_files",
            # copy / move
            "cp": "copy_path",
            "copy": "copy_path",
            "copy_file": "copy_path",
            "filesystem_copy": "copy_path",
            "mv": "move_path",
            "move": "move_path",
            "rename": "move_path",
            "move_file": "move_path",
            "rename_file": "move_path",
            "filesystem_move": "move_path",
            "filesystem_rename": "move_path",
            # file info
            "stat": "file_info",
            "fileinfo": "file_info",
            "file_stat": "file_info",
            "filesystem_stat": "file_info",
            "filesystem_info": "file_info",
            # tree
            "tree": "tree_directory",
            "dirtree": "tree_directory",
            "filesystem_tree": "tree_directory",
            # diff
            "diff": "diff_files",
            "compare": "diff_files",
            "compare_files": "diff_files",
            "file_diff": "diff_files",
            # remove
            "rm": "remove_path",
            "delete": "remove_path",
            "delete_file": "remove_path",
            "remove": "remove_path",
            "unlink": "remove_path",
            "filesystem_delete": "remove_path",
            "filesystem_remove": "remove_path",
            # web search
            "websearch": "web_search",
            "searchweb": "web_search",
            "search_web": "web_search",
            "google": "web_search",
            # web fetch
            "webfetch": "web_fetch",
            "fetchurl": "web_fetch",
            "fetch": "web_fetch",
            "get_url": "web_fetch",
            "browse": "web_fetch",
            "open_url": "web_fetch",
            "curl": "web_fetch",
            # http
            "http": "http_request",
            "api_request": "http_request",
            "rest_request": "http_request",
            "api_call": "http_request",
            # download
            "download": "download_file",
            "wget": "download_file",
            "save_url": "download_file",
            # git
            "gitstatus": "git_status",
            "gitdiff": "git_diff",
            "gitlog": "git_log",
            "gitcommit": "git_commit",
            "gitbranch": "git_branch",
            # clipboard
            "clipboard": "clipboard_read",
            "paste": "clipboard_read",
            "pbpaste": "clipboard_read",
            "copy_to_clipboard": "clipboard_write",
            "pbcopy": "clipboard_write",
            # json
            "jq": "json_query",
            "jsonquery": "json_query",
            "query_json": "json_query",
            # context window
            "context": "context_window_status",
            "context_status": "context_window_status",
            "token_usage": "context_window_status",
            "tokens": "context_window_status",
            "window_status": "context_window_status",
            # sandbox
            "sandbox": "code_sandbox",
            "execute": "code_sandbox",
            "repl": "code_sandbox",
            "run": "code_sandbox",
            # dynamic tools
            "make_tool": "create_tool",
            "define_tool": "create_tool",
            "new_tool": "create_tool",
            # task delegation
            "delegatetask": "task",
            # copilot
            "copilot": "copilot_query",
            "gpt5": "copilot_query",
            "gpt5_mini": "copilot_query",
            "ask_copilot": "copilot_query",
            # memory
            "remember": "store_searchable",
            "save_memory": "store_searchable",
            "search_memory": "semantic_search",
            "memory_search": "semantic_search",
            "memory_store": "store_memory",
            "memory_recall": "recall_memory",
            # browser tools
            "browsernavigate": "browser_navigate",
            "browsersnapshot": "browser_snapshot",
            "browserclick": "browser_click",
            "browserfill": "browser_fill",
            # task tracking (Claude Code name → Obscura name)
            "todowrite": "todo_write",
            "todo": "todo_write",
            "update_todos": "todo_write",
            "write_todos": "todo_write",
        }

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec
        # MCP tools are registered with dotted names (e.g. "fetch.fetch")
        # but the Claude SDK sanitizes dots to underscores in tool names.
        # Register the underscored variant so both forms resolve.
        if "." in spec.name:
            underscored = spec.name.replace(".", "_")
            if underscored not in self._tools:
                self._tools[underscored] = spec

    def register_alias(self, alias: str, canonical: str) -> None:
        """Map *alias* to an already-registered *canonical* tool name.

        Useful for runtime registration of backend-specific naming conventions::

            registry.register_alias("execute_python", "run_python3")
            registry.register_alias("google", "web_search")
        """
        self._alias_targets[_normalize_tool_name(alias)] = canonical

    def get(self, name: str) -> ToolSpec | None:
        direct = self._tools.get(name)
        if direct is not None:
            return direct
        # Strip Claude SDK MCP prefix: mcp__<server>__<tool> → <tool>
        stripped = name
        if name.startswith("mcp__") and name.count("__") >= 2:
            stripped = name.split("__", 2)[-1]
            direct = self._tools.get(stripped)
            if direct is not None:
                return direct
        # Try dot ↔ underscore variants (Claude SDK sanitizes dots to underscores)
        if "." in stripped:
            direct = self._tools.get(stripped.replace(".", "_"))
            if direct is not None:
                return direct
        elif "_" in stripped:
            direct = self._tools.get(stripped.replace("_", ".", 1))
            if direct is not None:
                return direct
        # Strip common MCP server name prefixes (filesystem_, git_, memory_, fetch_)
        # LLMs often prepend the MCP server name to tool names
        for prefix in ("filesystem_", "git_", "memory_", "fetch_", "sequentialthinking_"):
            if stripped.startswith(prefix):
                without_prefix = stripped[len(prefix):]
                direct = self._tools.get(without_prefix)
                if direct is not None:
                    return direct
                # Also check alias for the unprefixed name
                canonical = self._alias_targets.get(_normalize_tool_name(without_prefix))
                if canonical is not None:
                    found = self._tools.get(canonical)
                    if found is not None:
                        return found
        # Final alias lookup on the full stripped name
        canonical = self._alias_targets.get(_normalize_tool_name(stripped))
        if canonical is None:
            return None
        return self._tools.get(canonical)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def for_tier(self, tier_value: str) -> list[ToolSpec]:
        """Return tools accessible at *tier_value*.

        ``"privileged"`` gets all tools; ``"public"`` gets only those
        with ``required_tier == "public"``.
        """
        # TODO: restrict public tier once tier differentiation is enabled
        return self.all()

    def names_for_tier(self, tier_value: str) -> list[str]:
        """Return tool names accessible at *tier_value*."""
        return [t.name for t in self.for_tier(tier_value)]


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[type[Any], str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def infer_schema_from_hints(fn: Callable[..., Any]) -> dict[str, Any]:
    """Basic JSON Schema inference from function type hints.

    Handles simple types (str, int, float, bool). For anything more complex,
    pass an explicit schema or use a Pydantic model.
    """
    hints = inspect.get_annotations(fn, eval_str=True)
    sig = inspect.signature(fn)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls", "return"):
            continue

        hint = hints.get(param_name, str)
        json_type = _TYPE_MAP.get(hint, "string")
        properties[param_name] = {"type": json_type}

        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _normalize_tool_name(name: str) -> str:
    chars: list[str] = []
    for char in name.strip().lower():
        if char.isalnum() or char == "_":
            chars.append(char)
    return "".join(chars)


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    *,
    pydantic_model: type[Any] | None = None,
    required_tier: str = "public",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to define a tool that works with both backends.

    Usage::

        @tool("read_file", "Read a file from disk", {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        })
        def read_file(path: str) -> str:
            return Path(path).read_text()

    The decorated function gains a ``.spec`` attribute (``ToolSpec``) that
    the client uses for registration. The function itself remains callable.

    If *parameters* is omitted and *pydantic_model* is provided, the schema
    is generated from the Pydantic model. If both are omitted, a basic schema
    is inferred from type hints.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        schema = parameters
        if schema is None and pydantic_model is not None:
            schema = pydantic_model.model_json_schema()
        elif schema is None:
            schema = infer_schema_from_hints(fn)

        spec = ToolSpec(
            name=name,
            description=description,
            parameters=schema or {},
            handler=fn,
            _pydantic_model=pydantic_model,
            required_tier=required_tier,
        )

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if inspect.iscoroutinefunction(fn):
                return _traced_tool_call_async(name, fn, *args, **kwargs)
            return _traced_tool_call(name, fn, *args, **kwargs)

        setattr(wrapper, "spec", spec)
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Traced tool execution
# ---------------------------------------------------------------------------


def _traced_tool_call(
    tool_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Execute a tool handler wrapped in an OTel span."""
    try:
        from obscura.telemetry.traces import get_tracer

        tracer = get_tracer("obscura.tools")
    except Exception:
        return fn(*args, **kwargs)

    import time

    with tracer.start_as_current_span(f"tool.{tool_name}") as span:
        span.set_attribute("tool.name", tool_name)
        start = time.monotonic()
        try:
            result = fn(*args, **kwargs)
            _record_tool_metric(tool_name, "success", time.monotonic() - start)
            return result
        except Exception as exc:
            _record_tool_metric(tool_name, "error", time.monotonic() - start)
            try:
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, str(exc))
                span.record_exception(exc)
            except ImportError:
                pass
            raise


async def _traced_tool_call_async(
    tool_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Execute an async tool handler wrapped in an OTel span."""
    try:
        from obscura.telemetry.traces import get_tracer

        tracer = get_tracer("obscura.tools")
    except Exception:
        return await fn(*args, **kwargs)

    import time

    with tracer.start_as_current_span(f"tool.{tool_name}") as span:
        span.set_attribute("tool.name", tool_name)
        start = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
            _record_tool_metric(tool_name, "success", time.monotonic() - start)
            return result
        except Exception as exc:
            _record_tool_metric(tool_name, "error", time.monotonic() - start)
            try:
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, str(exc))
                span.record_exception(exc)
            except ImportError:
                pass
            raise


def _record_tool_metric(tool_name: str, status: str, duration: float) -> None:
    """Record tool call metrics."""
    try:
        from obscura.telemetry.metrics import get_metrics

        m = get_metrics()
        m.tool_calls_total.add(1, {"tool_name": tool_name, "status": status})
        m.tool_duration_seconds.record(duration, {"tool_name": tool_name})
    except Exception:
        pass
