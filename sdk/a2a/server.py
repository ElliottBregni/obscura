"""
sdk.a2a.server — ObscuraA2AServer: lifecycle and component wiring.

Holds references to the A2AService, TaskStore, and transport routers.
Used by the main ``create_app()`` to register A2A endpoints.
"""

from __future__ import annotations

import logging
from typing import Any

from sdk.a2a.agent_card import AgentCardGenerator
from sdk.a2a.service import A2AService
from sdk.a2a.store import InMemoryTaskStore, TaskStore
from sdk.a2a.types import AgentCard

logger = logging.getLogger(__name__)


class ObscuraA2AServer:
    """High-level A2A server lifecycle manager.

    Parameters
    ----------
    store:
        Task persistence backend. Defaults to InMemoryTaskStore.
    agent_card:
        Pre-built agent card. If not provided, one is generated from kwargs.
    get_runtime:
        Factory returning per-user AgentRuntime (from ``sdk.deps``).
    agent_model:
        Default model for spawned agents.
    agent_system_prompt:
        Default system prompt for spawned agents.
    """

    def __init__(
        self,
        *,
        store: TaskStore | None = None,
        agent_card: AgentCard | None = None,
        get_runtime: Any = None,
        agent_model: str = "copilot",
        agent_system_prompt: str = "",
        name: str = "Obscura Agent",
        url: str = "http://localhost:8080",
        description: str = "",
    ) -> None:
        self._store: TaskStore = store or InMemoryTaskStore()
        self._agent_card = agent_card or AgentCardGenerator(
            name=name,
            url=url,
            description=description,
        ).with_bearer_auth().with_provider("Obscura", "https://obscura.dev").build()

        self._service = A2AService(
            store=self._store,
            agent_card=self._agent_card,
            get_runtime=get_runtime,
            agent_model=agent_model,
            agent_system_prompt=agent_system_prompt,
        )

    @property
    def service(self) -> A2AService:
        return self._service

    @property
    def store(self) -> TaskStore:
        return self._store

    @property
    def agent_card(self) -> AgentCard:
        return self._agent_card

    async def startup(self) -> None:
        """Connect resources (e.g., Redis)."""
        from sdk.a2a.store import RedisTaskStore

        if isinstance(self._store, RedisTaskStore):
            await self._store.connect()
        logger.info("A2A server started")

    async def shutdown(self) -> None:
        """Disconnect resources."""
        from sdk.a2a.store import RedisTaskStore

        if isinstance(self._store, RedisTaskStore):
            await self._store.disconnect()
        logger.info("A2A server stopped")
