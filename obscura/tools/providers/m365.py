"""M365 CLI provider — wraps @pnp/cli-microsoft365 binary."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

logger = logging.getLogger(__name__)


async def M365Provider(**kwargs: Any) -> dict[str, Any]:
    command = kwargs.get("command") or kwargs.get("args") or ""

    binary = shutil.which("m365")
    if not binary:
        return {
            "error": (
                "m365 CLI binary not found on PATH. "
                "Install with: npm install -g @pnp/cli-microsoft365"
            ),
        }

    cmd: list[str] = [binary]
    if command:
        cmd.extend(command.split())

    for key, val in kwargs.items():
        if key.startswith("_") or key in ("command", "args"):
            continue
        if isinstance(val, bool):
            if val:
                cmd.append(f"--{key.replace('_', '-')}")
        else:
            cmd.extend([f"--{key.replace('_', '-')}", str(val)])

    # Always request JSON output
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
            return {"error": err or f"m365 exited with code {proc.returncode}", "output": output}

        try:
            return json.loads(output)
        except (json.JSONDecodeError, ValueError):
            return {"output": output.strip()}
    except Exception as e:
        return {"error": str(e)}
