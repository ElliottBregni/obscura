"""Unit tests for obscura.tools.providers.coingecko.

Async handlers using httpx. Mocked with respx.
"""

from __future__ import annotations

import httpx
import pytest
import respx

import obscura.tools.providers.coingecko as _cg

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _handler_price
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_price_returns_dict() -> None:
    respx.get("https://api.coingecko.com/api/v3/simple/price").mock(
        return_value=httpx.Response(200, json={"bitcoin": {"usd": 50000}})
    )

    result = await _cg._handler_price("bitcoin")

    assert result == {"bitcoin": {"usd": 50000}}


@respx.mock
async def test_handler_price_passes_vs_currencies() -> None:
    route = respx.get("https://api.coingecko.com/api/v3/simple/price").mock(
        return_value=httpx.Response(200, json={"eth": {"eur": 1000}})
    )

    await _cg._handler_price("ethereum", vs_currencies="eur")

    assert "vs_currencies=eur" in str(route.calls.last.request.url)


@respx.mock
async def test_handler_price_http_error_returns_error_dict() -> None:
    respx.get("https://api.coingecko.com/api/v3/simple/price").mock(
        return_value=httpx.Response(429, text="rate limited")
    )

    result = await _cg._handler_price("bitcoin")

    assert "error" in result


async def test_handler_price_network_error_returns_error_dict() -> None:
    with respx.mock:
        respx.get("https://api.coingecko.com/api/v3/simple/price").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        result = await _cg._handler_price("bitcoin")

    assert "error" in result


# ---------------------------------------------------------------------------
# _handler_market_chart
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_market_chart_returns_data() -> None:
    respx.get("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart").mock(
        return_value=httpx.Response(200, json={"prices": [[1000000, 50000]]})
    )

    result = await _cg._handler_market_chart("bitcoin")

    assert "prices" in result


@respx.mock
async def test_handler_market_chart_non_dict_response_coerced() -> None:
    """A non-dict JSON body is wrapped in {"data": ...}."""
    respx.get("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart").mock(
        return_value=httpx.Response(200, json=[1, 2, 3])
    )

    result = await _cg._handler_market_chart("bitcoin")

    assert "data" in result
    assert result["data"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# _handler_trending
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_trending_returns_coins() -> None:
    respx.get("https://api.coingecko.com/api/v3/search/trending").mock(
        return_value=httpx.Response(200, json={"coins": [{"item": {"id": "btc"}}]})
    )

    result = await _cg._handler_trending()

    assert "coins" in result


@respx.mock
async def test_handler_trending_500_error_returns_error() -> None:
    respx.get("https://api.coingecko.com/api/v3/search/trending").mock(
        return_value=httpx.Response(500, text="server error")
    )

    result = await _cg._handler_trending()

    assert "error" in result
