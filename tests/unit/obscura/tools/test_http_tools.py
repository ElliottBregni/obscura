"""Unit tests for HTTP and Web tools (Http.http_request, Http.download_file,
Web.web_fetch, Web.web_search)."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib import error as url_error

import pytest

import obscura.tools.system._http as _http_mod
import obscura.tools.system._web as _web_mod
from obscura.tools.system._http import Http
from obscura.tools.system._web import Web

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _full_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass path-allow checks (needed for download_file)."""
    monkeypatch.setenv("OBSCURA_UNSAFE_FULL_ACCESS", "1")


@pytest.fixture(autouse=True)
def _clear_web_cache() -> None:
    """Clear web fetch cache so prior test results don't bleed through."""
    Web._cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_response(
    body: bytes = b"hello world",
    status: int = 200,
    content_type: str = "text/plain",
) -> MagicMock:
    """Return a MagicMock that behaves like urllib urlopen context manager."""
    resp = MagicMock()
    resp.read = MagicMock(return_value=body)
    resp.status = status
    resp.headers = MagicMock()
    resp.headers.items = MagicMock(return_value=[("Content-Type", content_type)])
    resp.headers.get = MagicMock(
        side_effect=lambda k, default="": (
            content_type if k == "Content-Type" else default
        )
    )
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# Http.http_request
# ---------------------------------------------------------------------------


async def test_http_request_get_success() -> None:
    resp = _fake_response(body=b'{"key":"val"}', content_type="application/json")
    with (
        patch.object(_http_mod.Policy, "validate_url", side_effect=lambda u: u),
        patch.object(_http_mod.url_request, "urlopen", return_value=resp),
    ):
        result = json.loads(await Http.http_request(url="https://example.com/api"))

    assert result["ok"] is True
    assert result["status"] == 200
    assert result["method"] == "GET"


async def test_http_request_network_error() -> None:
    exc = url_error.URLError(reason="connection refused")
    with (
        patch.object(_http_mod.Policy, "validate_url", side_effect=lambda u: u),
        patch.object(_http_mod.url_request, "urlopen", side_effect=exc),
    ):
        result = json.loads(await Http.http_request(url="https://example.com"))

    assert result["ok"] is False


async def test_http_request_ssrf_blocked() -> None:
    """AWS metadata endpoint should be rejected by SSRF guard."""
    result = json.loads(await Http.http_request(url="http://169.254.169.254/latest"))
    assert result["ok"] is False
    assert "ssrf" in result.get("error", "").lower()


async def test_http_request_post_with_json_body() -> None:
    resp = _fake_response()
    with (
        patch.object(_http_mod.Policy, "validate_url", side_effect=lambda u: u),
        patch.object(_http_mod.url_request, "urlopen", return_value=resp) as mock_open,
    ):
        result = json.loads(
            await Http.http_request(
                url="https://api.example.com/items",
                method="POST",
                json_body={"name": "test"},
            )
        )

    assert result["ok"] is True
    mock_open.assert_called_once()


async def test_http_request_http_error_returns_error() -> None:
    """HTTP 404 → ok=False with status."""
    http_err = url_error.HTTPError(
        url="https://example.com",
        code=404,
        msg="Not Found",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,  # type: ignore[arg-type]
    )
    http_err.read = lambda n=-1: b"not found body"  # type: ignore[method-assign]
    with (
        patch.object(_http_mod.Policy, "validate_url", side_effect=lambda u: u),
        patch.object(_http_mod.url_request, "urlopen", side_effect=http_err),
    ):
        result = json.loads(await Http.http_request(url="https://example.com/gone"))

    assert result["ok"] is False
    assert result.get("status") == 404


# ---------------------------------------------------------------------------
# Http.download_file
# ---------------------------------------------------------------------------


async def test_download_file_writes_to_disk(tmp_path: Any) -> None:
    target = str(tmp_path / "out.bin")
    resp = _fake_response(body=b"binary content")
    with (
        patch.object(_http_mod.Policy, "validate_url", side_effect=lambda u: u),
        patch.object(_http_mod.url_request, "urlopen", return_value=resp),
    ):
        result = json.loads(
            await Http.download_file(url="https://files.example.com/f.bin", path=target)
        )

    assert result["ok"] is True
    assert Path(target).read_bytes() == b"binary content"


async def test_download_file_too_large_returns_error(tmp_path: Any) -> None:
    target = str(tmp_path / "big.bin")
    big_body = b"x" * 11
    resp = _fake_response(body=big_body)
    with (
        patch.object(_http_mod.Policy, "validate_url", side_effect=lambda u: u),
        patch.object(_http_mod.url_request, "urlopen", return_value=resp),
    ):
        result = json.loads(
            await Http.download_file(
                url="https://example.com/huge", path=target, max_bytes=10
            )
        )

    assert result["ok"] is False


async def test_download_file_ssrf_blocked(tmp_path: Any) -> None:
    target = str(tmp_path / "bad.bin")
    result = json.loads(
        await Http.download_file(url="http://192.168.1.1/admin", path=target)
    )
    assert result["ok"] is False
    assert "ssrf" in result.get("error", "").lower()


# ---------------------------------------------------------------------------
# Web.web_fetch
# ---------------------------------------------------------------------------


async def test_web_fetch_success() -> None:
    resp = _fake_response(body=b"plain text response")
    with (
        patch.object(_web_mod.Policy, "validate_url", side_effect=lambda u: u),
        patch.object(_web_mod.url_request, "urlopen", return_value=resp),
    ):
        result = json.loads(await Web.web_fetch(url="https://example.com"))

    assert result["ok"] is True
    assert "body" in result
    assert "plain text" in result["body"]


async def test_web_fetch_html_content_type_strips_tags() -> None:
    html_body = b"<html><body><p>Hello World</p></body></html>"
    resp = _fake_response(body=html_body, content_type="text/html")
    with (
        patch.object(_web_mod.Policy, "validate_url", side_effect=lambda u: u),
        patch.object(_web_mod.url_request, "urlopen", return_value=resp),
    ):
        result = json.loads(await Web.web_fetch(url="https://example.com/page"))

    assert result["ok"] is True
    assert "Hello World" in result["body"]


async def test_web_fetch_network_error() -> None:
    exc = url_error.URLError(reason="no route to host")
    with (
        patch.object(_web_mod.Policy, "validate_url", side_effect=lambda u: u),
        patch.object(_web_mod.url_request, "urlopen", side_effect=exc),
    ):
        result = json.loads(await Web.web_fetch(url="https://unreachable.example.com"))

    assert result["ok"] is False


async def test_web_fetch_returns_cached_result_on_second_call() -> None:
    resp = _fake_response(body=b"first response")
    with (
        patch.object(_web_mod.Policy, "validate_url", side_effect=lambda u: u),
        patch.object(_web_mod.url_request, "urlopen", return_value=resp) as mock_open,
    ):
        await Web.web_fetch(url="https://example.com/cached")
        result2 = json.loads(await Web.web_fetch(url="https://example.com/cached"))

    # Second call should use cache, not call urlopen again
    assert mock_open.call_count == 1
    assert result2.get("cached") is True


# ---------------------------------------------------------------------------
# Web.web_search
# ---------------------------------------------------------------------------


async def test_web_search_returns_results_key() -> None:
    # Fake minimal DuckDuckGo HTML; even if no results parsed, "results" key present
    html = (
        b'<a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com">Example</a>'
        b'<span class="result__snippet">An example result snippet.</span>'
    )
    resp = _fake_response(body=html, content_type="text/html")
    with patch.object(_web_mod.url_request, "urlopen", return_value=resp):
        result = json.loads(await Web.web_search(query="test query"))

    assert result["ok"] is True
    assert "results" in result
    assert result["query"] == "test query"


async def test_web_search_network_failure_returns_error() -> None:
    exc = url_error.URLError(reason="timeout")
    with patch.object(_web_mod.url_request, "urlopen", side_effect=exc):
        result = json.loads(await Web.web_search(query="anything"))

    assert result["ok"] is False
