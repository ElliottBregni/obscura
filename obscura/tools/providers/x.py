"""X (Twitter) API v2 provider handlers."""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.twitter.com"


def _bearer() -> str:
    return os.environ.get("X_BEARER_TOKEN", "")


def _headers_read() -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer()}"}


def _headers_write() -> dict[str, str]:
    token = os.environ.get("X_ACCESS_TOKEN", _bearer())
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            base_url=_BASE, headers=_headers_read(), timeout=15
        ) as c:
            r = await c.get(path, params=params)
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
    except Exception as e:
        logger.warning("X API GET %s failed: %s", path, e)
        return {"error": str(e)}


async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            base_url=_BASE, headers=_headers_write(), timeout=15
        ) as c:
            r = await c.post(path, json=body)
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
    except Exception as e:
        logger.warning("X API POST %s failed: %s", path, e)
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Read handlers
# ---------------------------------------------------------------------------


async def _handler_search_tweets(
    query: str = "",
    max_results: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    return await _get(
        "/2/tweets/search/recent",
        {"query": query, "max_results": max_results},
    )


async def _handler_get_user(
    username: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    return await _get(f"/2/users/by/username/{username}")


async def _handler_get_tweet(
    tweet_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    return await _get(f"/2/tweets/{tweet_id}")


async def _handler_user_timeline(
    user_id: str = "",
    max_results: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    return await _get(
        f"/2/users/{user_id}/tweets",
        {"max_results": max_results},
    )


async def _handler_user_mentions(
    user_id: str = "",
    max_results: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    return await _get(
        f"/2/users/{user_id}/mentions",
        {"max_results": max_results},
    )


# ---------------------------------------------------------------------------
# Write handlers
# ---------------------------------------------------------------------------


async def _handler_post_tweet(
    text: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    return await _post("/2/tweets", {"text": text})


async def _handler_reply_tweet(
    text: str = "",
    in_reply_to_tweet_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    return await _post(
        "/2/tweets",
        {"text": text, "reply": {"in_reply_to_tweet_id": in_reply_to_tweet_id}},
    )


async def _handler_retweet(
    tweet_id: str = "",
    user_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    return await _post(
        f"/2/users/{user_id}/retweets",
        {"tweet_id": tweet_id},
    )


async def _handler_like_tweet(
    tweet_id: str = "",
    user_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    return await _post(
        f"/2/users/{user_id}/likes",
        {"tweet_id": tweet_id},
    )


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


async def healthcheck() -> dict[str, Any]:
    if not _bearer():
        return {"status": "unhealthy", "error": "X_BEARER_TOKEN not set"}
    result = await _get("/2/users/me")
    if "data" in result:
        return {"status": "healthy"}
    return {"status": "unhealthy", "error": result.get("error", "unknown")}
