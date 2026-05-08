"""Unit tests for obscura.tools.providers.x (X/Twitter API v2).

Async handlers using httpx. Mocked with respx.
X_BEARER_TOKEN set via monkeypatch.
"""

from __future__ import annotations

import httpx
import pytest
import respx

import obscura.tools.providers.x as _x

pytestmark = pytest.mark.unit

_BASE = "https://api.twitter.com"


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_BEARER_TOKEN", "test-bearer-token")
    monkeypatch.setenv("X_ACCESS_TOKEN", "test-access-token")


# ---------------------------------------------------------------------------
# _handler_search_tweets
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_search_tweets_returns_data() -> None:
    respx.get(f"{_BASE}/2/tweets/search/recent").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "1", "text": "hello"}]})
    )

    result = await _x._handler_search_tweets(query="hello", max_results=1)

    assert "data" in result


@respx.mock
async def test_handler_search_tweets_http_error_returns_error() -> None:
    respx.get(f"{_BASE}/2/tweets/search/recent").mock(
        return_value=httpx.Response(401, json={"errors": [{"message": "unauthorized"}]})
    )

    result = await _x._handler_search_tweets(query="test")

    assert "error" in result


# ---------------------------------------------------------------------------
# _handler_get_user
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_get_user_returns_user_data() -> None:
    respx.get(f"{_BASE}/2/users/by/username/testuser").mock(
        return_value=httpx.Response(
            200, json={"data": {"id": "123", "username": "testuser"}}
        )
    )

    result = await _x._handler_get_user(username="testuser")

    assert result["data"]["username"] == "testuser"


@respx.mock
async def test_handler_get_user_not_found_returns_error() -> None:
    respx.get(f"{_BASE}/2/users/by/username/nobody").mock(
        return_value=httpx.Response(404, json={"errors": [{"detail": "not found"}]})
    )

    result = await _x._handler_get_user(username="nobody")

    assert "error" in result


# ---------------------------------------------------------------------------
# _handler_get_tweet
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_get_tweet_returns_tweet() -> None:
    respx.get(f"{_BASE}/2/tweets/99999").mock(
        return_value=httpx.Response(200, json={"data": {"id": "99999", "text": "hi"}})
    )

    result = await _x._handler_get_tweet(tweet_id="99999")

    assert result["data"]["id"] == "99999"


# ---------------------------------------------------------------------------
# _handler_user_timeline / _handler_user_mentions
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_user_timeline_returns_tweets() -> None:
    respx.get(f"{_BASE}/2/users/42/tweets").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    result = await _x._handler_user_timeline(user_id="42")

    assert "data" in result


@respx.mock
async def test_handler_user_mentions_returns_mentions() -> None:
    respx.get(f"{_BASE}/2/users/42/mentions").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "5"}]})
    )

    result = await _x._handler_user_mentions(user_id="42")

    assert "data" in result


# ---------------------------------------------------------------------------
# Write handlers — POST
# ---------------------------------------------------------------------------


@respx.mock
async def test_handler_post_tweet_creates_tweet() -> None:
    respx.post(f"{_BASE}/2/tweets").mock(
        return_value=httpx.Response(
            201, json={"data": {"id": "new-id", "text": "hello"}}
        )
    )

    result = await _x._handler_post_tweet(text="hello")

    assert "data" in result


@respx.mock
async def test_handler_reply_tweet_creates_reply() -> None:
    respx.post(f"{_BASE}/2/tweets").mock(
        return_value=httpx.Response(201, json={"data": {"id": "reply-id"}})
    )

    result = await _x._handler_reply_tweet(
        text="reply", in_reply_to_tweet_id="original-1"
    )

    assert "data" in result


@respx.mock
async def test_handler_retweet_calls_correct_endpoint() -> None:
    route = respx.post(f"{_BASE}/2/users/7/retweets").mock(
        return_value=httpx.Response(200, json={"data": {"retweeted": True}})
    )

    await _x._handler_retweet(tweet_id="5", user_id="7")

    assert route.called


@respx.mock
async def test_handler_like_tweet_calls_correct_endpoint() -> None:
    route = respx.post(f"{_BASE}/2/users/7/likes").mock(
        return_value=httpx.Response(200, json={"data": {"liked": True}})
    )

    await _x._handler_like_tweet(tweet_id="5", user_id="7")

    assert route.called


# ---------------------------------------------------------------------------
# healthcheck
# ---------------------------------------------------------------------------


@respx.mock
async def test_healthcheck_healthy() -> None:
    respx.get(f"{_BASE}/2/users/me").mock(
        return_value=httpx.Response(200, json={"data": {"id": "me"}})
    )

    result = await _x.healthcheck()

    assert result["status"] == "healthy"


async def test_healthcheck_no_token_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)

    result = await _x.healthcheck()

    assert result["status"] == "unhealthy"
    assert "X_BEARER_TOKEN" in result.get("error", "")


@respx.mock
async def test_healthcheck_api_error_unhealthy() -> None:
    respx.get(f"{_BASE}/2/users/me").mock(
        return_value=httpx.Response(401, json={"errors": [{"message": "unauthorized"}]})
    )

    result = await _x.healthcheck()

    assert result["status"] == "unhealthy"
