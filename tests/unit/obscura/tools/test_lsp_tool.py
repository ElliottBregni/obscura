"""Unit tests for obscura.tools.lsp.

Tests cover:
  - No-manager early return
  - No-client early return
  - Each of the four operations with a mock client
  - Unknown operation returns error
  - Exception from client is caught and returned as JSON error
  - _format_location / _format_locations helpers
"""

from __future__ import annotations

import json
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest

import obscura.tools.lsp as _lsp_mod
from obscura.tools.lsp import (
    lsp_tool,
    set_lsp_manager,
    _format_location,
    _format_locations,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers — _format_location / _format_locations
# ---------------------------------------------------------------------------


def test_format_location_file_uri() -> None:
    loc = {
        "uri": "file:///home/user/src/main.py",
        "range": {"start": {"line": 9, "character": 3}},
    }
    result = _format_location(loc)
    assert result == "/home/user/src/main.py:10:4"


def test_format_location_plain_path() -> None:
    loc = {
        "uri": "/src/app.py",
        "range": {"start": {"line": 0, "character": 0}},
    }
    result = _format_location(loc)
    assert result == "/src/app.py:1:1"


def test_format_locations_none_returns_empty() -> None:
    assert _format_locations(None) == []


def test_format_locations_single_dict_wrapped() -> None:
    loc: dict = {
        "uri": "file:///a.py",
        "range": {"start": {"line": 4, "character": 1}},
    }
    result = _format_locations(loc)
    assert len(result) == 1
    assert result[0].endswith(":5:2")


def test_format_locations_list() -> None:
    locs = [
        {"uri": "file:///a.py", "range": {"start": {"line": 0, "character": 0}}},
        {"uri": "file:///b.py", "range": {"start": {"line": 1, "character": 2}}},
    ]
    result = _format_locations(locs)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Fixture: reset module _lsp_manager after each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_manager() -> Generator[None]:
    original = _lsp_mod._lsp_manager
    yield
    _lsp_mod._lsp_manager = original


# ---------------------------------------------------------------------------
# No-manager case
# ---------------------------------------------------------------------------


async def test_lsp_tool_no_manager_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When lazy init cannot produce a manager, lsp_tool returns a clear error."""

    async def _unavailable() -> None:
        return None

    # Patch _ensure_lsp_manager so it returns None, simulating a failed lazy
    # init (e.g. LSPServerManager import error or constructor failure).
    monkeypatch.setattr(_lsp_mod, "_ensure_lsp_manager", _unavailable)

    result = json.loads(await lsp_tool("goToDefinition", "/src/main.py"))

    assert result["ok"] is False
    assert result["error"] == "lsp_not_available"


# ---------------------------------------------------------------------------
# No-client case
# ---------------------------------------------------------------------------


async def test_lsp_tool_no_client_returns_error() -> None:
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=None)
    set_lsp_manager(manager)

    result = json.loads(await lsp_tool("goToDefinition", "/src/main.py"))

    assert result["ok"] is False
    assert result["error"] == "no_server"


# ---------------------------------------------------------------------------
# goToDefinition
# ---------------------------------------------------------------------------


async def test_lsp_tool_goto_definition_returns_locations() -> None:
    client = MagicMock()
    client.goto_definition = AsyncMock(
        return_value=[
            {
                "uri": "file:///src/defs.py",
                "range": {"start": {"line": 9, "character": 0}},
            }
        ]
    )
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=client)
    set_lsp_manager(manager)

    result = json.loads(
        await lsp_tool("goToDefinition", "/src/main.py", line=5, character=10)
    )

    assert result["ok"] is True
    assert result["count"] == 1
    assert "/src/defs.py:10:1" in result["results"]


async def test_lsp_tool_goto_definition_no_results() -> None:
    client = MagicMock()
    client.goto_definition = AsyncMock(return_value=None)
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=client)
    set_lsp_manager(manager)

    result = json.loads(await lsp_tool("goToDefinition", "/src/main.py"))

    assert result["ok"] is True
    assert result["count"] == 0


# ---------------------------------------------------------------------------
# findReferences
# ---------------------------------------------------------------------------


async def test_lsp_tool_find_references_returns_locations() -> None:
    client = MagicMock()
    client.find_references = AsyncMock(
        return_value=[
            {"uri": "file:///a.py", "range": {"start": {"line": 2, "character": 0}}},
            {"uri": "file:///b.py", "range": {"start": {"line": 7, "character": 4}}},
        ]
    )
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=client)
    set_lsp_manager(manager)

    result = json.loads(await lsp_tool("findReferences", "/src/main.py"))

    assert result["ok"] is True
    assert result["count"] == 2


# ---------------------------------------------------------------------------
# hover
# ---------------------------------------------------------------------------


async def test_lsp_tool_hover_string_contents() -> None:
    client = MagicMock()
    client.hover = AsyncMock(return_value={"contents": "def my_func() -> None"})
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=client)
    set_lsp_manager(manager)

    result = json.loads(await lsp_tool("hover", "/src/main.py"))

    assert result["ok"] is True
    assert "my_func" in result["content"]


async def test_lsp_tool_hover_dict_contents() -> None:
    client = MagicMock()
    client.hover = AsyncMock(
        return_value={"contents": {"kind": "markdown", "value": "**docs here**"}}
    )
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=client)
    set_lsp_manager(manager)

    result = json.loads(await lsp_tool("hover", "/src/main.py"))

    assert "docs here" in result["content"]


async def test_lsp_tool_hover_list_contents() -> None:
    client = MagicMock()
    client.hover = AsyncMock(
        return_value={"contents": [{"value": "part1"}, {"value": "part2"}]}
    )
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=client)
    set_lsp_manager(manager)

    result = json.loads(await lsp_tool("hover", "/src/main.py"))

    assert "part1" in result["content"]
    assert "part2" in result["content"]


async def test_lsp_tool_hover_no_contents() -> None:
    client = MagicMock()
    client.hover = AsyncMock(return_value=None)
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=client)
    set_lsp_manager(manager)

    result = json.loads(await lsp_tool("hover", "/src/main.py"))

    assert result["ok"] is True
    assert result["content"] == ""


# ---------------------------------------------------------------------------
# documentSymbol
# ---------------------------------------------------------------------------


async def test_lsp_tool_document_symbol_returns_symbols() -> None:
    client = MagicMock()
    client.document_symbols = AsyncMock(
        return_value=[
            {
                "name": "MyClass",
                "kind": 5,
                "range": {"start": {"line": 10, "character": 0}},
            },
            {
                "name": "my_func",
                "kind": 12,
                "range": {"start": {"line": 20, "character": 4}},
            },
        ]
    )
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=client)
    set_lsp_manager(manager)

    result = json.loads(await lsp_tool("documentSymbol", "/src/main.py"))

    assert result["ok"] is True
    assert result["count"] == 2
    names = [s["name"] for s in result["symbols"]]
    assert "MyClass" in names


# ---------------------------------------------------------------------------
# Unknown operation
# ---------------------------------------------------------------------------


async def test_lsp_tool_unknown_operation_returns_error() -> None:
    client = MagicMock()
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=client)
    set_lsp_manager(manager)

    result = json.loads(await lsp_tool("flyToMoon", "/src/main.py"))

    assert result["ok"] is False
    assert result["error"] == "unknown_operation"


# ---------------------------------------------------------------------------
# Exception in client
# ---------------------------------------------------------------------------


async def test_lsp_tool_client_exception_caught() -> None:
    client = MagicMock()
    client.goto_definition = AsyncMock(side_effect=RuntimeError("server crashed"))
    manager = MagicMock()
    manager.get_client = AsyncMock(return_value=client)
    set_lsp_manager(manager)

    result = json.loads(await lsp_tool("goToDefinition", "/src/main.py"))

    assert result["ok"] is False
    assert result["error"] == "lsp_error"
    assert "server crashed" in result["detail"]
