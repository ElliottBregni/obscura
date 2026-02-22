"""Async client placeholder for TUI interactions.

Notes:
- The original implementation is pending; this shim exists so imports used in
  tests and docs resolve without errors.
"""

from __future__ import annotations

from typing import Any, AsyncIterator


class TUIClient:
    """Stub async client; replace with real implementation when available."""

    async def __aenter__(self) -> "TUIClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:  # pragma: no cover - placeholder
        return None

    # API surface mirrors the expected methods in tests (health, agents, memory)
    async def health(self) -> dict[str, str]:
        return {"status": "not-implemented"}

    async def list_agents(self) -> list[dict[str, Any]]:
        return []

    async def get_stats(self) -> dict[str, int]:
        return {"active": 0, "running": 0, "waiting": 0, "memory": 0}

    async def spawn_agent(self, name: str, model: str) -> dict[str, Any]:
        return {"name": name, "agent_id": "stub"}

    async def stop_agent(self, agent_id: str) -> None:
        return None

    async def set_memory(self, namespace: str, key: str, value: Any) -> None:
        return None

    async def get_memory(self, namespace: str, key: str) -> Any:
        return None

    async def delete_memory(self, namespace: str, key: str) -> None:
        return None

    # Stream placeholder to match potential future API
    async def stream(self, prompt: str) -> AsyncIterator[str]:
        if False:  # pragma: no cover
            yield prompt
