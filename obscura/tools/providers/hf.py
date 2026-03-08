"""Hugging Face Hub CLI provider — wraps the hf binary."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

logger = logging.getLogger(__name__)

# Tool-name to hf subcommand mapping.  The bare ``hf`` tool passes its
# ``command``/``args`` kwarg straight through; every other tool name is
# converted to a subcommand sequence (e.g. ``hf.repo.create`` -> ``repo create``).
_SUBCOMMAND_MAP: dict[str, list[str]] = {
    "hf": [],
    "hf.whoami": ["whoami"],
    "hf.repo.create": ["repo", "create"],
    "hf.repo.clone": ["repo", "clone"],
    "hf.repo.list": ["repo", "list"],
    "hf.repo.upload": ["repo", "upload"],
    "hf.repo.download": ["repo", "download"],
    "hf.repo.push": ["repo", "push"],
    "hf.repo.sync": ["repo", "sync"],
    "hf.repo.upload_from_url": ["repo", "upload-from-url"],
    "hf.repo.delete": ["repo", "delete"],
    "hf.repo.describe": ["repo", "describe"],
    "hf.spaces.create": ["spaces", "create"],
    "hf.spaces.list": ["spaces", "list"],
    "hf.model.list": ["model", "list"],
}

# Keys that are consumed by the provider and should never be forwarded
# as CLI flags.
_RESERVED_KEYS = frozenset({"command", "args", "_tool_name", "_subcommand"})


async def HFProvider(**kwargs: Any) -> dict[str, Any]:
    """Execute a Hugging Face Hub CLI command.

    The plugin loader resolves every ``hf.*`` tool to this single async
    function.  When invoked via the bare ``hf`` tool the caller passes a
    free-form ``command`` (or ``args``) string.  Named tools receive
    structured keyword arguments that are mapped to CLI flags.

    An optional ``_tool_name`` keyword (injected by the broker or loader)
    selects the right subcommand.  If absent the function falls back to
    the ``_subcommand`` keyword or runs the raw ``command`` string.
    """
    tool_name: str = str(kwargs.get("_tool_name", ""))
    raw_command: str = str(kwargs.get("command") or kwargs.get("args") or "")
    subcommand_hint: str = str(kwargs.get("_subcommand", ""))

    binary = shutil.which("hf")
    if not binary:
        return {
            "error": (
                "hf CLI binary not found on PATH. "
                "Install with: pip install huggingface_hub[cli]"
            ),
        }

    # Build the base command list.
    cmd: list[str] = [binary]

    # Determine subcommand tokens.
    if tool_name and tool_name in _SUBCOMMAND_MAP:
        cmd.extend(_SUBCOMMAND_MAP[tool_name])
    elif subcommand_hint:
        cmd.extend(subcommand_hint.split())

    # Append the free-form command string (used by the bare ``hf`` tool).
    if raw_command:
        cmd.extend(raw_command.split())

    # Convert remaining kwargs to CLI flags.
    for key, val in kwargs.items():
        if key.startswith("_") or key in _RESERVED_KEYS:
            continue
        flag = f"--{key.replace('_', '-')}"
        if isinstance(val, bool):
            if val:
                cmd.append(flag)
        elif val is not None:
            cmd.extend([flag, str(val)])

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
                "error": err or f"hf exited with code {proc.returncode}",
                "output": output,
            }

        # Attempt JSON parse; fall back to plain text.
        try:
            return json.loads(output)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, ValueError):
            return {"output": output.strip()}
    except Exception as exc:
        return {"error": str(exc)}
