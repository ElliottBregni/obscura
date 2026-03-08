"""NotebookLM provider — wraps NotebookLM CLI binary.

This provider expects a CLI that supports subcommands like
``notebook list`` and returns JSON to stdout. The package
``notebooklm-mcp-server`` provides such a CLI as the binary
``notebooklm-mcp-server`` (some installs may expose it as
``notebooklm-mcp-server`` only). By contrast, the binary
``notebooklm-mcp`` is an MCP server entrypoint and does not
accept these subcommands. Prefer the CLI-capable binary.

You can override the binary path via the environment variable
``OBSCURA_NOTEBOOKLM_BINARY``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import os
from typing import Any

logger = logging.getLogger(__name__)


async def _run_nlm(*args: str) -> dict[str, Any]:
    # Allow override for environments with nonstandard install locations
    override = os.environ.get("OBSCURA_NOTEBOOKLM_BINARY")
    binary = (
        override
        or shutil.which("notebooklm-mcp-server")
        or shutil.which("notebooklm-mcp")
    )
    if not binary:
        return {
            "error": (
                "NotebookLM CLI not found. Install with: "
                "uv pip install notebooklm-mcp-server, or set "
                "OBSCURA_NOTEBOOKLM_BINARY to the CLI path"
            ),
        }

    # If the only available binary is the MCP server, surface a clear hint
    base = os.path.basename(binary)
    if base == "notebooklm-mcp":
        return {
            "error": (
                "Detected 'notebooklm-mcp' (MCP server), which does not support CLI "
                "subcommands like 'notebook list'. Install the CLI-capable binary "
                "via 'uv pip install notebooklm-mcp-server' and ensure "
                "'notebooklm-mcp-server' is on PATH, or set OBSCURA_NOTEBOOKLM_BINARY."
            )
        }

    cmd = [binary, *args]
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
            return {"error": err or f"notebooklm-mcp exited with code {proc.returncode}", "output": output}

        try:
            return json.loads(output)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, ValueError):
            return {"output": output.strip()}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Notebook handlers
# ---------------------------------------------------------------------------


async def _handler_create_notebook(**kwargs: Any) -> dict[str, Any]:
    title = kwargs.get("title", "Untitled")
    return await _run_nlm("notebook", "create", "--title", str(title))


async def _handler_list_notebooks(**kwargs: Any) -> dict[str, Any]:
    return await _run_nlm("notebook", "list")


async def _handler_get_notebook(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    return await _run_nlm("notebook", "get", "--id", str(notebook_id))


async def _handler_delete_notebook(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    return await _run_nlm("notebook", "delete", "--id", str(notebook_id))


# ---------------------------------------------------------------------------
# Source handlers
# ---------------------------------------------------------------------------


async def _handler_add_source(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}

    args = ["source", "add", "--notebook-id", str(notebook_id)]

    source_url = kwargs.get("source_url")
    source_text = kwargs.get("source_text")
    if source_url:
        args.extend(["--url", str(source_url)])
    elif source_text:
        args.extend(["--text", str(source_text)])
    else:
        return {"error": "source_url or source_text is required"}

    return await _run_nlm(*args)


async def _handler_list_sources(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    return await _run_nlm("source", "list", "--notebook-id", str(notebook_id))


async def _handler_get_source(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    source_id = kwargs.get("source_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    if not source_id:
        return {"error": "source_id is required"}
    return await _run_nlm("source", "get", "--notebook-id", str(notebook_id), "--id", str(source_id))


async def _handler_delete_source(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    source_id = kwargs.get("source_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    if not source_id:
        return {"error": "source_id is required"}
    return await _run_nlm("source", "delete", "--notebook-id", str(notebook_id), "--id", str(source_id))


# ---------------------------------------------------------------------------
# Note handlers
# ---------------------------------------------------------------------------


async def _handler_create_note(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    content = kwargs.get("content", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    if not content:
        return {"error": "content is required"}
    return await _run_nlm("note", "create", "--notebook-id", str(notebook_id), "--content", str(content))


async def _handler_list_notes(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    return await _run_nlm("note", "list", "--notebook-id", str(notebook_id))


async def _handler_get_note(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    note_id = kwargs.get("note_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    if not note_id:
        return {"error": "note_id is required"}
    return await _run_nlm("note", "get", "--notebook-id", str(notebook_id), "--id", str(note_id))


async def _handler_delete_note(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    note_id = kwargs.get("note_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    if not note_id:
        return {"error": "note_id is required"}
    return await _run_nlm("note", "delete", "--notebook-id", str(notebook_id), "--id", str(note_id))


# ---------------------------------------------------------------------------
# Audio overview handlers
# ---------------------------------------------------------------------------


async def _handler_generate_audio_overview(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    return await _run_nlm("audio", "generate", "--notebook-id", str(notebook_id))


async def _handler_get_audio_overview(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    return await _run_nlm("audio", "get", "--notebook-id", str(notebook_id))
