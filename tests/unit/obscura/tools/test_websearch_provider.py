"""Unit tests for obscura.tools.providers.websearch (Rust CLI wrapper).

All paths route through _run_websearch which calls asyncio.create_subprocess_exec.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import obscura.tools.providers.websearch as _ws

pytestmark = pytest.mark.unit


def _make_proc(returncode: int, stdout: bytes, stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# Binary not found
# ---------------------------------------------------------------------------


async def test_run_websearch_binary_not_found() -> None:
    with patch.object(_ws.shutil, "which", return_value=None):
        result = await _ws._run_websearch("search", "--query", "test")

    assert "error" in result
    assert "websearch" in result["error"]


# ---------------------------------------------------------------------------
# _handler_search
# ---------------------------------------------------------------------------


async def test_handler_search_returns_results() -> None:
    results_json = json.dumps({"results": [{"title": "A"}]}).encode()
    proc = _make_proc(0, results_json)

    with (
        patch.object(_ws.shutil, "which", return_value="/usr/bin/websearch"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _ws._handler_search(query="test query")

    assert "results" in result


async def test_handler_search_passes_num_results() -> None:
    proc = _make_proc(0, json.dumps({}).encode())
    mock_exec = AsyncMock(return_value=proc)

    with (
        patch.object(_ws.shutil, "which", return_value="/usr/bin/websearch"),
        patch.object(asyncio, "create_subprocess_exec", new=mock_exec),
    ):
        await _ws._handler_search(query="q", num_results=5)

    cmd_str = " ".join(str(a) for a in mock_exec.call_args[0])
    # Binary now uses --max-results (not --num-results subcommand syntax).
    assert "--max-results" in cmd_str
    assert "5" in cmd_str


# ---------------------------------------------------------------------------
# _handler_news
# ---------------------------------------------------------------------------


async def test_handler_news_uses_format_json() -> None:
    # The "news" subcommand was removed in the binary upgrade; news now uses
    # the same positional-query path as search with --format json.
    proc = _make_proc(0, json.dumps([{"title": "Breaking"}]).encode())
    mock_exec = AsyncMock(return_value=proc)

    with (
        patch.object(_ws.shutil, "which", return_value="/usr/bin/websearch"),
        patch.object(asyncio, "create_subprocess_exec", new=mock_exec),
    ):
        await _ws._handler_news(query="breaking")

    cmd_str = " ".join(str(a) for a in mock_exec.call_args[0])
    assert "--format" in cmd_str
    assert "json" in cmd_str
    assert "breaking" in cmd_str


# ---------------------------------------------------------------------------
# _handler_images
# ---------------------------------------------------------------------------


async def test_handler_images_returns_unsupported_error() -> None:
    # Image search is not supported by the current websearch binary version.
    # The handler must return an error dict without invoking the subprocess.
    mock_exec = AsyncMock()

    with (
        patch.object(_ws.shutil, "which", return_value="/usr/bin/websearch"),
        patch.object(asyncio, "create_subprocess_exec", new=mock_exec),
    ):
        result = await _ws._handler_images(query="cats")

    assert "error" in result
    mock_exec.assert_not_called()


# ---------------------------------------------------------------------------
# _handler_summarize
# ---------------------------------------------------------------------------


async def test_handler_summarize_missing_url_returns_error() -> None:
    result = await _ws._handler_summarize()

    assert "error" in result
    assert "url" in result["error"]


async def test_handler_summarize_with_url_returns_unsupported_error() -> None:
    # URL summarization is not supported by the current websearch binary version.
    # The handler must return an error dict without invoking the subprocess.
    mock_exec = AsyncMock()

    with (
        patch.object(_ws.shutil, "which", return_value="/usr/bin/websearch"),
        patch.object(asyncio, "create_subprocess_exec", new=mock_exec),
    ):
        result = await _ws._handler_summarize(url="https://example.com")

    assert "error" in result
    mock_exec.assert_not_called()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_run_websearch_nonzero_exit_returns_error() -> None:
    proc = _make_proc(1, b"", b"search failed")

    with (
        patch.object(_ws.shutil, "which", return_value="/usr/bin/websearch"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _ws._run_websearch("search", "--query", "test")

    assert "error" in result


async def test_run_websearch_non_json_output_returned_raw() -> None:
    proc = _make_proc(0, b"plain text result")

    with (
        patch.object(_ws.shutil, "which", return_value="/usr/bin/websearch"),
        patch.object(asyncio, "create_subprocess_exec", return_value=proc),
    ):
        result = await _ws._run_websearch("search", "--query", "test")

    assert "output" in result
    assert "plain text result" in result["output"]


async def test_run_websearch_subprocess_exception_returns_error() -> None:
    with (
        patch.object(_ws.shutil, "which", return_value="/usr/bin/websearch"),
        patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=OSError("spawn failed"),
        ),
    ):
        result = await _ws._run_websearch("search", "--query", "test")

    assert "error" in result
