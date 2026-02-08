"""TUI API Client - Connects to Obscura server."""

from __future__ import annotations

import os
from typing import Any
import httpx


class TUIClient:
    """HTTP client for TUI to connect to Obscura API."""
    
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = base_url or os.environ.get("OBSCURA_URL", "http://localhost:8080")
        self.token = token or os.environ.get("OBSCURA_TOKEN", "")
        self._client: httpx.AsyncClient | None = None
    
    async def __aenter__(self):
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=30.0
        )
        return self
    
    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()
    
    # Health
    async def health(self) -> dict:
        """Check server health."""
        resp = await self._client.get("/health")
        resp.raise_for_status()
        return resp.json()
    
    # Agents
    async def list_agents(self) -> list[dict]:
        """List all agents."""
        resp = await self._client.get("/api/v1/agents")
        resp.raise_for_status()
        return resp.json().get("agents", [])
    
    async def get_agent(self, agent_id: str) -> dict:
        """Get agent by ID."""
        resp = await self._client.get(f"/api/v1/agents/{agent_id}")
        resp.raise_for_status()
        return resp.json()
    
    async def spawn_agent(self, name: str, model: str = "claude", **kwargs) -> dict:
        """Create a new agent."""
        data = {"name": name, "model": model, **kwargs}
        resp = await self._client.post("/api/v1/agents", json=data)
        resp.raise_for_status()
        return resp.json()
    
    async def stop_agent(self, agent_id: str) -> None:
        """Stop an agent."""
        resp = await self._client.delete(f"/api/v1/agents/{agent_id}")
        resp.raise_for_status()
    
    async def run_task(self, agent_id: str, prompt: str, context: dict | None = None) -> dict:
        """Run a task on an agent."""
        data = {"prompt": prompt}
        if context:
            data["context"] = context
        resp = await self._client.post(f"/api/v1/agents/{agent_id}/run", json=data)
        resp.raise_for_status()
        return resp.json()
    
    # Memory
    async def list_memory_keys(self) -> list[str]:
        """List all memory keys."""
        resp = await self._client.get("/api/v1/memory")
        resp.raise_for_status()
        return resp.json().get("keys", [])
    
    async def get_memory(self, namespace: str, key: str) -> Any:
        """Get memory value."""
        resp = await self._client.get(f"/api/v1/memory/{namespace}/{key}")
        resp.raise_for_status()
        return resp.json().get("value")
    
    async def set_memory(self, namespace: str, key: str, value: Any) -> None:
        """Set memory value."""
        resp = await self._client.post(
            f"/api/v1/memory/{namespace}/{key}",
            json={"value": value}
        )
        resp.raise_for_status()
    
    async def delete_memory(self, namespace: str, key: str) -> None:
        """Delete memory value."""
        resp = await self._client.delete(f"/api/v1/memory/{namespace}/{key}")
        resp.raise_for_status()
    
    # Stats
    async def get_stats(self) -> dict:
        """Get system stats."""
        agents = await self.list_agents()
        keys = await self.list_memory_keys()
        
        return {
            "active": len(agents),
            "running": sum(1 for a in agents if a.get("status") == "RUNNING"),
            "waiting": sum(1 for a in agents if a.get("status") == "WAITING"),
            "memory": len(keys),
        }
