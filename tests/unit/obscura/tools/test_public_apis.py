"""Unit tests for obscura.tools.providers.public_apis.

PublicAPIsProvider dispatches based on kwargs. Tests cover all branches:
  list_persisted, remove_persisted, create_tool, discover (with filters),
  and name_or_link lookup. File I/O is redirected to tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

import obscura.tools.providers.public_apis as _pa

pytestmark = pytest.mark.unit

_BASE = "https://api.publicapis.org"


@pytest.fixture(autouse=True)
def _redirect_persist_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect _PERSIST_PATH to a tmp file so tests never touch ~/.obscura."""
    fake_path = tmp_path / "public_apis_tools.json"
    monkeypatch.setattr(_pa, "_PERSIST_PATH", fake_path)


# ---------------------------------------------------------------------------
# list_persisted
# ---------------------------------------------------------------------------


async def test_list_persisted_empty_when_no_file() -> None:
    result = await _pa.PublicAPIsProvider(action="list_persisted")

    assert result["tools"] == []


async def test_list_persisted_returns_saved_tools(tmp_path: Path) -> None:
    fake_path = tmp_path / "public_apis_tools.json"
    fake_path.write_text(json.dumps([{"name": "my-api"}]))
    with patch.object(_pa, "_PERSIST_PATH", fake_path):
        result = await _pa.PublicAPIsProvider(list_persisted=True)

    assert result["tools"][0]["name"] == "my-api"


# ---------------------------------------------------------------------------
# create_tool
# ---------------------------------------------------------------------------


async def test_create_tool_returns_tool_def() -> None:
    result = await _pa.PublicAPIsProvider(
        action="create_tool",
        tool_name="my-tool",
        api_name="My API",
        method="GET",
        path="/data",
        persist=False,
    )

    assert "created" in result
    assert result["created"]["name"] == "my-tool"


async def test_create_tool_persists_when_flag_true(tmp_path: Path) -> None:
    fake_path = tmp_path / "public_apis_tools.json"
    with patch.object(_pa, "_PERSIST_PATH", fake_path):
        await _pa.PublicAPIsProvider(
            action="create_tool",
            tool_name="saved-tool",
            api_name="Saved API",
            persist=True,
        )
        saved = json.loads(fake_path.read_text())

    assert any(t["name"] == "saved-tool" for t in saved)


# ---------------------------------------------------------------------------
# remove_persisted
# ---------------------------------------------------------------------------


async def test_remove_persisted_removes_by_name(tmp_path: Path) -> None:
    fake_path = tmp_path / "public_apis_tools.json"
    fake_path.write_text(json.dumps([{"name": "tool-a"}, {"name": "tool-b"}]))
    with patch.object(_pa, "_PERSIST_PATH", fake_path):
        result = await _pa.PublicAPIsProvider(
            action="remove_persisted", tool_name="tool-a"
        )

    assert result["removed"] == "tool-a"
    assert result["remaining"] == 1


async def test_remove_persisted_missing_name_is_noop() -> None:
    result = await _pa.PublicAPIsProvider(
        action="remove_persisted", tool_name="nonexistent"
    )

    assert result["removed"] == "nonexistent"
    assert result["remaining"] == 0


# ---------------------------------------------------------------------------
# discover — name_or_link path
# ---------------------------------------------------------------------------


@respx.mock
async def test_name_or_link_calls_discover() -> None:
    respx.get(f"{_BASE}/entries").mock(
        return_value=httpx.Response(
            200, json={"entries": [{"API": "Example", "Link": "https://ex.com"}]}
        )
    )

    result = await _pa.PublicAPIsProvider(name_or_link="example")

    assert "entries" in result


# ---------------------------------------------------------------------------
# discover — default path with filters
# ---------------------------------------------------------------------------


@respx.mock
async def test_discover_with_category_filter() -> None:
    route = respx.get(f"{_BASE}/entries").mock(
        return_value=httpx.Response(200, json={"entries": []})
    )

    await _pa.PublicAPIsProvider(category="Finance")

    assert "category=Finance" in str(route.calls.last.request.url)


@respx.mock
async def test_discover_with_https_filter() -> None:
    route = respx.get(f"{_BASE}/entries").mock(
        return_value=httpx.Response(200, json={"entries": []})
    )

    await _pa.PublicAPIsProvider(https=True)

    assert "https=true" in str(route.calls.last.request.url)


@respx.mock
async def test_discover_network_error_returns_error() -> None:
    respx.get(f"{_BASE}/entries").mock(side_effect=httpx.ConnectError("no network"))

    result = await _pa.PublicAPIsProvider()

    assert "error" in result
