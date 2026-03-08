"""Data.gov CKAN API provider — search federal open datasets."""
from __future__ import annotations

import re
from typing import Any

import httpx

CKAN_BASE = "https://catalog.data.gov/api/3/action/package_search"
HTML_SEARCH = "https://catalog.data.gov/dataset"
TIMEOUT = 15.0


async def search_datasets(
    q: str,
    rows: int = 10,
    start: int = 0,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        return await _ckan_search(q, rows, start)
    except Exception:
        pass

    try:
        return await _html_fallback(q, rows, start)
    except Exception as exc:
        return {"error": str(exc)}


async def _ckan_search(q: str, rows: int, start: int) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            CKAN_BASE,
            params={"q": q, "rows": rows, "start": start},
        )
        resp.raise_for_status()
        body = resp.json()

    if not body.get("success"):
        raise RuntimeError(body.get("error", "CKAN returned success=false"))

    result_block = body.get("result", {})
    datasets = [
        {
            "name": ds.get("name"),
            "title": ds.get("title"),
            "notes": (ds.get("notes") or "")[:300],
            "url": ds.get("url"),
            "organization": (ds.get("organization") or {}).get("title"),
            "formats": sorted(
                {r.get("format", "").upper() for r in ds.get("resources", []) if r.get("format")}
            ),
        }
        for ds in result_block.get("results", [])
    ]

    return {
        "results": datasets,
        "count": result_block.get("count", len(datasets)),
    }


async def _html_fallback(q: str, rows: int, start: int) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(
            HTML_SEARCH,
            params={"q": q, "page": (start // max(rows, 1)) + 1},
        )
        resp.raise_for_status()

    html = resp.text
    pattern = re.compile(
        r'<h3[^>]*class="dataset-heading"[^>]*>\s*<a[^>]*href="([^"]*)"[^>]*>([^<]+)</a>',
        re.IGNORECASE,
    )
    matches = pattern.findall(html)

    datasets = [
        {
            "name": href.rstrip("/").rsplit("/", 1)[-1],
            "title": title.strip(),
            "url": f"https://catalog.data.gov{href}" if href.startswith("/") else href,
        }
        for href, title in matches[:rows]
    ]

    return {
        "results": datasets,
        "count": len(datasets),
        "source": "html_fallback",
    }
