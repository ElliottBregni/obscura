"""Google Workspace CLI provider — wraps the gws binary."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

logger = logging.getLogger(__name__)

_TOOL_TO_SUBCOMMAND: dict[str, str] = {
    "gws": "",
    "gws.schema.inspect": "schema inspect",
    "gws.drive.files.list": "drive files list",
    "gws.drive.files.get": "drive files get",
    "gws.drive.files.upload": "drive files upload",
    "gws.drive.files.download_to_path": "drive files download",
    "gws.drive.files.create_folder": "drive files create-folder",
    "gws.drive.files.copy": "drive files copy",
    "gws.drive.files.delete": "drive files delete",
    "gws.drive.permissions.list": "drive permissions list",
    "gws.drive.permissions.create": "drive permissions create",
    "gws.sheets.spreadsheets.create": "sheets spreadsheets create",
    "gws.sheets.spreadsheets.values.get": "sheets spreadsheets values get",
    "gws.gmail.users.messages.send": "gmail users messages send",
    "gws.gmail.users.messages.send_with_attachments": "gmail users messages send",
    "gws.chat.spaces.messages.create": "chat spaces messages create",
    "gws.calendar.events.insert": "calendar events insert",
    "gws.calendar.events.list": "calendar events list",
    "gws.calendar.events.delete": "calendar events delete",
}

_RESERVED_KEYS = frozenset({"command", "args", "_tool_name"})


async def GWSProvider(**kwargs: Any) -> dict[str, Any]:
    tool_name = kwargs.get("_tool_name", "")
    command = kwargs.get("command") or kwargs.get("args") or ""

    binary = shutil.which("gws")
    if not binary:
        return {
            "error": (
                "gws CLI binary not found on PATH. "
                "Install with: pip install gws-cli"
            ),
        }

    cmd: list[str] = [binary]

    # Resolve subcommand: prefer the static map, fall back to raw command string
    subcommand = _TOOL_TO_SUBCOMMAND.get(tool_name)
    if subcommand:
        cmd.extend(subcommand.split())
    elif command:
        cmd.extend(command.split())

    # Convert remaining kwargs to CLI flags
    for key, val in kwargs.items():
        if key.startswith("_") or key in _RESERVED_KEYS:
            continue
        flag = f"--{key.replace('_', '-')}"
        if isinstance(val, bool):
            if val:
                cmd.append(flag)
        elif isinstance(val, list):
            for item in val:
                cmd.extend([flag, str(item)])
        else:
            cmd.extend([flag, str(val)])

    # Always request JSON output unless already specified
    if "--output" not in " ".join(cmd):
        cmd.extend(["--output", "json"])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode() if stdout else ""
        err = stderr.decode() if stderr else ""

        if proc.returncode != 0:
            return {
                "error": err or f"gws exited with code {proc.returncode}",
                "output": output,
            }

        try:
            return json.loads(output)
        except (json.JSONDecodeError, ValueError):
            return {"output": output.strip()}
    except Exception as e:
        return {"error": str(e)}
