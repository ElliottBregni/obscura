"""Gitleaks secret-scanning provider."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

logger = logging.getLogger(__name__)


async def _handler_scan_repo(**kwargs: Any) -> dict[str, Any]:
    path = kwargs.get("path", ".")
    binary = shutil.which("gitleaks")
    if not binary:
        return {"error": "gitleaks binary not found on PATH. Install from https://github.com/gitleaks/gitleaks"}
    cmd = [binary, "detect", "--source", str(path), "--report-format", "json", "--report-path", "/dev/stdout", "--no-banner"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode() if stdout else ""
        # gitleaks returns exit code 1 if leaks found (not an error)
        if proc.returncode not in (0, 1):
            err = stderr.decode() if stderr else ""
            return {"error": err or f"gitleaks exited {proc.returncode}"}
        try:
            findings = json.loads(out) if out.strip() else []
            return {"findings": findings, "count": len(findings), "clean": len(findings) == 0}
        except (json.JSONDecodeError, ValueError):
            return {"output": out.strip(), "clean": "no leaks" in out.lower()}
    except Exception as e:
        return {"error": str(e)}
