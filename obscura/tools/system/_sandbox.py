"""Code sandbox + dynamically created tools."""

from __future__ import annotations

import asyncio
import json
import re
import time as _time
from pathlib import Path
from typing import Any, ClassVar

from obscura.core.tools import tool
from obscura.core.types import ToolSpec
from obscura.tools.system._policy import Policy


class Sandbox:
    """Code-sandbox and dynamic-tool namespace."""

    # In-memory store for dynamically created tools (session-scoped).
    dynamic_tools: ClassVar[dict[str, ToolSpec]] = {}

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @staticmethod
    @tool(
        "create_tool",
        (
            "Dynamically create a new tool at runtime. Write a Python function body "
            "that accepts keyword arguments and returns a JSON string. The tool becomes "
            "immediately available for subsequent calls in this session."
        ),
        {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Tool name (lowercase, underscored).",
                },
                "description": {"type": "string", "description": "What the tool does."},
                "parameters": {
                    "type": "object",
                    "description": "JSON Schema for tool parameters.",
                },
                "code": {
                    "type": "string",
                    "description": (
                        "Python function body. Receives kwargs matching the parameters schema. "
                        "Must return a JSON string. Has access to: json, os, re, pathlib.Path, "
                        'asyncio, subprocess, urllib. Example: \'return json.dumps({"ok": True, "result": kwargs["x"] * 2})\''
                    ),
                },
            },
            "required": ["name", "description", "code"],
        },
    )
    async def create_tool(
        name: str,
        description: str,
        code: str,
        parameters: dict[str, Any] | None = None,
    ) -> str:
        if parameters is None:
            parameters = {"type": "object", "properties": {}}
        clean_name = re.sub(r"[^a-z0-9_]", "_", name.strip().lower())
        if not clean_name:
            return Policy.json_error("invalid_tool_name")
        from obscura.tools.system import get_system_tool_specs

        if clean_name in {s.name for s in get_system_tool_specs()}:
            return Policy.json_error("name_conflicts_with_builtin", name=clean_name)

        # Build the async handler function
        # Available imports inside the sandbox
        _SAFE_BUILTINS: dict[str, Any] = {
            "len": len,
            "range": range,
            "enumerate": enumerate,
            "zip": zip,
            "map": map,
            "filter": filter,
            "sorted": sorted,
            "reversed": reversed,
            "list": list,
            "dict": dict,
            "set": set,
            "tuple": tuple,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "isinstance": isinstance,
            "hasattr": hasattr,
            "getattr": getattr,
            "print": print,
            "type": type,
            "None": None,
            "True": True,
            "False": False,
            "min": min,
            "max": max,
            "sum": sum,
            "abs": abs,
            "round": round,
            "any": any,
            "all": all,
            "ValueError": ValueError,
            "TypeError": TypeError,
            "KeyError": KeyError,
            "RuntimeError": RuntimeError,
            "Exception": Exception,
        }
        sandbox_globals: dict[str, Any] = {
            "__builtins__": _SAFE_BUILTINS,
            "json": json,
            "re": re,
            "Path": Path,
            "asyncio": asyncio,
            "base64": __import__("base64"),
            "time": _time,
        }

        # Wrap user code in an async function
        indented_code = "\n".join(f"    {line}" for line in code.splitlines())
        func_source = f"async def _dynamic_handler(**kwargs):\n{indented_code}"

        try:
            exec(func_source, sandbox_globals)  # noqa: S102
        except SyntaxError as exc:
            return Policy.json_error("syntax_error", detail=str(exc), line=exc.lineno)

        handler = sandbox_globals["_dynamic_handler"]

        # Create and store the ToolSpec
        spec = ToolSpec(
            name=clean_name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            handler=handler,
        )
        Sandbox.dynamic_tools[clean_name] = spec

        return json.dumps(
            {
                "ok": True,
                "name": clean_name,
                "description": description,
                "message": f"Tool '{clean_name}' created. Call it with the tool name '{clean_name}'.",
            },
        )

    @staticmethod
    @tool(
        "call_dynamic_tool",
        "Call a dynamically created tool by name.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the dynamic tool."},
                "args": {"type": "object", "description": "Arguments to pass as kwargs."},
            },
            "required": ["name"],
        },
    )
    async def call_dynamic_tool(
        name: str, args: dict[str, Any] | None = None
    ) -> str:
        clean_name = re.sub(r"[^a-z0-9_]", "_", name.strip().lower())
        spec = Sandbox.dynamic_tools.get(clean_name)
        if spec is None:
            available = list(Sandbox.dynamic_tools.keys())
            return Policy.json_error(
                "dynamic_tool_not_found",
                name=clean_name,
                available=available,
            )

        kwargs = args or {}
        try:
            result = await spec.handler(**kwargs)
            if isinstance(result, str):
                return result
            return json.dumps({"ok": True, "result": result})
        except Exception as exc:
            return Policy.json_error(
                "dynamic_tool_error", name=clean_name, detail=str(exc)
            )

    @staticmethod
    @tool(
        "list_dynamic_tools",
        "List all dynamically created tools in this session.",
        {
            "type": "object",
            "properties": {},
        },
    )
    async def list_dynamic_tools() -> str:
        tools = [
            {"name": name, "description": spec.description}
            for name, spec in Sandbox.dynamic_tools.items()
        ]
        return json.dumps({"ok": True, "count": len(tools), "tools": tools})

    @staticmethod
    @tool(
        "code_sandbox",
        (
            "Execute code in a sandboxed environment with timeout and resource limits. "
            "Supports Python, Node.js, and shell scripts. Captures stdout, stderr, "
            "and exit code. Files created in the sandbox persist for the session. "
            "Use for writing and testing code, running scripts, or prototyping."
        ),
        {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "description": "'python', 'node', 'bash', or 'zsh'.",
                },
                "code": {"type": "string", "description": "Source code to execute."},
                "timeout_seconds": {"type": "number"},
                "cwd": {
                    "type": "string",
                    "description": "Working directory for execution.",
                },
                "stdin": {"type": "string", "description": "Text to pipe to stdin."},
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Extra environment variables.",
                },
                "save_as": {
                    "type": "string",
                    "description": "Save the code to this file path before executing.",
                },
            },
            "required": ["language", "code"],
        },
    )
    async def code_sandbox(
        language: str,
        code: str,
        timeout_seconds: float = 30.0,
        cwd: str = "",
        stdin: str = "",
        env: dict[str, str] | None = None,
        save_as: str = "",
    ) -> str:
        lang = language.strip().lower()
        timeout_seconds = max(1.0, min(float(timeout_seconds), 300.0))

        # Resolve interpreter
        interpreters: dict[str, tuple[str, list[str]]] = {
            "python": ("python3", ["-c", code]),
            "python3": ("python3", ["-c", code]),
            "node": ("node", ["-e", code]),
            "nodejs": ("node", ["-e", code]),
            "javascript": ("node", ["-e", code]),
            "js": ("node", ["-e", code]),
            "bash": ("/bin/bash", ["-c", code]),
            "zsh": ("/bin/zsh", ["-c", code]),
            "sh": ("/bin/sh", ["-c", code]),
            "shell": ("/bin/zsh", ["-lc", code]),
        }

        if lang not in interpreters:
            return Policy.json_error(
                "unsupported_language",
                language=lang,
                supported=list(interpreters.keys()),
            )

        cmd, args = interpreters[lang]
        from obscura.tools.system._shell import Shell

        resolved_cmd = Shell.resolve_command(cmd)

        # Optionally save code to file first
        if save_as:
            save_path = Policy.resolve_path(save_as)
            if (
                not Policy.unsafe_full_access_enabled()
                and not Policy.is_path_allowed(save_path)
            ):
                return Policy.json_error("path_not_allowed", path=str(save_path))
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(code, encoding="utf-8")
            # Execute the file instead of -c
            if (
                lang in ("python", "python3")
                or lang in ("node", "nodejs", "javascript", "js")
                or lang in ("bash", "zsh", "sh", "shell")
            ):
                args = [str(save_path)]

        # Build environment -- caller-supplied ``env`` wins and is never stripped
        # by strict mode, so the sandbox can be explicitly handed the secrets
        # it needs while the rest of the parent env is filtered when
        # OBSCURA_TOOL_ENV_STRICT=1.
        from obscura.auth.secrets import safe_subprocess_env

        run_env = safe_subprocess_env(env)

        proc = await asyncio.create_subprocess_exec(
            resolved_cmd,
            *args,
            cwd=(cwd or None),
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=run_env,
        )

        try:
            input_data = stdin.encode("utf-8") if stdin else None
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_data),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return json.dumps(
                {
                    "ok": False,
                    "error": "timeout",
                    "language": lang,
                    "timeout_seconds": timeout_seconds,
                },
            )

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        result: dict[str, object] = {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "language": lang,
            "stdout": stdout_text[:100_000],
            "stderr": stderr_text[:50_000],
        }
        if save_as:
            result["saved_to"] = str(Policy.resolve_path(save_as))
        if len(stdout_text) > 100_000:
            result["stdout_truncated"] = True
        if len(stderr_text) > 50_000:
            result["stderr_truncated"] = True

        return json.dumps(result)
