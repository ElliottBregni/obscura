"""obscura.a2a.server — ObscuraA2AServer: lifecycle and component wiring.

Holds references to the A2AService, TaskStore, and transport routers.
Used by the main ``create_app()`` to register A2A endpoints.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from obscura.integrations.a2a.agent_card import AgentCardGenerator
from obscura.integrations.a2a.service import A2AService
from obscura.integrations.a2a.store import InMemoryTaskStore, RedisTaskStore, TaskStore
from obscura.integrations.a2a.transports.unix_socket import (
    start_unix_socket_server,
    stop_unix_socket_server,
)

if TYPE_CHECKING:
    import asyncio

    from obscura.integrations.a2a.types import AgentCard

logger = logging.getLogger(__name__)


class ObscuraA2AServer:
    """High-level A2A server lifecycle manager.

    Parameters
    ----------
    store:
        Task persistence backend. Defaults to InMemoryTaskStore.
    agent_card:
        Pre-built agent card. If not provided, one is generated from kwargs.
    agent_backend:
        Provider backend identifier (``"copilot"``, ``"claude"``, …) used
        for every A2A task. Threaded into the ``SessionConfig`` that the
        composition layer builds per-task.
    agent_model:
        Default model for spawned agents.
    agent_system_prompt:
        Default system prompt for spawned agents.
    unix_socket_path:
        Optional path for a Unix domain socket transport. If set, a socket
        server is started alongside HTTP transports.

    """

    def __init__(
        self,
        *,
        store: TaskStore | None = None,
        agent_card: AgentCard | None = None,
        agent_backend: str = "copilot",
        agent_model: str = "copilot",
        agent_system_prompt: str = "",
        name: str = "Obscura Agent",
        url: str = "http://localhost:8080",
        description: str = "",
        unix_socket_path: str | None = None,
    ) -> None:
        self._store: TaskStore = store or InMemoryTaskStore()
        self._agent_card = (
            agent_card
            or AgentCardGenerator(
                name=name,
                url=url,
                description=description,
            )
            .with_bearer_auth()
            .with_provider("Obscura", "https://obscura.dev")
            .build()
        )

        self._service = A2AService(
            store=self._store,
            agent_card=self._agent_card,
            agent_backend=agent_backend,
            agent_model=agent_model,
            agent_system_prompt=agent_system_prompt,
        )

        self._unix_socket_path = unix_socket_path
        self._unix_socket_server: asyncio.Server | None = None

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
        """Connect resources (e.g., Redis, Unix socket)."""
        if isinstance(self._store, RedisTaskStore):
            await self._store.connect()

        if self._unix_socket_path:
            await self.start_unix_socket()

        logger.info("A2A server started")

    async def shutdown(self) -> None:
        """Disconnect resources."""
        if self._unix_socket_server is not None:
            await self.stop_unix_socket()

        if isinstance(self._store, RedisTaskStore):
            await self._store.disconnect()
        logger.info("A2A server stopped")

    async def start_unix_socket(
        self,
        socket_path: str | None = None,
    ) -> asyncio.Server:
        """Start a Unix domain socket transport for the A2A service.

        Parameters
        ----------
        socket_path:
            Override the socket path set in ``__init__``.

        Returns
        -------
        asyncio.Server
            The running Unix socket server.

        """
        path = socket_path or self._unix_socket_path or "/tmp/obscura-a2a.sock"
        self._unix_socket_path = path
        self._unix_socket_server = await start_unix_socket_server(
            self._service,
            path,
        )
        return self._unix_socket_server

    async def stop_unix_socket(self) -> None:
        """Stop the Unix domain socket transport."""
        if self._unix_socket_server is None:
            return
        await stop_unix_socket_server(
            self._unix_socket_server,
            self._unix_socket_path or "/tmp/obscura-a2a.sock",
        )
        self._unix_socket_server = None
