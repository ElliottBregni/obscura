"""Websearch tool provider — wraps the `websearch` Rust CLI binary."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any, cast

logger = logging.getLogger(__name__)

__all__ = [
    "_handler_images",
    "_handler_news",
    "_handler_search",
    "_handler_summarize",
]


async def _run_websearch(*args: str) -> dict[str, Any]:
    binary = shutil.which("websearch")
    if not binary:
        return {
            "error": "websearch binary not found on PATH. Install with: cargo install websearch",
        }
    cmd = [binary, *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode() if stdout else ""
        err = stderr.decode() if stderr else ""
        if proc.returncode != 0:
            return {
                "error": err or f"websearch exited {proc.returncode}",
                "output": out,
            }
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                return cast(dict[str, Any], parsed)
            return {"output": parsed}
        except (json.JSONDecodeError, ValueError):
            logger.debug("suppressed exception in _run_websearch", exc_info=True)
            return {"output": out.strip()}
    except Exception as e:
        logger.debug("suppressed exception in _run_websearch", exc_info=True)
        return {"error": str(e)}


async def _handler_search(**kwargs: Any) -> dict[str, Any]:
    query = kwargs.get("query", "")
    num = str(kwargs.get("num_results", 10))
    provider = str(kwargs.get("provider", "duckduckgo"))
    return await _run_websearch(
        "--format", "json", "--max-results", num, "--provider", provider, query
    )


async def _handler_news(**kwargs: Any) -> dict[str, Any]:
    query = kwargs.get("query", "")
    num = str(kwargs.get("num_results", 10))
    # Default to duckduckgo (no API key required). Callers can pass
    # provider="brave" or "google" if they have a configured API key.
    provider = str(kwargs.get("provider", "duckduckgo"))
    return await _run_websearch(
        "--format", "json", "--max-results", num, "--provider", provider, query
    )


async def _handler_images(**_kwargs: Any) -> dict[str, Any]:
    # The websearch binary does not support image search.
    return {
        "error": "images search is not supported by the installed websearch binary",
        "hint": "Use web_search or fetch_url to find image URLs manually.",
    }


async def _handler_summarize(**kwargs: Any) -> dict[str, Any]:
    url = kwargs.get("url", "")
    if not url:
        return {"error": "url is required"}
    # The websearch binary does not support URL summarization.
    # Delegate to a plain search query using the URL as context.
    return {
        "error": "URL summarization is not supported by the installed websearch binary",
        "hint": "Use fetch_url to retrieve the page, then summarize its content.",
    }
