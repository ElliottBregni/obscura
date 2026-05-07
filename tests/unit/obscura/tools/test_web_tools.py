"""Unit tests for Web.web_fetch and Web.web_search.

Both tools are async static methods on the Web class.

Mock strategy:
  - Patch url_request.urlopen to avoid real network calls.
  - Clear Web._cache in autouse fixture to prevent cache cross-contamination.
  - For SSRF: patch Policy.validate_url to raise ValueError.
  - For HTTP errors: use urllib.error.HTTPError with an io.BytesIO body.
"""
from __future__ import annotations

import io
import json
import time
from collections.abc import Generator
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

import obscura.tools.system._web as _web_mod
from obscura.tools.system._web import Web

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Autouse: clear class-level cache between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache() -> Generator[None, None, None]:
    Web._cache.clear()
    yield
    Web._cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_url_response(
    body: bytes,
    content_type: str = "text/plain; charset=utf-8",
    final_url: str = "https://example.com",
    status: int = 200,
) -> MagicMock:
    """Build a mock context-manager response for urlopen."""
    resp = MagicMock()
    resp.read.return_value = body
    resp.headers = MagicMock()
    resp.headers.items.return_value = [("Content-Type", content_type)]
    resp.geturl.return_value = final_url
    resp.status = status
    # Support `with urlopen(...) as response:`
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# web_fetch — basic success
# ---------------------------------------------------------------------------


async def test_web_fetch_plain_text_returns_ok() -> None:
    cm = _make_url_response(b"Hello, world!", content_type="text/plain")
    with patch.object(_web_mod.url_request, "urlopen", return_value=cm):
        result = json.loads(await Web.web_fetch("https://example.com"))

    assert result["ok"] is True
    assert "Hello" in result["body"]
    assert result["truncated"] is False


async def test_web_fetch_html_strips_tags() -> None:
    html = b"<html><body><h1>Title</h1><p>Content</p></body></html>"
    cm = _make_url_response(html, content_type="text/html; charset=utf-8")
    with patch.object(_web_mod.url_request, "urlopen", return_value=cm):
        result = json.loads(await Web.web_fetch("https://example.com"))

    assert result["ok"] is True
    assert "<html>" not in result["body"]
    assert "Title" in result["body"] or "Content" in result["body"]


async def test_web_fetch_includes_url_and_status() -> None:
    cm = _make_url_response(b"ok")
    with patch.object(_web_mod.url_request, "urlopen", return_value=cm):
        result = json.loads(await Web.web_fetch("https://example.com"))

    assert result["url"] == "https://example.com"
    assert result["status"] == 200


# ---------------------------------------------------------------------------
# web_fetch — caching
# ---------------------------------------------------------------------------


async def test_web_fetch_cache_hit_returns_cached_flag() -> None:
    url = "https://cached.example.com"
    cached_payload = json.dumps(
        {
            "ok": True,
            "url": url,
            "final_url": url,
            "status": 200,
            "content_type": "text/plain",
            "body": "cached content",
            "truncated": False,
            "bytes_read": 14,
        }
    )
    Web._cache[(url, "")] = (time.time(), cached_payload)

    result = json.loads(await Web.web_fetch(url))

    assert result["cached"] is True
    assert result["body"] == "cached content"


async def test_web_fetch_post_not_cached() -> None:
    """POST requests must not be written to the cache."""
    cm = _make_url_response(b"post response")
    with patch.object(_web_mod.url_request, "urlopen", return_value=cm):
        await Web.web_fetch("https://example.com", method="POST", body="data")

    assert ("https://example.com", "") not in Web._cache


# ---------------------------------------------------------------------------
# web_fetch — error paths
# ---------------------------------------------------------------------------


async def test_web_fetch_ssrf_blocked_returns_error() -> None:
    with patch.object(
        _web_mod.Policy,
        "validate_url",
        side_effect=ValueError("private address blocked"),
    ):
        result = json.loads(await Web.web_fetch("http://127.0.0.1/admin"))

    assert result["ok"] is False


async def test_web_fetch_http_404_returns_error_json() -> None:
    exc = HTTPError(
        "https://example.com/404",
        404,
        "Not Found",
        None,  # type: ignore[arg-type]
        io.BytesIO(b"page not found"),
    )
    with patch.object(_web_mod.url_request, "urlopen", side_effect=exc):
        result = json.loads(await Web.web_fetch("https://example.com/404"))

    assert result["ok"] is False
    assert result["status"] == 404
    assert result["error"] == "http_error"


async def test_web_fetch_network_error_returns_error_json() -> None:
    with patch.object(
        _web_mod.url_request,
        "urlopen",
        side_effect=URLError("connection refused"),
    ):
        result = json.loads(await Web.web_fetch("https://example.com"))

    assert result["ok"] is False


async def test_web_fetch_redirect_to_different_domain_sets_redirect_info() -> None:
    cm = _make_url_response(
        b"Redirected",
        content_type="text/plain",
        final_url="https://other-domain.example.org/path",
    )
    with patch.object(_web_mod.url_request, "urlopen", return_value=cm):
        result = json.loads(await Web.web_fetch("https://example.com"))

    assert result["ok"] is True
    assert "redirect" in result
    assert result["redirect"]["redirected"] is True


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------

_DDG_HTML = (
    b'<a class="result__a" href="https://example.com">Example Site</a>'
    b'<span class="result__snippet">An example website</span>'
    b'<a class="result__a" href="https://python.org">Python</a>'
    b'<span class="result__snippet">Python programming</span>'
)


async def test_web_search_returns_results() -> None:
    cm = _make_url_response(_DDG_HTML, content_type="text/html")
    with patch.object(_web_mod.url_request, "urlopen", return_value=cm):
        result = json.loads(await Web.web_search("test query"))

    assert result["ok"] is True
    assert result["count"] >= 1
    assert result["results"][0]["title"] == "Example Site"


async def test_web_search_respects_max_results() -> None:
    cm = _make_url_response(_DDG_HTML, content_type="text/html")
    with patch.object(_web_mod.url_request, "urlopen", return_value=cm):
        result = json.loads(await Web.web_search("q", max_results=1))

    assert result["count"] == 1
    assert len(result["results"]) == 1


async def test_web_search_blocked_domain_excluded() -> None:
    cm = _make_url_response(_DDG_HTML, content_type="text/html")
    with patch.object(_web_mod.url_request, "urlopen", return_value=cm):
        result = json.loads(
            await Web.web_search("q", blocked_domains=["example.com"])
        )

    urls = [r["url"] for r in result["results"]]
    assert not any("example.com" in u for u in urls)


async def test_web_search_network_error_returns_error() -> None:
    with patch.object(
        _web_mod.url_request,
        "urlopen",
        side_effect=URLError("no network"),
    ):
        result = json.loads(await Web.web_search("query"))

    assert result["ok"] is False
