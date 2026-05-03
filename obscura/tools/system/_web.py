"""Web fetch, search, and HTML utility tools."""

from __future__ import annotations

import html as _html
import json
import re
import time as _time
from typing import Any, ClassVar, cast
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request

from obscura.core.context_window import (
    MAX_WEB_FETCH_TOKENS,
    truncate_to_token_budget,
)
from obscura.core.tools import tool
from obscura.tools.system._policy import Policy
import logging

logger = logging.getLogger(__name__)


class Web:
    """Web fetch / search tool namespace."""

    WEB_FETCH_CACHE_TTL: ClassVar[float] = 900.0  # 15 minutes
    _cache: ClassVar[dict[tuple[str, str], tuple[float, str]]] = {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def strip_html(raw: str) -> str:
        """Strip HTML tags and decode entities, returning plain text."""
        # Drop script/style blocks entirely
        text = re.sub(
            r"<(script|style)[^>]*>.*?</\1>",
            "",
            raw,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Replace block-level tags with newlines for readability
        text = re.sub(
            r"</(p|div|li|tr|h[1-6]|br)[^>]*>", "\n", text, flags=re.IGNORECASE
        )
        # Strip remaining tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Decode HTML entities (&amp; &lt; etc.)
        text = _html.unescape(text)
        # Collapse whitespace while preserving paragraph breaks
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def html_to_markdown(html_text: str) -> str:
        """Convert HTML to Markdown using markdownify if available, else strip tags."""
        try:
            import markdownify  # pyright: ignore[reportMissingImports]

            converter = cast("Any", markdownify).markdownify
            return cast(
                "str",
                converter(
                    html_text,
                    heading_style="ATX",
                    strip=["img", "script", "style"],
                ),
            )
        except ImportError:
            logger.debug("suppressed exception in html_to_markdown", exc_info=True)
            return Web.strip_html(html_text)

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @staticmethod
    @tool(
        "web_fetch",
        (
            "Fetch a URL and return the page content. "
            "HTML is automatically converted to Markdown (or plain text as fallback). "
            "Provide a `prompt` describing what to extract. "
            "Results are cached for 15 minutes."
        ),
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "prompt": {
                    "type": "string",
                    "description": "What to extract or summarize from the page.",
                },
                "method": {"type": "string"},
                "headers": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "body": {"type": "string"},
                "timeout_seconds": {"type": "number"},
                "max_bytes": {"type": "integer"},
            },
            "required": ["url"],
        },
    )
    async def web_fetch(
        url: str,
        prompt: str = "",
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str = "",
        timeout_seconds: float = 20.0,
        max_bytes: int = 200_000,
    ) -> str:
        # Check cache for GET requests.
        cache_key = (url, prompt)
        if method.upper() == "GET" and cache_key in Web._cache:
            cached_ts, cached_result = Web._cache[cache_key]
            if _time.time() - cached_ts < Web.WEB_FETCH_CACHE_TTL:
                result = json.loads(cached_result)
                result["cached"] = True
                return json.dumps(result)

        timeout_seconds = float(timeout_seconds)
        max_bytes = int(max_bytes)
        request_headers = headers or {}
        payload = body.encode("utf-8") if body else None
        try:
            url = Policy.validate_url(url)
        except ValueError as exc:
            logger.debug("suppressed exception in web_fetch", exc_info=True)
            return Policy.json_error("ssrf_blocked", url=url, detail=str(exc))
        req = url_request.Request(
            url=url,
            method=method.upper(),
            headers=request_headers,
            data=payload,
        )
        try:
            with url_request.urlopen(req, timeout=timeout_seconds) as response:
                raw = response.read(max_bytes + 1)
                truncated = len(raw) > max_bytes
                data = raw[:max_bytes]
                text = data.decode("utf-8", errors="replace")
                response_headers = dict(response.headers.items())
                content_type = response_headers.get("Content-Type", "").lower()
                is_html = "html" in content_type or text.lstrip().startswith("<")

                # Convert HTML to Markdown if available, else strip tags.
                body_text = Web.html_to_markdown(text) if is_html else text

                # Token budget truncation.
                body_text, token_truncated = truncate_to_token_budget(
                    body_text,
                    MAX_WEB_FETCH_TOKENS,
                )
                truncated = truncated or token_truncated

                # Redirect detection.
                final_url: str = str(response.geturl())
                redirect_info: dict[str, object] = {}
                if final_url != url:
                    from urllib.parse import urlparse

                    orig_host = urlparse(url).hostname
                    final_host = urlparse(final_url).hostname
                    if orig_host != final_host:
                        redirect_info = {
                            "redirected": True,
                            "original_host": orig_host,
                            "final_host": final_host,
                            "warning": "Redirected to a different domain",
                        }

                result: dict[str, object] = {
                    "ok": True,
                    "url": url,
                    "final_url": final_url,
                    "status": getattr(response, "status", 200),
                    "content_type": content_type,
                    "body": body_text,
                    "truncated": truncated,
                    "bytes_read": len(data),
                }
                if prompt:
                    result["prompt"] = prompt
                if redirect_info:
                    result["redirect"] = redirect_info

                result_json = json.dumps(result)
                # Cache GET responses.
                if method.upper() == "GET":
                    Web._cache[cache_key] = (_time.time(), result_json)
                return result_json
        except url_error.HTTPError as exc:
            logger.debug("suppressed exception in web_fetch", exc_info=True)
            raw_error = exc.read(max_bytes)
            return json.dumps(
                {
                    "ok": False,
                    "url": url,
                    "status": exc.code,
                    "error": "http_error",
                    "body": raw_error.decode("utf-8", errors="replace"),
                },
            )
        except Exception as exc:
            logger.debug("suppressed exception in web_fetch", exc_info=True)
            return Policy.json_error("web_fetch_failed", url=url, detail=str(exc))

    @staticmethod
    @tool(
        "web_search",
        (
            "Search the web for a query and return concise result items. "
            "Optionally filter by allowed_domains or blocked_domains."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
                "allowed_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only include results from these domains.",
                },
                "blocked_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exclude results from these domains.",
                },
            },
            "required": ["query"],
        },
    )
    async def web_search(
        query: str,
        max_results: int = 5,
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
    ) -> str:
        """Search the web via DuckDuckGo HTML scraping (no API key required)."""
        limit = max(1, min(int(max_results), 20))
        encoded = url_parse.quote_plus(query)
        endpoint = f"https://html.duckduckgo.com/html/?q={encoded}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Referer": "https://duckduckgo.com/",
        }

        # Fetch raw HTML directly (web_fetch strips tags, we need structure)
        try:
            req = url_request.Request(endpoint, headers=headers)
            with url_request.urlopen(req, timeout=20) as resp:
                raw_html = resp.read(500_000).decode("utf-8", errors="replace")
        except Exception as exc:
            logger.debug("suppressed exception in web_search", exc_info=True)
            return Policy.json_error("web_search_fetch_failed", detail=str(exc))

        def clean(s: str) -> str:
            return re.sub(r"<[^>]+>", "", _html.unescape(s)).strip()

        titles = [
            clean(t) for t in re.findall(r'class="result__a"[^>]*>(.*?)</a>', raw_html)
        ]
        snippets = [
            clean(s)
            for s in re.findall(
                r'class="result__snippet"[^>]*>(.*?)</span>',
                raw_html,
                re.DOTALL,
            )
        ]
        hrefs = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', raw_html)
        urls_fb = [
            clean(u)
            for u in re.findall(
                r'class="result__url"[^>]*>\s*(.*?)\s*</a>',
                raw_html,
                re.DOTALL,
            )
        ]

        items: list[dict[str, str]] = []
        for i, title in enumerate(titles):
            if len(items) >= limit:
                break
            if not title:
                continue
            href = hrefs[i] if i < len(hrefs) else ""
            # DDG wraps hrefs in a redirect — extract uddg param if present
            if "uddg=" in href:
                uddg_match = re.search(r"uddg=([^&]+)", href)
                href = (
                    url_parse.unquote_plus(uddg_match.group(1)) if uddg_match else href
                )
            url = href or (urls_fb[i] if i < len(urls_fb) else "")
            snippet = snippets[i] if i < len(snippets) else ""

            # Domain filtering.
            if url and (allowed_domains or blocked_domains):
                try:
                    from urllib.parse import urlparse as _urlparse

                    domain = (_urlparse(url).hostname or "").lower()
                except Exception:
                    logger.debug("suppressed exception in web_search", exc_info=True)
                    domain = ""
                if allowed_domains and not any(
                    domain.endswith(d.lower()) for d in allowed_domains
                ):
                    continue
                if blocked_domains and any(
                    domain.endswith(d.lower()) for d in blocked_domains
                ):
                    continue

            items.append({"title": title, "url": url, "snippet": snippet})

        return json.dumps(
            {"ok": True, "query": query, "count": len(items), "results": items},
        )
