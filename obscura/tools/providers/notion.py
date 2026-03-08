"""Notion API provider — search, get_page, query_database, healthcheck."""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.notion.com"
_VERSION = "2022-06-28"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ.get('NOTION_API_KEY', '')}",
        "Notion-Version": _VERSION,
        "Content-Type": "application/json",
    }


async def _handler_search(**kwargs: Any) -> dict[str, Any]:
    query = kwargs.get("query", "")
    try:
        async with httpx.AsyncClient(base_url=_BASE, headers=_headers(), timeout=15) as c:
            r = await c.post("/v1/search", json={"query": query})
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
    except Exception as e:
        return {"error": str(e)}


async def _handler_get_page(**kwargs: Any) -> dict[str, Any]:
    page_id = kwargs.get("page_id", "")
    if not page_id:
        return {"error": "page_id is required"}
    try:
        async with httpx.AsyncClient(base_url=_BASE, headers=_headers(), timeout=15) as c:
            r = await c.get(f"/v1/pages/{page_id}")
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
    except Exception as e:
        return {"error": str(e)}


async def _handler_query_database(**kwargs: Any) -> dict[str, Any]:
    db_id = kwargs.get("database_id", "")
    if not db_id:
        return {"error": "database_id is required"}
    body: dict[str, Any] = {}
    if kwargs.get("filter"):
        body["filter"] = kwargs["filter"]
    if kwargs.get("sorts"):
        body["sorts"] = kwargs["sorts"]
    try:
        async with httpx.AsyncClient(base_url=_BASE, headers=_headers(), timeout=15) as c:
            r = await c.post(f"/v1/databases/{db_id}/query", json=body)
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
    except Exception as e:
        return {"error": str(e)}


async def healthcheck() -> dict[str, Any]:
    if not os.environ.get("NOTION_API_KEY"):
        return {"status": "unhealthy", "error": "NOTION_API_KEY not set"}
    try:
        async with httpx.AsyncClient(base_url=_BASE, headers=_headers(), timeout=10) as c:
            r = await c.get("/v1/users/me")
            r.raise_for_status()
            return {"status": "healthy"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}
