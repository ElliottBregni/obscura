"""Shell and command execution tools."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from obscura.core.tools import tool
from obscura.tools.system._policy import Policy


class Shell:
    """Shell-execution tool namespace."""

    # ------------------------------------------------------------------
    # Helpers (also exposed as classmethods so other modules can call them)
    # ------------------------------------------------------------------

    @staticmethod
    def read_allowed_commands() -> set[str]:
        raw = os.environ.get("OBSCURA_SYSTEM_TOOLS_ALLOWED_COMMANDS", "")
        return Policy.normalize_list(raw)

    @staticmethod
    def read_denied_commands() -> set[str]:
        if "OBSCURA_SYSTEM_TOOLS_DENIED_COMMANDS" in os.environ:
            raw = os.environ.get("OBSCURA_SYSTEM_TOOLS_DENIED_COMMANDS", "")
            return Policy.normalize_list(raw)
        return set(Policy.DEFAULT_DENIED_COMMANDS)

    @staticmethod
    def resolve_command(command: str) -> str:
        direct = shutil.which(command)
        if direct:
            return direct
        if command == "npx":
            nvm_root = Path.home() / ".nvm" / "versions" / "node"
            if nvm_root.is_dir():
                candidates = sorted(
                    p for p in nvm_root.glob("*/bin/npx") if p.is_file()
                )
                if candidates:
                    return str(candidates[-1])
        return command

    @staticmethod
    def shell_quote(s: str) -> str:
        """Single-quote a string for safe shell embedding."""
        return "'" + s.replace("'", "'\"'\"'") + "'"

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @staticmethod
    @tool(
        "run_python3",
        "Execute Python code using python3 -c and return "
        "stdout/stderr/exit_code.",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["code"],
        },
        output_schema={
            "x-output-levels": {
                "minimal": ["ok", "exit_code"],
                "standard": [
                    "ok",
                    "stdout",
                    "exit_code",
                    "command",
                    "cwd",
                    "stdout_lines",
                ],
                "full": [
                    "ok",
                    "stdout",
                    "stderr",
                    "exit_code",
                    "command",
                    "cwd",
                    "stdout_lines",
                ],
            },
            "x-default-level": "standard",
        },
    )
    async def run_python3(
        code: str,
        cwd: str = "",
        timeout_seconds: float = 30.0,
    ) -> str:
        command = Shell.resolve_command("python3")
        from obscura.auth.secrets import safe_subprocess_env

        proc = await asyncio.create_subprocess_exec(
            command,
            "-c",
            code,
            cwd=(cwd or None),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_subprocess_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return Policy.json_error("timeout")
        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")
        return json.dumps(
            {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "command": "python3 -c <code>",
                "cwd": cwd or str(Path.cwd()),
                "stdout": stdout_str,
                "stderr": stderr_str,
                "stdout_lines": stdout_str.count("\n") + (1 if stdout_str else 0),
            },
        )

    @staticmethod
    @tool(
        "run_command",
        "Execute a system command with args and return stdout/stderr/exit_code.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["command"],
        },
        output_schema={
            "x-output-levels": {
                "minimal": ["ok", "exit_code"],
                "standard": [
                    "ok",
                    "stdout",
                    "exit_code",
                    "command",
                    "cwd",
                    "stdout_lines",
                ],
                "full": [
                    "ok",
                    "stdout",
                    "stderr",
                    "exit_code",
                    "command",
                    "args",
                    "cwd",
                    "stdout_lines",
                ],
            },
            "x-default-level": "standard",
        },
    )
    async def run_command(
        command: str,
        args: list[str] | None = None,
        cwd: str = "",
        timeout_seconds: float = 60.0,
    ) -> str:
        normalized_command = command.strip()
        if not normalized_command:
            return Policy.json_error("empty_command")

        if not Policy.unsafe_full_access_enabled():
            allowed_commands = Shell.read_allowed_commands()
            denied_commands = Shell.read_denied_commands()
            if allowed_commands and normalized_command not in allowed_commands:
                return Policy.json_error(
                    "command_not_allowed",
                    command=normalized_command,
                )
            if normalized_command in denied_commands:
                return Policy.json_error(
                    "command_denied",
                    command=normalized_command,
                )
            if not Policy.is_cwd_allowed(cwd):
                return Policy.json_error("cwd_not_allowed", cwd=cwd)

        resolved_command = Shell.resolve_command(normalized_command)
        if (
            shutil.which(resolved_command) is None
            and not Path(resolved_command).is_file()
        ):
            return Policy.json_error("command_not_found", command=normalized_command)

        process_args = args or []
        from obscura.auth.secrets import safe_subprocess_env

        proc = await asyncio.create_subprocess_exec(
            resolved_command,
            *process_args,
            cwd=(cwd or None),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_subprocess_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return Policy.json_error("timeout")

        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")
        return json.dumps(
            {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "command": normalized_command,
                "args": process_args,
                "cwd": cwd or str(Path.cwd()),
                "stdout": stdout_str,
                "stderr": stderr_str,
                "stdout_lines": stdout_str.count("\n") + (1 if stdout_str else 0),
            },
        )

    @staticmethod
    @tool(
        "run_shell",
        (
            "Execute a shell command via /bin/zsh -lc and return "
            "stdout/stderr/exit_code. Set run_in_background=true for "
            "long-running commands; returns a task_id that can be checked later."
        ),
        {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "Shell script to execute."},
                "command": {
                    "type": "string",
                    "description": "Alias for script (LLM compat).",
                },
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "number"},
                "description": {
                    "type": "string",
                    "description": "User-facing description of what this command does.",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Run async and return a task_id immediately.",
                },
            },
        },
        output_schema={
            "x-output-levels": {
                "minimal": ["ok", "exit_code"],
                "standard": [
                    "ok",
                    "stdout",
                    "exit_code",
                    "command",
                    "cwd",
                    "stdout_lines",
                    "description",
                ],
                "full": [
                    "ok",
                    "stdout",
                    "stderr",
                    "exit_code",
                    "command",
                    "cwd",
                    "stdout_lines",
                    "description",
                    "background",
                    "task_id",
                    "stdout_truncated",
                    "stderr_truncated",
                    "stdout_full_path",
                    "stderr_full_path",
                    "stdout_full_size",
                    "stderr_full_size",
                ],
            },
            "x-default-level": "standard",
        },
    )
    async def run_shell(
        script: str = "",
        command: str = "",
        cwd: str = "",
        timeout_seconds: float = 60.0,
        description: str = "",
        run_in_background: bool = False,
    ) -> str:
        actual_script = script or command
        if not actual_script:
            return json.dumps({"ok": False, "error": "no_script_provided"})

        if run_in_background:
            from obscura.core.background_tasks import get_background_task_manager

            mgr = get_background_task_manager()
            task_id = await mgr.start(
                f"/bin/zsh -lc {Shell.shell_quote(actual_script)}",
                cwd=cwd,
                timeout=float(timeout_seconds),
            )
            return json.dumps(
                {
                    "ok": True,
                    "background": True,
                    "task_id": task_id,
                    "command": actual_script[:200],
                    "description": description,
                },
            )

        result_json = await Shell.run_command(
            "/bin/zsh",
            args=["-lc", actual_script],
            cwd=cwd,
            timeout_seconds=float(timeout_seconds),
        )

        # Post-process: add context and truncate large output.
        result = json.loads(result_json)
        result["command"] = actual_script
        result["cwd"] = cwd or str(Path.cwd())
        if description:
            result["description"] = description
        stdout_val = result.get("stdout", "")
        result["stdout_lines"] = stdout_val.count("\n") + (1 if stdout_val else 0)

        _MAX_INLINE_OUTPUT = 100_000  # 100KB
        for key in ("stdout", "stderr"):
            val = result.get(key, "")
            if len(val) > _MAX_INLINE_OUTPUT:
                # Persist full output to disk.
                output_dir = Path.home() / ".obscura" / "output"
                output_dir.mkdir(parents=True, exist_ok=True)
                import hashlib

                h = hashlib.sha256(val.encode("utf-8")).hexdigest()[:12]
                output_path = output_dir / f"{key}_{h}.txt"
                output_path.write_text(val, encoding="utf-8")
                result[key] = val[:_MAX_INLINE_OUTPUT]
                result[f"{key}_truncated"] = True
                result[f"{key}_full_path"] = str(output_path)
                result[f"{key}_full_size"] = len(val)

        return json.dumps(result)

    @staticmethod
    @tool(
        "which_command",
        "Resolve an executable path for a command name.",
        {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    )
    async def which_command(command: str) -> str:
        normalized = command.strip()
        if not normalized:
            return Policy.json_error("empty_command")
        resolved = Shell.resolve_command(normalized)
        discovered = shutil.which(resolved)
        if discovered is None:
            return Policy.json_error("command_not_found", command=normalized)
        return json.dumps(
            {
                "ok": True,
                "command": normalized,
                "path": discovered,
                "exists": True,
            },
        )
