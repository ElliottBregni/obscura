"""System info and process management tools."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path

from obscura.core.tools import tool
from obscura.tools.system._policy import Policy
from obscura.tools.system._shell import Shell


class Process:
    """Process & system-info tool namespace."""

    @staticmethod
    @tool(
        "get_environment",
        "Return environment variables (optionally filtered by prefix).",
        {
            "type": "object",
            "properties": {
                "prefix": {"type": "string"},
                "include_values": {"type": "boolean"},
            },
        },
    )
    async def get_environment(prefix: str = "", include_values: bool = False) -> str:
        selected: dict[str, str | None] = {}
        for key, value in sorted(os.environ.items()):
            if prefix and not key.startswith(prefix):
                continue
            selected[key] = value if include_values else None
        return json.dumps(
            {"ok": True, "count": len(selected), "variables": selected},
        )

    @staticmethod
    @tool(
        "get_system_info",
        "Return host system information and common tool availability.",
        {"type": "object", "properties": {}},
    )
    async def get_system_info() -> str:
        info = {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": sys.version,
            "cwd": str(Path.cwd()),
            "home": str(Path.home()),
            "commands": {
                "python3": shutil.which("python3"),
                "npx": Shell.resolve_command("npx"),
                "node": shutil.which("node"),
                "git": shutil.which("git"),
                "uv": shutil.which("uv"),
            },
        }
        return json.dumps({"ok": True, "info": info})

    @staticmethod
    @tool(
        "list_processes",
        "List running processes with pid/ppid/user/command.",
        {"type": "object", "properties": {}},
    )
    async def list_processes() -> str:
        return await Shell.run_command(
            "ps",
            args=["-ax", "-o", "pid,ppid,user,%cpu,%mem,command"],
            timeout_seconds=30.0,
        )

    @staticmethod
    @tool(
        "signal_process",
        "Send a signal to a process id.",
        {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "signal": {"type": "string"},
            },
            "required": ["pid"],
        },
    )
    async def signal_process(pid: int, signal: str = "TERM") -> str:
        return await Shell.run_command(
            "kill",
            args=[f"-{signal}", str(pid)],
            timeout_seconds=10.0,
        )

    @staticmethod
    @tool(
        "list_listening_ports",
        "List listening TCP/UDP ports.",
        {"type": "object", "properties": {}},
    )
    async def list_listening_ports() -> str:
        if shutil.which("lsof"):
            return await Shell.run_command(
                "lsof",
                args=["-nP", "-iTCP", "-sTCP:LISTEN"],
                timeout_seconds=30.0,
            )
        if shutil.which("netstat"):
            return await Shell.run_command(
                "netstat",
                args=["-an"],
                timeout_seconds=30.0,
            )
        return Policy.json_error(
            "no_supported_port_tool",
            required_any=["lsof", "netstat"],
        )

    @staticmethod
    @tool(
        "list_unix_capabilities",
        "Describe enabled Unix/system automation capabilities and active guardrails.",
        {"type": "object", "properties": {}},
    )
    async def list_unix_capabilities() -> str:
        # lazy: avoid circular dep with obscura.tools.system (this module is imported by its __init__)
        from obscura.tools.system import get_system_tool_specs

        tool_names = [spec.name for spec in get_system_tool_specs()]
        return json.dumps(
            {
                "ok": True,
                "unsafe_full_access": Policy.unsafe_full_access_enabled(),
                "guardrails": {
                    "allowed_commands": sorted(Shell.read_allowed_commands()),
                    "denied_commands": sorted(Shell.read_denied_commands()),
                    "base_dir": (
                        str(Policy.resolve_base_dir())
                        if Policy.resolve_base_dir()
                        else ""
                    ),
                },
                "tools_count": len(tool_names),
                "tools": tool_names,
            },
        )
