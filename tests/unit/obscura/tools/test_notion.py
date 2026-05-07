"""Unit tests for obscura.tools.providers.notion.

Async handlers using httpx. Mocked with respx.
NOTION_API_KEY set via monkeypatch (not required for most error paths).
"""
from __future__ import annotations

import httpx
import pytest
import respx

import obscura.tools.providers.notion as _notion

pytestmark = pytest.mark.unit

_BASE = "https://api.notion.com"


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_API_KEY", "test-key")


# ---------------------------------------------------------------------------
# _handler_search
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_search_returns_results() -> None:
    respx.post(f"{_BASE}/v1/search").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "abc"}]})
    )

    result = await _notion._handler_search(query="meeting notes")

    assert "results" in result


@respx.mock
async def test_handler_search_http_error_returns_error() -> None:
    respx.post(f"{_BASE}/v1/search").mock(
        return_value=httpx.Response(401, json={"message": "unauthorized"})
    )

    result = await _notion._handler_search(query="x")

    assert "error" in result


@respx.mock
async def test_handler_search_empty_query_still_calls_api() -> None:
    route = respx.post(f"{_BASE}/v1/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    await _notion._handler_search()

    assert route.called


# ---------------------------------------------------------------------------
# _handler_get_page
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_get_page_returns_page_data() -> None:
    page_id = "page-123"
    respx.get(f"{_BASE}/v1/pages/{page_id}").mock(
        return_value=httpx.Response(200, json={"id": page_id, "object": "page"})
    )

    result = await _notion._handler_get_page(page_id=page_id)

    assert result["id"] == page_id


async def test_handler_get_page_missing_page_id_returns_error() -> None:
    result = await _notion._handler_get_page()

    assert "error" in result
    assert "page_id" in result["error"]


@respx.mock
async def test_handler_get_page_404_returns_error() -> None:
    respx.get(f"{_BASE}/v1/pages/bad-id").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )

    result = await _notion._handler_get_page(page_id="bad-id")

    assert "error" in result


# ---------------------------------------------------------------------------
# _handler_query_database
# ---------------------------------------------------------------------------


async def test_handler_query_database_missing_id_returns_error() -> None:
    result = await _notion._handler_query_database()

    assert "error" in result
    assert "database_id" in result["error"]


@respx.mock
async def test_handler_query_database_returns_results() -> None:
    db_id = "db-456"
    respx.post(f"{_BASE}/v1/databases/{db_id}/query").mock(
        return_value=httpx.Response(200, json={"results": [], "object": "list"})
    )

    result = await _notion._handler_query_database(database_id=db_id)

    assert "results" in result


# ---------------------------------------------------------------------------
# healthcheck
# ---------------------------------------------------------------------------


@respx.mock
async def test_healthcheck_with_api_key_healthy() -> None:
    respx.get(f"{_BASE}/v1/users/me").mock(
        return_value=httpx.Response(200, json={"object": "user"})
    )

    result = await _notion.healthcheck()

    assert result["status"] == "healthy"


async def test_healthcheck_no_api_key_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)

    result = await _notion.healthcheck()

    assert result["status"] == "unhealthy"
    assert "NOTION_API_KEY" in result.get("error", "")
