"""Public APIs provider — discover, wrap, and persist public API tools."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.publicapis.org"
_PERSIST_PATH = Path(os.environ.get("OBSCURA_DATA_DIR", os.path.expanduser("~/.obscura"))) / "public_apis_tools.json"


async def _discover(params: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=_BASE, timeout=15) as c:
            r = await c.get("/entries", params=params)
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
    except Exception as e:
        return {"error": str(e)}


def _load_persisted() -> list[dict[str, Any]]:
    if _PERSIST_PATH.exists():
        return json.loads(_PERSIST_PATH.read_text())  # type: ignore[no-any-return]
    return []


def _save_persisted(tools: list[dict[str, Any]]) -> None:
    _PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PERSIST_PATH.write_text(json.dumps(tools, indent=2))


async def PublicAPIsProvider(**kwargs: Any) -> dict[str, Any]:
    action = kwargs.get("_action") or kwargs.get("action") or ""

    if action == "list_persisted" or kwargs.get("list_persisted"):
        return {"tools": _load_persisted()}

    if action == "remove_persisted" or kwargs.get("tool_name_to_remove"):
        name = kwargs.get("tool_name") or kwargs.get("tool_name_to_remove", "")
        tools = [t for t in _load_persisted() if t.get("name") != name]
        _save_persisted(tools)
        return {"removed": name, "remaining": len(tools)}

    if action == "create_tool" or kwargs.get("api_name"):
        tool_def: dict[str, Any] = {
            "name": kwargs.get("tool_name") or kwargs.get("api_name", ""),
            "api_name": kwargs.get("api_name", ""),
            "method": kwargs.get("method", "GET"),
            "path": kwargs.get("path", "/"),
            "headers": kwargs.get("headers", {}),
            "query_params": kwargs.get("query_params", {}),
        }
        if kwargs.get("persist", True):
            tools = _load_persisted()
            tools.append(tool_def)
            _save_persisted(tools)
        return {"created": tool_def}

    if kwargs.get("name_or_link"):
        return await _discover({"title": kwargs["name_or_link"]})

    # Default: discover
    params: dict[str, Any] = {}
    if kwargs.get("category"):
        params["category"] = kwargs["category"]
    if kwargs.get("https") is not None:
        params["https"] = str(kwargs["https"]).lower()
    if kwargs.get("auth"):
        params["auth"] = kwargs["auth"]
    if kwargs.get("description_contains"):
        params["title"] = kwargs["description_contains"]
    return await _discover(params)
