"""System command tools exposed to agent loops.

Each tool group is a class in its own ``_<group>.py`` submodule (Policy,
Shell, FsRead, FsWrite, Grep, Web, Http, Git, Process, UI, Session, Sandbox,
Mcp). This ``__init__`` re-exports the classes plus a few orphan tools
(``task``, ``json_query``, ``notebook_edit``, ``config_tool``,
``write_agent_shared``, ``list_system_tools``, ``tool_search``,
``set_tool_registry``) and aggregates everything via
:func:`get_system_tool_specs`.

For backwards compatibility, individual tool methods are also re-bound at the
module level (``read_text_file = FsRead.read_text_file`` etc.) so legacy
imports like ``from obscura.tools.system import read_text_file`` keep working.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
from pathlib import Path
from typing import Any, ClassVar, cast

from obscura.core.paths import resolve_obscura_home
from obscura.core.tool_context import current_tool_context
from obscura.core.tools import tool
from obscura.core.types import ToolSpec
from obscura.tools.system._fs_read import FsRead
from obscura.tools.system._fs_write import FsWrite
from obscura.tools.system._git import Git
from obscura.tools.system._grep import Grep
from obscura.tools.system._http import Http
from obscura.tools.system._mcp import Mcp
from obscura.tools.system._policy import Policy
from obscura.tools.system._process import Process
from obscura.tools.system._sandbox import Sandbox
from obscura.tools.system._session import Session
from obscura.tools.system._shared import (
    get_system_tool_specs as get_system_tool_specs,
    set_spec_provider as _set_spec_provider,
)
from obscura.tools.system._shell import Shell
from obscura.tools.system._ui import UI
from obscura.tools.system._web import Web
from obscura.tools.system.delegation import (
    build_agent_cards_section as build_agent_cards_section,
)
from obscura.tools.system.delegation import (
    build_delegate_tool_spec as build_delegate_tool_spec,
)
from obscura.tools.system.intelligence import (
    causal_trace,
    context_snapshot,
    policy_probe,
)
import logging

logger = logging.getLogger(__name__)


_tool_registry_ref: Any = None


# ---------------------------------------------------------------------------
# Orphan tools — small enough not to warrant their own class. Each uses the
# Policy class for sandbox/error formatting.
# ---------------------------------------------------------------------------


@tool(
    "file_change",
    (
        "Record a provider file-change notification. This is informational and "
        "does not modify files."
    ),
    {
        "type": "object",
        "properties": {
            "changes": {"type": "string"},
            "path": {"type": "string"},
            "summary": {"type": "string"},
        },
    },
)
async def file_change(
    changes: str = "",
    path: str = "",
    summary: str = "",
) -> str:
    return json.dumps(
        {
            "ok": True,
            "recorded": True,
            "changes": changes or summary,
            "path": path,
        },
    )


@tool(
    "task",
    (
        "Delegate a sub-task to a local Obscura agent subprocess. "
        "Spawns 'obscura <prompt>' and returns the captured output. "
        "Use 'target' to specify a specialist hint (e.g. 'explore', 'bash'); "
        "omit for default."
    ),
    {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The task to delegate."},
            "target": {
                "type": "string",
                "description": "Optional agent type hint (e.g. 'explore', 'bash').",
            },
        },
        "required": ["prompt"],
    },
)
async def task(prompt: str, target: str = "", timeout_seconds: float = 120.0) -> str:
    obscura_bin = Shell.resolve_command("obscura")
    cmd = [obscura_bin]
    if target:
        cmd += ["-s", f"You are a {target} specialist. Focus on {target} tasks."]
    cmd += ["--max-turns", "25", "--no-confirm", prompt]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            logger.debug("suppressed exception in task", exc_info=True)
            proc.kill()
            await proc.wait()
            return json.dumps({"ok": False, "error": "timeout", "prompt": prompt})
        output = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        return json.dumps(
            {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "result": output,
                "stderr": err,
                "prompt": prompt,
                "target": target,
            },
        )
    except Exception as exc:
        logger.debug("suppressed exception in task", exc_info=True)
        return json.dumps(
            {
                "ok": False,
                "error": "delegation_failed",
                "message": str(exc),
                "prompt": prompt,
                "target": target,
            },
        )


def _parse_json_path(query: str) -> list[str | int]:
    """Parse a dot-notation JSON path like 'users[0].name' into parts."""
    parts: list[str | int] = []
    for segment in query.split("."):
        if not segment:
            continue
        bracket_match = re.match(r"^(\w+)\[(\d+)\]$", segment)
        if bracket_match:
            parts.append(bracket_match.group(1))
            parts.append(int(bracket_match.group(2)))
        elif segment.isdigit():
            parts.append(int(segment))
        else:
            parts.append(segment)
    return parts


@tool(
    "json_query",
    "Query a JSON file or string using dot-notation paths (e.g. 'data.users[0].name').",
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to a JSON file (optional if data provided).",
            },
            "data": {"type": "string", "description": "Raw JSON string to query."},
            "query": {
                "type": "string",
                "description": "Dot-notation path (e.g. 'users[0].name').",
            },
            "keys_only": {
                "type": "boolean",
                "description": "Return only keys at the query path.",
            },
        },
        "required": ["query"],
    },
)
async def json_query(
    query: str,
    path: str = "",
    data: str = "",
    keys_only: bool = False,
) -> str:
    if path:
        target = Policy.resolve_path(path)
        if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(
            target,
        ):
            return Policy.json_error("path_not_allowed", path=str(target))
        if not target.exists():
            return Policy.json_error("path_not_found", path=str(target))
        try:
            raw = target.read_text(encoding="utf-8")
            obj: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.debug("suppressed exception in json_query", exc_info=True)
            return Policy.json_error("invalid_json", path=str(target), detail=str(exc))
    elif data:
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            logger.debug("suppressed exception in json_query", exc_info=True)
            return Policy.json_error("invalid_json", detail=str(exc))
    else:
        return Policy.json_error("no_input", detail="Provide either path or data.")

    current: Any = obj
    parts = _parse_json_path(query)
    for part in parts:
        try:
            if isinstance(current, dict):
                current = cast("dict[str, Any]", current)[str(part)]
            elif isinstance(current, list):
                current = cast("list[Any]", current)[int(part)]
            else:
                return Policy.json_error("invalid_path", query=query, at=part)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            logger.debug("suppressed exception in json_query", exc_info=True)
            return Policy.json_error(
                "path_not_found_in_data",
                query=query,
                at=part,
                detail=str(exc),
            )

    if keys_only and isinstance(current, dict):
        keys_dict = cast("dict[str, Any]", current)
        return json.dumps({"ok": True, "query": query, "keys": list(keys_dict.keys())})

    try:
        result_str = json.dumps(current)
    except (TypeError, ValueError):
        logger.debug("suppressed exception in json_query", exc_info=True)
        result_str = str(current)

    return json.dumps(
        {
            "ok": True,
            "query": query,
            "result": current
            if isinstance(current, (str, int, float, bool, type(None), list, dict))
            else result_str,
        },
    )


@tool(
    "notebook_edit",
    (
        "Edit a Jupyter notebook (.ipynb) cell. Supports replacing cell content, "
        "inserting new cells, or deleting cells."
    ),
    {
        "type": "object",
        "properties": {
            "notebook_path": {
                "type": "string",
                "description": "Path to the .ipynb file.",
            },
            "cell_index": {
                "type": "integer",
                "description": "0-based cell index.",
            },
            "new_source": {
                "type": "string",
                "description": "New cell source content (required for replace/insert).",
            },
            "cell_type": {
                "type": "string",
                "enum": ["code", "markdown"],
                "description": "Cell type (required for insert).",
            },
            "edit_mode": {
                "type": "string",
                "enum": ["replace", "insert", "delete"],
                "description": "Edit mode: replace, insert after, or delete.",
            },
        },
        "required": ["notebook_path", "cell_index"],
    },
)
async def notebook_edit(
    notebook_path: str,
    cell_index: int,
    new_source: str = "",
    cell_type: str = "code",
    edit_mode: str = "replace",
) -> str:
    target = Policy.resolve_path(notebook_path)
    if not Policy.unsafe_full_access_enabled() and not Policy.is_path_allowed(target):
        return Policy.json_error("path_not_allowed", path=str(target))
    if not target.exists():
        return Policy.json_error("path_not_found", path=str(target))
    if target.suffix.lower() != ".ipynb":
        return Policy.json_error("not_a_notebook", path=str(target))

    try:
        nb_data = cast("dict[str, Any]", json.loads(target.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("suppressed exception in notebook_edit", exc_info=True)
        return Policy.json_error("notebook_parse_error", detail=str(exc))

    cells = cast("list[dict[str, Any]]", nb_data.get("cells", []))

    try:
        cell_index = int(cell_index)
    except (TypeError, ValueError):
        logger.debug("suppressed exception in notebook_edit", exc_info=True)
        cell_index = 0

    if edit_mode == "delete":
        if cell_index < 0 or cell_index >= len(cells):
            return Policy.json_error(
                "cell_index_out_of_range",
                index=cell_index,
                total_cells=len(cells),
            )
        old_source_list = cast("list[str]", cells[cell_index].get("source", []))
        old_source = "".join(old_source_list)
        del cells[cell_index]
        target.write_text(
            json.dumps(nb_data, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return json.dumps(
            {
                "ok": True,
                "edit_mode": "delete",
                "cell_index": cell_index,
                "deleted_source": old_source[:200],
                "cell_count": len(cells),
            },
        )

    if edit_mode == "replace":
        if cell_index < 0 or cell_index >= len(cells):
            return Policy.json_error(
                "cell_index_out_of_range",
                index=cell_index,
                total_cells=len(cells),
            )
        old_source_list = cast("list[str]", cells[cell_index].get("source", []))
        old_source = "".join(old_source_list)
        cells[cell_index]["source"] = new_source.splitlines(keepends=True)
        if cell_type:
            cells[cell_index]["cell_type"] = cell_type
        target.write_text(
            json.dumps(nb_data, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return json.dumps(
            {
                "ok": True,
                "edit_mode": "replace",
                "cell_index": cell_index,
                "cell_type": cells[cell_index].get("cell_type", "code"),
                "old_source": old_source[:200],
                "new_source": new_source[:200],
            },
        )

    if edit_mode == "insert":
        if cell_index < -1 or cell_index >= len(cells):
            return Policy.json_error(
                "cell_index_out_of_range",
                index=cell_index,
                total_cells=len(cells),
            )
        new_cell: dict[str, Any] = {
            "cell_type": cell_type,
            "source": new_source.splitlines(keepends=True),
            "metadata": {},
            "outputs": [],
        }
        if cell_type == "code":
            new_cell["execution_count"] = None
        cells.insert(cell_index + 1, new_cell)
        target.write_text(
            json.dumps(nb_data, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return json.dumps(
            {
                "ok": True,
                "edit_mode": "insert",
                "inserted_after": cell_index,
                "cell_type": cell_type,
                "new_source": new_source[:200],
                "cell_count": len(cells),
            },
        )

    return Policy.json_error(
        "invalid_edit_mode",
        detail=f"Unknown edit_mode: {edit_mode}",
    )


@tool(
    "config",
    "Read or write Obscura settings (~/.obscura/settings.json).",
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "set", "list"],
                "description": "Action: 'get' a key, 'set' a key, or 'list' all.",
            },
            "key": {
                "type": "string",
                "description": "Settings key (dot-notation, e.g. 'backend.default').",
            },
            "value": {
                "description": "Value to set (string, number, bool, or null).",
            },
        },
        "required": ["action"],
    },
)
async def config_tool(
    action: str,
    key: str = "",
    value: Any = None,
) -> str:
    settings_path = Path.home() / ".obscura" / "settings.json"

    settings: dict[str, Any] = {}
    if settings_path.is_file():
        try:
            settings = cast(
                "dict[str, Any]",
                json.loads(settings_path.read_text(encoding="utf-8")),
            )
        except (json.JSONDecodeError, OSError):
            logger.debug("suppressed exception in config_tool", exc_info=True)
            settings = {}

    if action == "list":
        return json.dumps({"ok": True, "settings": settings})

    if action == "get":
        if not key:
            return Policy.json_error(
                "missing_key",
                detail="'key' is required for 'get' action",
            )
        parts = key.split(".")
        current: Any = settings
        for part in parts:
            if isinstance(current, dict) and part in current:
                current_dict = cast("dict[str, Any]", current)
                current = current_dict[part]
            else:
                return json.dumps(
                    {"ok": True, "key": key, "value": None, "found": False},
                )
        return json.dumps({"ok": True, "key": key, "value": current, "found": True})

    if action == "set":
        if not key:
            return Policy.json_error(
                "missing_key",
                detail="'key' is required for 'set' action",
            )
        parts = key.split(".")
        target_dict = settings
        for part in parts[:-1]:
            if part not in target_dict or not isinstance(target_dict[part], dict):
                target_dict[part] = {}
            target_dict = cast("dict[str, Any]", target_dict[part])
        target_dict[parts[-1]] = value
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(settings, indent=2) + "\n",
            encoding="utf-8",
        )
        return json.dumps({"ok": True, "key": key, "value": value, "written": True})

    return Policy.json_error("invalid_action", detail=f"Unknown action: {action}")


@tool(
    "write_agent_shared",
    (
        "Write to the shared vault zone. Backs up the previous version and "
        "attempts a line-level fork-merge. Returns merged/conflict flags."
    ),
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path relative to vault/shared/ (e.g. 'decisions/plan.md'). "
                    "Must not escape vault/shared/."
                ),
            },
            "text": {"type": "string", "description": "Content to write."},
        },
        "required": ["path", "text"],
    },
)
async def write_agent_shared(path: str, text: str) -> str:
    shared_root = (resolve_obscura_home() / "vault" / "shared").resolve()

    candidate = (shared_root / path).resolve()
    try:
        candidate.relative_to(shared_root)
    except ValueError:
        logger.debug("suppressed exception in write_agent_shared", exc_info=True)
        return Policy.json_error(
            "path_not_allowed",
            detail="Resolved path escapes vault/shared/",
            path=path,
        )

    backed_up = False
    merged = False
    had_conflict = False

    if candidate.exists():
        ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")
        backup_dir = shared_root / ".backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{candidate.name}.{ts}.bak"
        try:
            old_bytes = candidate.read_bytes()
            backup_path.write_bytes(old_bytes)
            backed_up = True
        except OSError as exc:
            logger.debug("suppressed exception in write_agent_shared", exc_info=True)
            return Policy.json_error(
                "backup_failed",
                path=str(candidate),
                detail=str(exc),
            )

        old_lines = old_bytes.decode("utf-8", errors="replace").splitlines(
            keepends=True,
        )
        new_lines = text.splitlines(keepends=True)
        merged_lines, had_conflict = FsWrite.merge_lines(old_lines, new_lines)
        final_text = "".join(merged_lines)
        merged = True
    else:
        final_text = text

    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(final_text, encoding="utf-8")
    except OSError as exc:
        logger.debug("suppressed exception in write_agent_shared", exc_info=True)
        return Policy.json_error("write_failed", path=str(candidate), detail=str(exc))

    return json.dumps(
        {
            "ok": True,
            "path": str(candidate),
            "backed_up": backed_up,
            "merged": merged,
            "conflict": had_conflict,
        },
    )


# ---------------------------------------------------------------------------
# Tool registry — list/search system tools, set the active registry.
# ---------------------------------------------------------------------------


class Registry:
    """Tool-registry namespace (search, listing, set/get the active registry)."""

    tool_registry_ref: ClassVar[Any] = None

    @classmethod
    def set_tool_registry(cls, registry: Any) -> None:
        """Set the global ToolRegistry reference for tool_search."""
        global _tool_registry_ref
        cls.tool_registry_ref = registry
        _tool_registry_ref = registry

    @staticmethod
    @tool(
        "list_system_tools",
        "List available built-in system tools and their metadata.",
        {"type": "object", "properties": {}},
    )
    async def list_system_tools() -> str:
        tool_specs = get_system_tool_specs()
        data = [
            {
                "name": spec.name,
                "description": spec.description,
                "required_tier": spec.required_tier,
            }
            for spec in tool_specs
        ]
        return json.dumps({"ok": True, "count": len(data), "tools": data})

    @staticmethod
    @tool(
        "tool_search",
        (
            "Search for available tools by name or keyword. "
            "Use 'select:ToolName' for exact match, or keywords for fuzzy search."
        ),
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query. 'select:name' for exact match, or keywords."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results (default 5).",
                },
            },
            "required": ["query"],
        },
    )
    async def tool_search(query: str, max_results: int = 5) -> str:
        ctx = current_tool_context()
        registry: Any = ctx.registry if ctx is not None else None
        if registry is None:
            registry = Registry.tool_registry_ref
        if registry is None:
            registry = _tool_registry_ref
        if registry is None:
            return Policy.json_error(
                "no_registry",
                detail="Tool registry not available",
            )

        all_specs = cast("list[Any]", registry.all())
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            logger.debug("suppressed exception in tool_search", exc_info=True)
            max_results = 5
        cap = max(1, min(max_results, 50))

        if query.startswith("select:"):
            names = [n.strip() for n in query[7:].split(",") if n.strip()]
            found: list[dict[str, str]] = []
            for name in names:
                spec = registry.get(name)
                if spec is not None:
                    found.append(
                        {"name": str(spec.name), "description": str(spec.description)},
                    )
            return json.dumps(
                {
                    "ok": True,
                    "query": query,
                    "matches": found,
                    "total_tools": len(all_specs),
                },
            )

        terms = query.lower().split()
        scored: list[tuple[float, Any]] = []
        for spec in all_specs:
            name_lower = str(spec.name).lower()
            desc_lower = str(spec.description).lower()
            score = 0.0
            for term in terms:
                if term == name_lower:
                    score += 10.0
                elif term in name_lower:
                    score += 5.0
                if term in desc_lower:
                    score += 1.0
            if score > 0:
                scored.append((score, spec))

        scored.sort(key=lambda x: x[0], reverse=True)
        matches = [
            {"name": str(spec.name), "description": str(spec.description)}
            for _, spec in scored[:cap]
        ]
        return json.dumps(
            {
                "ok": True,
                "query": query,
                "matches": matches,
                "total_tools": len(all_specs),
            },
        )


# ---------------------------------------------------------------------------
# _aggregate_tool_specs — concrete aggregator. Registered with _shared so
# children that need this list can import it from _shared.get_system_tool_specs
# at module top without re-entering this __init__.
# ---------------------------------------------------------------------------


def _aggregate_tool_specs() -> list[ToolSpec]:
    """Return default system tool specs for the agent runtime."""
    static_specs: list[ToolSpec] = [
        # Execution
        cast("ToolSpec", cast("Any", Shell.run_python3).spec),
        cast("ToolSpec", cast("Any", Shell.run_command).spec),
        cast("ToolSpec", cast("Any", Shell.run_shell).spec),
        # Web
        cast("ToolSpec", cast("Any", Web.web_fetch).spec),
        cast("ToolSpec", cast("Any", Web.web_search).spec),
        # Delegation
        cast("ToolSpec", cast("Any", task).spec),
        # Provider notifications
        cast("ToolSpec", cast("Any", file_change).spec),
        # System discovery
        cast("ToolSpec", cast("Any", Shell.which_command).spec),
        # Filesystem — basic
        cast("ToolSpec", cast("Any", FsRead.list_directory).spec),
        cast("ToolSpec", cast("Any", FsRead.read_text_file).spec),
        cast("ToolSpec", cast("Any", FsWrite.write_text_file).spec),
        cast("ToolSpec", cast("Any", FsWrite.append_text_file).spec),
        cast("ToolSpec", cast("Any", write_agent_shared).spec),
        cast("ToolSpec", cast("Any", FsWrite.make_directory).spec),
        cast("ToolSpec", cast("Any", FsWrite.remove_path).spec),
        # Filesystem — advanced
        cast("ToolSpec", cast("Any", Grep.grep_files).spec),
        cast("ToolSpec", cast("Any", FsRead.find_files).spec),
        cast("ToolSpec", cast("Any", FsWrite.edit_text_file).spec),
        cast("ToolSpec", cast("Any", FsWrite.copy_path).spec),
        cast("ToolSpec", cast("Any", FsWrite.move_path).spec),
        cast("ToolSpec", cast("Any", FsRead.file_info).spec),
        cast("ToolSpec", cast("Any", FsRead.tree_directory).spec),
        cast("ToolSpec", cast("Any", FsWrite.diff_files).spec),
        # Git
        cast("ToolSpec", cast("Any", Git.git).spec),
        # Utilities
        cast("ToolSpec", cast("Any", Http.download_file).spec),
        cast("ToolSpec", cast("Any", Http.http_request).spec),
        cast("ToolSpec", cast("Any", Http.clipboard_read).spec),
        cast("ToolSpec", cast("Any", Http.clipboard_write).spec),
        cast("ToolSpec", cast("Any", json_query).spec),
        # Context window
        cast("ToolSpec", cast("Any", Session.context_window_status).spec),
        # Dynamic tools + sandbox
        cast("ToolSpec", cast("Any", Sandbox.create_tool).spec),
        cast("ToolSpec", cast("Any", Sandbox.call_dynamic_tool).spec),
        cast("ToolSpec", cast("Any", Sandbox.list_dynamic_tools).spec),
        cast("ToolSpec", cast("Any", Sandbox.code_sandbox).spec),
        # System info
        cast("ToolSpec", cast("Any", Process.get_environment).spec),
        cast("ToolSpec", cast("Any", Process.get_system_info).spec),
        cast("ToolSpec", cast("Any", Process.list_processes).spec),
        cast("ToolSpec", cast("Any", Process.signal_process).spec),
        cast("ToolSpec", cast("Any", Process.list_listening_ports).spec),
        cast("ToolSpec", cast("Any", Process.list_unix_capabilities).spec),
        cast("ToolSpec", cast("Any", Registry.list_system_tools).spec),
        # Task tracking
        cast("ToolSpec", cast("Any", Session.todo_write).spec),
        # Agent intent reporting
        cast("ToolSpec", cast("Any", Session.report_intent).spec),
        # User interaction
        cast("ToolSpec", cast("Any", UI.ask_user).spec),
        cast("ToolSpec", cast("Any", UI.user_ask).spec),
        cast("ToolSpec", cast("Any", UI.user_interact).spec),
        # Intelligence tools
        cast("ToolSpec", cast("Any", context_snapshot).spec),
        cast("ToolSpec", cast("Any", causal_trace).spec),
        cast("ToolSpec", cast("Any", policy_probe).spec),
        # History snip
        cast("ToolSpec", cast("Any", Session.history_snip).spec),
        # Notebook edit
        cast("ToolSpec", cast("Any", notebook_edit).spec),
        # Tool search
        cast("ToolSpec", cast("Any", Registry.tool_search).spec),
        # MCP discovery
        cast("ToolSpec", cast("Any", Mcp.mcp_discovery_status).spec),
        cast("ToolSpec", cast("Any", Mcp.mcp_cleanup_orphans).spec),
        # Sleep & Config
        cast("ToolSpec", cast("Any", Session.sleep).spec),
        cast("ToolSpec", cast("Any", config_tool).spec),
    ]
    # Append any dynamically created tools
    for spec in Sandbox.dynamic_tools.values():
        static_specs.append(spec)
    return static_specs


# Wire the aggregator into _shared so children (_sandbox, _process) can
# import get_system_tool_specs from _shared at module-top without cycles.
_set_spec_provider(_aggregate_tool_specs)


# ---------------------------------------------------------------------------
# Backward-compat re-exports.
#
# Legacy callers do ``from obscura.tools.system import read_text_file`` etc.
# These bindings keep that surface working without forcing every site to
# migrate to ``FsRead.read_text_file``. New code should prefer the class
# form for clarity.
# ---------------------------------------------------------------------------

# Filesystem
read_text_file = FsRead.read_text_file
list_directory = FsRead.list_directory
file_info = FsRead.file_info
find_files = FsRead.find_files
tree_directory = FsRead.tree_directory
write_text_file = FsWrite.write_text_file
append_text_file = FsWrite.append_text_file
edit_text_file = FsWrite.edit_text_file
copy_path = FsWrite.copy_path
move_path = FsWrite.move_path
make_directory = FsWrite.make_directory
remove_path = FsWrite.remove_path
diff_files = FsWrite.diff_files
grep_files = Grep.grep_files

# Shell + process
run_python3 = Shell.run_python3
run_command = Shell.run_command
run_shell = Shell.run_shell
which_command = Shell.which_command
discover_all_commands = Shell.which_command
get_environment = Process.get_environment
get_system_info = Process.get_system_info
list_processes = Process.list_processes
signal_process = Process.signal_process
list_listening_ports = Process.list_listening_ports
list_unix_capabilities = Process.list_unix_capabilities

# Network
web_fetch = Web.web_fetch
web_search = Web.web_search
download_file = Http.download_file
http_request = Http.http_request
clipboard_read = Http.clipboard_read
clipboard_write = Http.clipboard_write
git = Git.git

# Session & UI
context_window_status = Session.context_window_status
todo_write = Session.todo_write
report_intent = Session.report_intent
enter_plan_mode = Session.enter_plan_mode
exit_plan_mode = Session.exit_plan_mode
history_snip = Session.history_snip
sleep = Session.sleep
ask_user = UI.ask_user
user_ask = UI.user_ask
user_interact = UI.user_interact

# Setters / state
set_permission_mode_callback = Session.set_permission_mode_callback
set_plan_approval_callback = Session.set_plan_approval_callback
set_snip_message_history = Session.set_snip_message_history
update_token_usage = Session.update_token_usage
set_ask_user_callback = UI.set_ask_user_callback
was_ask_user_called = UI.was_ask_user_called
reset_ask_user_called = UI.reset_ask_user_called
set_user_interact_callback = UI.set_user_interact_callback
set_tool_registry = Registry.set_tool_registry

# Sandbox
code_sandbox = Sandbox.code_sandbox
create_tool = Sandbox.create_tool
call_dynamic_tool = Sandbox.call_dynamic_tool
list_dynamic_tools = Sandbox.list_dynamic_tools

# MCP
mcp_discovery_status = Mcp.mcp_discovery_status
mcp_cleanup_orphans = Mcp.mcp_cleanup_orphans

# Registry
list_system_tools = Registry.list_system_tools
tool_search = Registry.tool_search

# Policy (private aliases — old code expected these names)
add_allowed_dir = Policy.add_allowed_dir
_is_vault_write_allowed = Policy.is_vault_write_allowed
_is_path_allowed = Policy.is_path_allowed
_is_cwd_allowed = Policy.is_cwd_allowed
_resolve_path = Policy.resolve_path
_resolve_base_dir = Policy.resolve_base_dir
_json_error = Policy.json_error
_validate_url = Policy.validate_url
_normalize_list = Policy.normalize_list
_env_flag = Policy.env_flag
_unsafe_full_access_enabled = Policy.unsafe_full_access_enabled
_string_key_dict = Policy.string_key_dict
_runtime_allowed_dirs = Policy.runtime_allowed_dirs
_DEFAULT_DENIED_COMMANDS = Policy.DEFAULT_DENIED_COMMANDS

# Shell helper aliases
_resolve_command = Shell.resolve_command
_shell_quote = Shell.shell_quote
_read_allowed_commands = Shell.read_allowed_commands
_read_denied_commands = Shell.read_denied_commands

# Sandbox state alias
_dynamic_tools = Sandbox.dynamic_tools

# Session state aliases (old code reads these directly)
_set_permission_mode_callback = Session.permission_mode_callback
_plan_approval_callback = Session.plan_approval_callback
_token_usage = Session.token_usage
_snip_message_history = Session.snip_message_history

# UI state aliases
_ask_user_callback = UI.ask_user_callback
_ask_user_called = UI.ask_user_called
_user_interact_callback = UI.user_interact_callback


# Compatibility stub for copilot_query (some tests import it directly).
# Returns a JSON error payload to avoid calling external services during tests.
@tool(
    "copilot_query",
    "Query the Copilot/GPT backend (stub). Returns a JSON string.",
    {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)
async def copilot_query(query: str) -> str:
    try:
        return json.dumps({"ok": False, "error": "copilot_unavailable", "query": query})
    except Exception:
        logger.debug("suppressed exception in copilot_query", exc_info=True)
        return json.dumps({"ok": False, "error": "copilot_stub_error"})


# Backward-compatible git helpers
async def git_status(*, cwd: str = "", short: bool = True) -> str:
    return await Git.git("status", short=short, cwd=cwd)


async def git_diff(
    *,
    cwd: str = "",
    staged: bool = False,
    stat_only: bool = False,
    ref: str = "",
    path: str = "",
) -> str:
    return await Git.git(
        "diff", staged=staged, stat_only=stat_only, ref=ref, path=path, cwd=cwd
    )


async def git_log(
    *,
    cwd: str = "",
    max_count: int = 10,
    oneline: bool = True,
    ref: str = "",
    author: str = "",
    since: str = "",
) -> str:
    return await Git.git(
        "log",
        max_count=max_count,
        oneline=oneline,
        ref=ref,
        author=author,
        since=since,
        cwd=cwd,
    )


async def git_commit(
    message: str, *, cwd: str = "", files: list[str] | None = None
) -> str:
    return await Git.git("commit", message=message, files=files or ["."], cwd=cwd)


async def git_branch(sub_action: str = "list", *, ref: str = "", cwd: str = "") -> str:
    return await Git.git("branch", sub_action=sub_action, ref=ref, cwd=cwd)


@tool(
    "manage_crontab",
    (
        "Manage crontab entries. Compatibility stub for tests: 'list' returns "
        "an empty set of entries on non-mac environments."
    ),
    {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "marker": {"type": "string"},
            "entry": {"type": "string"},
        },
        "required": ["action"],
    },
)
async def manage_crontab(action: str, marker: str = "", entry: str = "") -> str:
    if action == "list":
        return json.dumps({"ok": True, "entries": []})
    return json.dumps({"ok": False, "error": "not_implemented", "action": action})


# Backward-compatible command helpers
async def run_python(code: str, *, cwd: str = "", timeout_seconds: float = 30.0) -> str:
    return await Shell.run_python3(code, cwd=cwd, timeout_seconds=timeout_seconds)


async def run_npx(
    args: list[str] | None = None, *, cwd: str = "", timeout_seconds: float = 60.0
) -> str:
    return await Shell.run_command(
        "npx", args=args or [], cwd=cwd, timeout_seconds=timeout_seconds
    )


# Security lookup stub (legacy name used by tests)
async def security_lookup(query: str) -> str:
    return json.dumps(
        {"ok": False, "error": "security_lookup_unavailable", "query": query}
    )
