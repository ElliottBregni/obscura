"""Async HTTP client for the Obscura FastAPI server."""

from __future__ import annotations

from typing import Any

import httpx

from obscura.mcp_server.config import ObscuraMCPServerConfig


class ObscuraAPIClient:
    """Thin httpx wrapper for calling the Obscura FastAPI server."""

    def __init__(self, config: ObscuraMCPServerConfig) -> None:
        self._config = config
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            headers["X-API-Key"] = config.api_key
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            timeout=config.timeout,
        )

    async def get(self, path: str, **params: Any) -> Any:
        """Send a GET request and return parsed JSON."""
        resp = await self._client.get(path, params=params or None)
        resp.raise_for_status()
        return resp.json()

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        """Send a POST request and return parsed JSON."""
        resp = await self._client.post(path, json=json or {})
        resp.raise_for_status()
        return resp.json()

    async def put(self, path: str, json: dict[str, Any] | None = None) -> Any:
        """Send a PUT request and return parsed JSON."""
        resp = await self._client.put(path, json=json or {})
        resp.raise_for_status()
        return resp.json()

    async def delete(self, path: str, **params: Any) -> Any:
        """Send a DELETE request and return parsed JSON."""
        resp = await self._client.delete(path, params=params or None)
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
