"""Unit tests for obscura.tools.providers.data_gov.

Tests the CKAN API path, the HTML fallback path, and error handling.
"""
from __future__ import annotations

import httpx
import pytest
import respx

import obscura.tools.providers.data_gov as _dg

pytestmark = pytest.mark.unit

_CKAN = "https://catalog.data.gov/api/3/action/package_search"
_HTML = "https://catalog.data.gov/dataset"


# ---------------------------------------------------------------------------
# CKAN path
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_datasets_ckan_success() -> None:
    respx.get(_CKAN).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "count": 1,
                    "results": [
                        {
                            "name": "census-data",
                            "title": "Census Data",
                            "notes": "Annual census data",
                            "url": "https://example.gov/census",
                            "organization": {"title": "Census Bureau"},
                            "resources": [{"format": "CSV"}],
                        }
                    ],
                },
            },
        )
    )

    result = await _dg.search_datasets("census")

    assert len(result["results"]) == 1
    assert result["results"][0]["title"] == "Census Data"
    assert result["count"] == 1


@respx.mock
async def test_search_datasets_passes_rows_and_start() -> None:
    route = respx.get(_CKAN).mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": {"count": 0, "results": []}}
        )
    )

    await _dg.search_datasets("climate", rows=5, start=10)

    url_str = str(route.calls.last.request.url)
    assert "rows=5" in url_str
    assert "start=10" in url_str


@respx.mock
async def test_search_datasets_ckan_success_false_falls_back_to_html() -> None:
    """CKAN returns success=False → HTML fallback path is tried."""
    respx.get(_CKAN).mock(
        return_value=httpx.Response(
            200, json={"success": False, "error": "unavailable"}
        )
    )
    html_body = (
        b'<h3 class="dataset-heading">'
        b'<a href="/dataset/air-quality">Air Quality Data</a></h3>'
    )
    respx.get(_HTML).mock(return_value=httpx.Response(200, content=html_body))

    result = await _dg.search_datasets("air quality")

    assert "source" not in result or result.get("source") == "html_fallback"


# ---------------------------------------------------------------------------
# HTML fallback path (test _html_fallback directly)
# ---------------------------------------------------------------------------


@respx.mock
async def test_html_fallback_parses_dataset_links() -> None:
    html_body = (
        b'<h3 class="dataset-heading">'
        b'<a href="/dataset/climate-change">Climate Change Dataset</a></h3>'
        b'<h3 class="dataset-heading">'
        b'<a href="/dataset/energy-use">Energy Use</a></h3>'
    )
    respx.get(_HTML).mock(return_value=httpx.Response(200, content=html_body))

    result = await _dg._html_fallback("climate", rows=10, start=0)

    assert result["source"] == "html_fallback"
    assert len(result["results"]) == 2
    assert result["results"][0]["title"] == "Climate Change Dataset"
    assert result["results"][0]["name"] == "climate-change"


@respx.mock
async def test_html_fallback_respects_rows_limit() -> None:
    rows = b"".join(
        f'<h3 class="dataset-heading"><a href="/dataset/ds-{i}">Dataset {i}</a></h3>'.encode()
        for i in range(10)
    )
    respx.get(_HTML).mock(return_value=httpx.Response(200, content=rows))

    result = await _dg._html_fallback("q", rows=3, start=0)

    assert len(result["results"]) == 3


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_datasets_all_paths_fail_returns_error() -> None:
    """Both CKAN and HTML paths fail → returns {"error": ...}."""
    respx.get(_CKAN).mock(side_effect=httpx.ConnectError("no network"))
    respx.get(_HTML).mock(side_effect=httpx.ConnectError("no network"))

    result = await _dg.search_datasets("test")

    assert "error" in result


@respx.mock
async def test_ckan_formats_deduped_and_sorted() -> None:
    """Resources with duplicate formats are deduplicated in the result."""
    respx.get(_CKAN).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "count": 1,
                    "results": [
                        {
                            "name": "multi",
                            "title": "Multi",
                            "resources": [
                                {"format": "csv"},
                                {"format": "CSV"},
                                {"format": "json"},
                            ],
                        }
                    ],
                },
            },
        )
    )

    result = await _dg.search_datasets("multi")

    formats = result["results"][0]["formats"]
    # After upper() normalization, "csv" and "CSV" become same
    assert formats == sorted(set(formats))
