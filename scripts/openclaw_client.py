"""
obscura_client.py — OpenClaw integration client for Obscura.

Place this file in your OpenClaw workspace (e.g., ~/.openclaw/workspace/)
to enable seamless Obscura integration.

Usage in OpenClaw:
    from obscura_client import get_obscura

    obscura = await get_obscura()
    agent = await obscura.spawn_agent("reviewer", "claude")
    result = await obscura.run_agent(agent["agent_id"], "Review this code")
"""

from __future__ import annotations

import os
import json
from datetime import datetime
from typing import Any, AsyncIterator, cast
from contextlib import asynccontextmanager

import httpx

# Configuration
OBSCURA_BASE = os.environ.get("OBSCURA_URL", "http://localhost:8080")
OBSCURA_WS = os.environ.get("OBSCURA_WS_URL", "ws://localhost:8080")
OBSCURA_TOKEN = os.environ.get("OBSCURA_TOKEN", "local-dev-token")


class ObscuraClient:
    """
    Client for interacting with Obscura from OpenClaw.

    Provides methods to:
    - Spawn and manage agents
    - Store/retrieve memory
    - Semantic search
    - Stream agent output
    """

    def __init__(self, token: str | None = None, base_url: str | None = None):
        self.token = token or OBSCURA_TOKEN
        self.base_url = base_url or OBSCURA_BASE
        self.ws_url = OBSCURA_WS
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=300.0,
            )
        return self._client

    # ====================================================================
    # Agent Management
    # ====================================================================

    async def spawn_agent(
        self,
        name: str,
        model: str = "claude",
        system_prompt: str = "",
        memory_namespace: str = "openclaw",
        max_iterations: int = 10,
    ) -> dict[str, Any]:
        """
        Spawn a new agent.

        Args:
            name: Human-readable name for the agent
            model: "claude" or "copilot"
            system_prompt: System instructions for the agent
            memory_namespace: Where agent stores its memory
            max_iterations: Safety limit for agent loops

        Returns:
            Agent info including agent_id
        """
        client = await self._get_client()
        resp = await client.post(
            "/api/v1/agents",
            json={
                "name": name,
                "model": model,
                "system_prompt": system_prompt,
                "memory_namespace": memory_namespace,
                "max_iterations": max_iterations,
            },
        )
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

    async def run_agent(
        self, agent_id: str, prompt: str, **context: Any
    ) -> dict[str, Any]:
        """
        Run a task on an existing agent.

        Args:
            agent_id: The agent's unique ID
            prompt: The task to execute
            **context: Additional context for the task

        Returns:
            Result with status and output
        """
        client = await self._get_client()
        resp = await client.post(
            f"/api/v1/agents/{agent_id}/run",
            json={"prompt": prompt, "context": context},
        )
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

    async def stream_agent(
        self, agent_id: str, prompt: str, **context: Any
    ) -> AsyncIterator[str]:
        """
        Stream agent output in real-time.

        Yields chunks of text as they're generated.
        """
        client = await self._get_client()
        async with client.stream(
            "POST",
            f"/api/v1/agents/{agent_id}/stream",
            json={"prompt": prompt, "context": context},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    if "text" in data:
                        yield data["text"]

    async def get_agent_status(self, agent_id: str) -> dict[str, Any]:
        """Get current status of an agent."""
        client = await self._get_client()
        resp = await client.get(f"/api/v1/agents/{agent_id}")
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

    async def list_agents(self, status: str | None = None) -> list[dict[str, Any]]:
        """List all agents, optionally filtered by status."""
        client = await self._get_client()
        params: dict[str, str] = {}
        if status:
            params["status"] = status
        resp = await client.get("/api/v1/agents", params=params)
        resp.raise_for_status()
        data = cast(dict[str, Any], resp.json())
        return cast(list[dict[str, Any]], data.get("agents", []))

    async def stop_agent(self, agent_id: str) -> dict[str, Any]:
        """Stop and cleanup an agent."""
        client = await self._get_client()
        resp = await client.delete(f"/api/v1/agents/{agent_id}")
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

    # ====================================================================
    # Memory Operations
    # ====================================================================

    async def store_memory(
        self,
        key: str,
        value: Any,
        namespace: str = "openclaw",
        ttl: int | None = None,
    ) -> None:
        """
        Store a value in shared memory.

        Args:
            key: Unique key for this value
            value: Any JSON-serializable value
            namespace: Logical grouping (e.g., "session", "user")
            ttl: Time-to-live in seconds
        """
        client = await self._get_client()
        body: dict[str, Any] = {"value": value}
        params: dict[str, int] = {}
        if ttl:
            params["ttl"] = ttl
        await client.post(
            f"/api/v1/memory/{namespace}/{key}",
            json=body,
            params=params,
        )

    async def get_memory(self, key: str, namespace: str = "openclaw") -> Any | None:
        """Get a value from shared memory. Returns None if not found."""
        client = await self._get_client()
        resp = await client.get(f"/api/v1/memory/{namespace}/{key}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = cast(dict[str, Any], resp.json())
        return data.get("value")

    async def delete_memory(self, key: str, namespace: str = "openclaw") -> bool:
        """Delete a value from memory. Returns True if existed."""
        client = await self._get_client()
        resp = await client.delete(f"/api/v1/memory/{namespace}/{key}")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    async def list_memory_keys(
        self, namespace: str | None = None
    ) -> list[dict[str, str]]:
        """List all memory keys."""
        client = await self._get_client()
        params: dict[str, str] = {}
        if namespace:
            params["namespace"] = namespace
        resp = await client.get("/api/v1/memory", params=params)
        resp.raise_for_status()
        data = cast(dict[str, Any], resp.json())
        return cast(list[dict[str, str]], data.get("keys", []))

    async def search_memory(self, query: str) -> list[dict[str, Any]]:
        """Search memory keys and values."""
        client = await self._get_client()
        resp = await client.get("/api/v1/memory/search", params={"q": query})
        resp.raise_for_status()
        data = cast(dict[str, Any], resp.json())
        return cast(list[dict[str, Any]], data.get("results", []))

    async def get_memory_stats(self) -> dict[str, Any]:
        """Get memory usage statistics."""
        client = await self._get_client()
        resp = await client.get("/api/v1/memory/stats")
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

    # ====================================================================
    # Vector / Semantic Memory
    # ====================================================================

    async def remember(
        self,
        text: str,
        key: str | None = None,
        metadata: dict[str, Any] | None = None,
        namespace: str = "semantic",
    ) -> str:
        """
        Store text with semantic embedding for later recall.

        Args:
            text: The text to remember
            key: Optional key (auto-generated if not provided)
            metadata: Additional metadata
            namespace: Storage namespace

        Returns:
            The key used to store this memory
        """
        if key is None:
            key = f"mem_{datetime.now().timestamp()}"

        client = await self._get_client()
        await client.post(
            f"/api/v1/vector-memory/{namespace}/{key}",
            json={"text": text, "metadata": metadata or {}},
        )
        return key

    async def recall(
        self,
        query: str,
        top_k: int = 3,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Recall semantically similar memories.

        Args:
            query: What to search for
            top_k: Number of results
            namespace: Filter by namespace

        Returns:
            List of memories with similarity scores
        """
        client = await self._get_client()
        params: dict[str, str | int] = {"q": query, "top_k": top_k}
        if namespace:
            params["namespace"] = namespace
        resp = await client.get("/api/v1/vector-memory/search", params=params)
        resp.raise_for_status()
        data = cast(dict[str, Any], resp.json())
        return cast(list[dict[str, Any]], data.get("results", []))

    # ====================================================================
    # High-Level Workflows
    # ====================================================================

    async def quick_agent(
        self,
        name: str,
        prompt: str,
        model: str = "claude",
        system_prompt: str = "",
    ) -> str:
        """
        Convenience: Spawn, run, stop, and return result.

        For one-off tasks where you don't need to keep the agent.
        """
        agent = await self.spawn_agent(name, model, system_prompt)
        agent_id = str(agent["agent_id"])

        try:
            result = await self.run_agent(agent_id, prompt)
            return str(result.get("result", ""))
        finally:
            await self.stop_agent(agent_id)

    async def multi_agent_workflow(
        self,
        tasks: list[tuple[str, str, str]],  # (name, system_prompt, prompt)
        model: str = "claude",
    ) -> list[dict[str, Any]]:
        """
        Run multiple agents in parallel and collect results.

        Args:
            tasks: List of (name, system_prompt, prompt) tuples
            model: Which model to use for all agents

        Returns:
            List of results in same order as tasks
        """
        import asyncio

        # Spawn all agents
        agents: list[dict[str, Any]] = []
        for name, sys_prompt, _ in tasks:
            agent = await self.spawn_agent(name, model, sys_prompt)
            agents.append(agent)

        # Run all tasks in parallel
        async def run_task(agent: dict[str, Any], prompt: str) -> dict[str, Any]:
            try:
                result = await self.run_agent(str(agent["agent_id"]), prompt)
                return {"agent": agent, "result": result, "error": None}
            except Exception as e:
                return {"agent": agent, "result": None, "error": str(e)}

        results: list[dict[str, Any]] = list(
            await asyncio.gather(
                *[
                    run_task(agent, prompt)
                    for agent, (_, _, prompt) in zip(agents, tasks)
                ]
            )
        )

        # Cleanup
        for agent in agents:
            await self.stop_agent(str(agent["agent_id"]))

        return results

    # ====================================================================
    # Context Manager
    # ====================================================================

    @asynccontextmanager
    async def session(self):
        """Async context manager for automatic cleanup."""
        try:
            yield self
        finally:
            await self.close()

    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def __del__(self):
        """Cleanup on garbage collection."""
        if self._client and not self._client.is_closed:
            import asyncio

            try:
                asyncio.get_event_loop().create_task(self._client.aclose())
            except Exception:
                pass


# Singleton instance
_obscura_client: ObscuraClient | None = None


async def get_obscura() -> ObscuraClient:
    """
    Get or create the global Obscura client.

    Usage:
        obscura = await get_obscura()
        result = await obscura.quick_agent("reviewer", "Review this code")
    """
    global _obscura_client
    if _obscura_client is None:
        _obscura_client = ObscuraClient()
    return _obscura_client


def reset_obscura():
    """Reset the global client (useful for testing)."""
    global _obscura_client
    _obscura_client = None


# ====================================================================
# Convenience functions for direct use
# ====================================================================


async def spawn(name: str, model: str = "claude", **kwargs: Any) -> dict[str, Any]:
    """Spawn an agent (convenience function)."""
    o = await get_obscura()
    return await o.spawn_agent(name, model, **kwargs)


async def run(agent_id: str, prompt: str, **context: Any) -> dict[str, Any]:
    """Run a task (convenience function)."""
    o = await get_obscura()
    return await o.run_agent(agent_id, prompt, **context)


async def remember(text: str, **kwargs: Any) -> str:
    """Store semantic memory (convenience function)."""
    o = await get_obscura()
    return await o.remember(text, **kwargs)


async def recall(query: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Recall semantic memory (convenience function)."""
    o = await get_obscura()
    return await o.recall(query, **kwargs)


async def quick(name: str, prompt: str, **kwargs: Any) -> str:
    """Quick one-off agent (convenience function)."""
    o = await get_obscura()
    return await o.quick_agent(name, prompt, **kwargs)
