"""CoinGecko free-tier provider handlers."""

from __future__ import annotations

from typing import Any, cast

import httpx

BASE_URL = "https://api.coingecko.com/api/v3"

__all__ = [
    "_handler_market_chart",
    "_handler_price",
    "_handler_trending",
]


def _coerce_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    return {"data": value}


async def _handler_price(
    ids: str,
    vs_currencies: str = "usd",
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/simple/price",
                params={"ids": ids, "vs_currencies": vs_currencies},
            )
            resp.raise_for_status()
            return _coerce_dict(resp.json())
    except Exception as e:
        return {"error": str(e)}


async def _handler_market_chart(
    id: str,
    vs_currency: str = "usd",
    days: int = 7,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/coins/{id}/market_chart",
                params={"vs_currency": vs_currency, "days": days},
            )
            resp.raise_for_status()
            return _coerce_dict(resp.json())
    except Exception as e:
        return {"error": str(e)}


async def _handler_trending(**kwargs: Any) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_URL}/search/trending")
            resp.raise_for_status()
            return _coerce_dict(resp.json())
    except Exception as e:
        return {"error": str(e)}
