"""Unit tests for obscura.tools.providers.alphavantage.

Async handlers all route through _get(), which requires ALPHAVANTAGE_API_KEY.
Mock strategy:
  - Set env var via monkeypatch so _api_key() doesn't raise.
  - Mock httpx via respx.
"""
from __future__ import annotations

import httpx
import pytest
import respx

import obscura.tools.providers.alphavantage as _av

pytestmark = pytest.mark.unit

_BASE = "https://www.alphavantage.co/query"


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test-key")


# ---------------------------------------------------------------------------
# _handler_quote
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_quote_returns_dict() -> None:
    respx.get(_BASE).mock(
        return_value=httpx.Response(200, json={"Global Quote": {"01. symbol": "IBM"}})
    )

    result = await _av._handler_quote("IBM")

    assert "Global Quote" in result


@respx.mock
async def test_handler_quote_includes_api_key_in_params() -> None:
    route = respx.get(_BASE).mock(
        return_value=httpx.Response(200, json={"Global Quote": {}})
    )

    await _av._handler_quote("IBM")

    assert "apikey=test-key" in str(route.calls.last.request.url)


@respx.mock
async def test_handler_quote_http_error_returns_error() -> None:
    respx.get(_BASE).mock(return_value=httpx.Response(403, text="forbidden"))

    result = await _av._handler_quote("IBM")

    assert "error" in result


# ---------------------------------------------------------------------------
# _handler_daily
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_daily_returns_time_series() -> None:
    respx.get(_BASE).mock(
        return_value=httpx.Response(
            200, json={"Time Series (Daily)": {"2024-01-01": {}}}
        )
    )

    result = await _av._handler_daily("AAPL")

    assert "Time Series (Daily)" in result


# ---------------------------------------------------------------------------
# _handler_sma
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_sma_passes_correct_function_param() -> None:
    route = respx.get(_BASE).mock(
        return_value=httpx.Response(200, json={"Technical Analysis: SMA": {}})
    )

    await _av._handler_sma("AAPL")

    assert "function=SMA" in str(route.calls.last.request.url)


# ---------------------------------------------------------------------------
# _handler_currency_exchange_rate
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_currency_exchange_rate_returns_rate() -> None:
    respx.get(_BASE).mock(
        return_value=httpx.Response(
            200, json={"Realtime Currency Exchange Rate": {"5. Exchange Rate": "1.25"}}
        )
    )

    result = await _av._handler_currency_exchange_rate("USD", "EUR")

    assert "Realtime Currency Exchange Rate" in result


# ---------------------------------------------------------------------------
# healthcheck
# ---------------------------------------------------------------------------


@respx.mock
async def test_healthcheck_returns_data() -> None:
    respx.get(_BASE).mock(
        return_value=httpx.Response(200, json={"Global Quote": {"01. symbol": "IBM"}})
    )

    result = await _av.healthcheck()

    assert "error" not in result


async def test_healthcheck_missing_api_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)

    # _api_key() raises KeyError *before* the try/except in _get(),
    # so the exception propagates all the way up.
    with pytest.raises(KeyError):
        await _av.healthcheck()
