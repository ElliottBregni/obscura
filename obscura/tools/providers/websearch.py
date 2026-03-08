"""Websearch tool provider — wraps the `websearch` Rust CLI binary."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

logger = logging.getLogger(__name__)


async def _run_websearch(*args: str) -> dict[str, Any]:
    binary = shutil.which("websearch")
    if not binary:
        return {"error": "websearch binary not found on PATH. Install with: cargo install websearch"}
    cmd = [binary, *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode() if stdout else ""
        err = stderr.decode() if stderr else ""
        if proc.returncode != 0:
            return {"error": err or f"websearch exited {proc.returncode}", "output": out}
        try:
            return json.loads(out)
        except (json.JSONDecodeError, ValueError):
            return {"output": out.strip()}
    except Exception as e:
        return {"error": str(e)}


async def _handler_search(**kwargs: Any) -> dict[str, Any]:
    query = kwargs.get("query", "")
    num = str(kwargs.get("num_results", 10))
    return await _run_websearch("search", "--query", query, "--num-results", num)


async def _handler_news(**kwargs: Any) -> dict[str, Any]:
    query = kwargs.get("query", "")
    num = str(kwargs.get("num_results", 10))
    return await _run_websearch("news", "--query", query, "--num-results", num)


async def _handler_images(**kwargs: Any) -> dict[str, Any]:
    query = kwargs.get("query", "")
    num = str(kwargs.get("num_results", 10))
    return await _run_websearch("images", "--query", query, "--num-results", num)


async def _handler_summarize(**kwargs: Any) -> dict[str, Any]:
    url = kwargs.get("url", "")
    if not url:
        return {"error": "url is required"}
    return await _run_websearch("summarize", "--url", url)
