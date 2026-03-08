"""Alpha Vantage financial data provider handlers."""
from __future__ import annotations

import os

import httpx

BASE_URL = "https://www.alphavantage.co/query"


def _api_key() -> str:
    return os.environ["ALPHAVANTAGE_API_KEY"]


async def _get(params: dict) -> dict:
    params["apikey"] = _api_key()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(BASE_URL, params=params)
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
    except Exception as e:
        return {"error": str(e)}


# ── Quotes & Time Series ─────────────────────────────────────────────


async def _handler_quote(symbol: str, **kwargs: object) -> dict:
    return await _get({"function": "GLOBAL_QUOTE", "symbol": symbol})


async def _handler_intraday(
    symbol: str, interval: str = "5min", **kwargs: object
) -> dict:
    return await _get({
        "function": "TIME_SERIES_INTRADAY",
        "symbol": symbol,
        "interval": interval,
    })


async def _handler_daily(symbol: str, **kwargs: object) -> dict:
    return await _get({"function": "TIME_SERIES_DAILY", "symbol": symbol})


async def _handler_weekly(symbol: str, **kwargs: object) -> dict:
    return await _get({"function": "TIME_SERIES_WEEKLY", "symbol": symbol})


async def _handler_monthly(symbol: str, **kwargs: object) -> dict:
    return await _get({"function": "TIME_SERIES_MONTHLY", "symbol": symbol})


# ── Technical Indicators ──────────────────────────────────────────────


async def _handler_sma(
    symbol: str,
    interval: str = "daily",
    time_period: int = 20,
    series_type: str = "close",
    **kwargs: object,
) -> dict:
    return await _get({
        "function": "SMA",
        "symbol": symbol,
        "interval": interval,
        "time_period": time_period,
        "series_type": series_type,
    })


async def _handler_ema(
    symbol: str,
    interval: str = "daily",
    time_period: int = 20,
    series_type: str = "close",
    **kwargs: object,
) -> dict:
    return await _get({
        "function": "EMA",
        "symbol": symbol,
        "interval": interval,
        "time_period": time_period,
        "series_type": series_type,
    })


async def _handler_rsi(
    symbol: str,
    interval: str = "daily",
    time_period: int = 14,
    series_type: str = "close",
    **kwargs: object,
) -> dict:
    return await _get({
        "function": "RSI",
        "symbol": symbol,
        "interval": interval,
        "time_period": time_period,
        "series_type": series_type,
    })


async def _handler_macd(
    symbol: str,
    interval: str = "daily",
    series_type: str = "close",
    **kwargs: object,
) -> dict:
    return await _get({
        "function": "MACD",
        "symbol": symbol,
        "interval": interval,
        "series_type": series_type,
    })


async def _handler_bbands(
    symbol: str,
    interval: str = "daily",
    time_period: int = 20,
    series_type: str = "close",
    **kwargs: object,
) -> dict:
    return await _get({
        "function": "BBANDS",
        "symbol": symbol,
        "interval": interval,
        "time_period": time_period,
        "series_type": series_type,
    })


async def _handler_stoch(
    symbol: str, interval: str = "daily", **kwargs: object
) -> dict:
    return await _get({
        "function": "STOCH",
        "symbol": symbol,
        "interval": interval,
    })


async def _handler_adx(
    symbol: str,
    interval: str = "daily",
    time_period: int = 14,
    **kwargs: object,
) -> dict:
    return await _get({
        "function": "ADX",
        "symbol": symbol,
        "interval": interval,
        "time_period": time_period,
    })


async def _handler_cci(
    symbol: str,
    interval: str = "daily",
    time_period: int = 20,
    **kwargs: object,
) -> dict:
    return await _get({
        "function": "CCI",
        "symbol": symbol,
        "interval": interval,
        "time_period": time_period,
    })


async def _handler_aroon(
    symbol: str,
    interval: str = "daily",
    time_period: int = 14,
    **kwargs: object,
) -> dict:
    return await _get({
        "function": "AROON",
        "symbol": symbol,
        "interval": interval,
        "time_period": time_period,
    })


async def _handler_ad(
    symbol: str, interval: str = "daily", **kwargs: object
) -> dict:
    return await _get({
        "function": "AD",
        "symbol": symbol,
        "interval": interval,
    })


async def _handler_obv(
    symbol: str, interval: str = "daily", **kwargs: object
) -> dict:
    return await _get({
        "function": "OBV",
        "symbol": symbol,
        "interval": interval,
    })


# ── Fundamentals ──────────────────────────────────────────────────────


async def _handler_overview(symbol: str, **kwargs: object) -> dict:
    return await _get({"function": "OVERVIEW", "symbol": symbol})


async def _handler_income_statement(symbol: str, **kwargs: object) -> dict:
    return await _get({"function": "INCOME_STATEMENT", "symbol": symbol})


async def _handler_balance_sheet(symbol: str, **kwargs: object) -> dict:
    return await _get({"function": "BALANCE_SHEET", "symbol": symbol})


async def _handler_cash_flow(symbol: str, **kwargs: object) -> dict:
    return await _get({"function": "CASH_FLOW", "symbol": symbol})


async def _handler_earnings(symbol: str, **kwargs: object) -> dict:
    return await _get({"function": "EARNINGS", "symbol": symbol})


# ── Forex ─────────────────────────────────────────────────────────────


async def _handler_currency_exchange_rate(
    from_currency: str, to_currency: str, **kwargs: object
) -> dict:
    return await _get({
        "function": "CURRENCY_EXCHANGE_RATE",
        "from_currency": from_currency,
        "to_currency": to_currency,
    })


# ── Healthcheck ───────────────────────────────────────────────────────


async def healthcheck(**kwargs: object) -> dict:
    return await _get({"function": "GLOBAL_QUOTE", "symbol": "IBM"})
