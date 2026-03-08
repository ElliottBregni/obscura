"""CoinGecko free-tier provider handlers."""
from __future__ import annotations

import httpx

BASE_URL = "https://api.coingecko.com/api/v3"


async def _handler_price(
    ids: str,
    vs_currencies: str = "usd",
    **kwargs: object,
) -> dict:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/simple/price",
                params={"ids": ids, "vs_currencies": vs_currencies},
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
    except Exception as e:
        return {"error": str(e)}


async def _handler_market_chart(
    id: str,
    vs_currency: str = "usd",
    days: int = 7,
    **kwargs: object,
) -> dict:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/coins/{id}/market_chart",
                params={"vs_currency": vs_currency, "days": days},
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
    except Exception as e:
        return {"error": str(e)}


async def _handler_trending(**kwargs: object) -> dict:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_URL}/search/trending")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
    except Exception as e:
        return {"error": str(e)}
