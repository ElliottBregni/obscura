"""
sdk.a2a.client — A2A client for invoking remote A2A agents.

Allows Obscura agents to discover, invoke, and monitor remote A2A agents
using standard protocol bindings (JSON-RPC, REST, SSE).

Usage::

    client = A2AClient("https://remote-agent.example.com")
    await client.discover()  # Fetch agent card

    # Blocking send
    task = await client.send_message("Analyze this data")

    # Streaming
    async for event in client.stream_message("Process in real-time"):
        print(event)
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator

import httpx

from sdk.a2a.types import (
    A2AMessage,
    AgentCard,
    StreamEvent,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)

logger = logging.getLogger(__name__)


class A2AClient:
    """Client for invoking a remote A2A agent.

    Parameters
    ----------
    base_url:
        Base URL of the remote A2A server (e.g. ``https://agent.example.com``).
    auth_token:
        Optional bearer token for authentication.
    timeout:
        HTTP request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth_token: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self._agent_card: AgentCard | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def agent_card(self) -> AgentCard | None:
        return self._agent_card

    async def connect(self) -> None:
        """Create the HTTP client."""
        headers: dict[str, str] = {"A2A-Version": "0.3"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=self._timeout,
        )

    async def disconnect(self) -> None:
        """Close the HTTP client."""
        if self._http:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> A2AClient:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()

    def _ensure_connected(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("Client not connected. Call connect() or use async context manager.")
        return self._http

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover(self) -> AgentCard:
        """Fetch the remote agent's Agent Card."""
        http = self._ensure_connected()
        resp = await http.get("/.well-known/agent.json")
        resp.raise_for_status()
        self._agent_card = AgentCard.model_validate(resp.json())
        return self._agent_card

    # ------------------------------------------------------------------
    # JSON-RPC helpers
    # ------------------------------------------------------------------

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a JSON-RPC 2.0 request and return the result."""
        http = self._ensure_connected()
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4().hex[:8]),
            "method": method,
            "params": params or {},
        }
        resp = await http.post("/a2a/rpc", json=payload)
        resp.raise_for_status()
        body = resp.json()

        if "error" in body:
            error = body["error"]
            from sdk.a2a.types import A2AError
            raise A2AError(error["code"], error["message"], error.get("data"))

        return body.get("result", {})

    # ------------------------------------------------------------------
    # message/send
    # ------------------------------------------------------------------

    async def send_message(
        self,
        text: str,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        blocking: bool = True,
    ) -> Task:
        """Send a message and return the task result.

        Parameters
        ----------
        text:
            Message text to send.
        context_id:
            Optional context for multi-turn conversations.
        task_id:
            Resume an existing task (for INPUT_REQUIRED flows).
        blocking:
            If True, waits for task completion.
        """
        message = A2AMessage(
            role="user",
            messageId=f"msg-{uuid.uuid4().hex[:8]}",
            parts=[TextPart(text=text)],
        )

        params: dict[str, Any] = {
            "message": message.model_dump(mode="json"),
            "configuration": {"blocking": blocking},
        }
        if context_id:
            params["contextId"] = context_id
        if task_id:
            params["taskId"] = task_id

        result = await self._rpc("message/send", params)
        return Task.model_validate(result)

    # ------------------------------------------------------------------
    # message/stream
    # ------------------------------------------------------------------

    async def stream_message(
        self,
        text: str,
        *,
        context_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a message and yield A2A events.

        Uses the SSE endpoint (POST /a2a/v1/tasks/streaming).
        """
        http = self._ensure_connected()

        message = A2AMessage(
            role="user",
            messageId=f"msg-{uuid.uuid4().hex[:8]}",
            parts=[TextPart(text=text)],
        )

        body: dict[str, Any] = {
            "message": message.model_dump(mode="json"),
        }
        if context_id:
            body["contextId"] = context_id

        async with http.stream("POST", "/a2a/v1/tasks/streaming", json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        data = json.loads(data_str)
                        kind = data.get("kind", "")
                        if kind == "status-update":
                            yield TaskStatusUpdateEvent.model_validate(data)
                        elif kind == "artifact-update":
                            yield TaskArtifactUpdateEvent.model_validate(data)
                    except (json.JSONDecodeError, Exception) as e:
                        logger.debug("Skipping unparseable SSE data: %s", e)

    # ------------------------------------------------------------------
    # tasks/get
    # ------------------------------------------------------------------

    async def get_task(self, task_id: str) -> Task:
        """Get a task by ID from the remote server."""
        result = await self._rpc("tasks/get", {"taskId": task_id})
        return Task.model_validate(result)

    # ------------------------------------------------------------------
    # tasks/list
    # ------------------------------------------------------------------

    async def list_tasks(
        self,
        *,
        context_id: str | None = None,
        state: TaskState | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[Task], str | None]:
        """List tasks on the remote server."""
        params: dict[str, Any] = {"limit": limit}
        if context_id:
            params["contextId"] = context_id
        if state:
            params["state"] = state.value
        if cursor:
            params["cursor"] = cursor

        result = await self._rpc("tasks/list", params)
        tasks = [Task.model_validate(t) for t in result.get("tasks", [])]
        next_cursor = result.get("nextCursor")
        return tasks, next_cursor

    # ------------------------------------------------------------------
    # tasks/cancel
    # ------------------------------------------------------------------

    async def cancel_task(self, task_id: str) -> Task:
        """Cancel a task on the remote server."""
        result = await self._rpc("tasks/cancel", {"taskId": task_id})
        return Task.model_validate(result)


class A2ASessionManager:
    """Manages multiple A2A client sessions.

    Useful when an Obscura agent needs to communicate with
    several remote A2A agents simultaneously.
    """

    def __init__(self) -> None:
        self._clients: dict[str, A2AClient] = {}

    async def add(
        self,
        name: str,
        base_url: str,
        *,
        auth_token: str | None = None,
    ) -> A2AClient:
        """Add and connect a new client session."""
        client = A2AClient(base_url, auth_token=auth_token)
        await client.connect()
        self._clients[name] = client
        return client

    def get(self, name: str) -> A2AClient | None:
        """Get a client by name."""
        return self._clients.get(name)

    async def remove(self, name: str) -> None:
        """Disconnect and remove a client."""
        client = self._clients.pop(name, None)
        if client:
            await client.disconnect()

    async def close_all(self) -> None:
        """Disconnect all clients."""
        for client in self._clients.values():
            await client.disconnect()
        self._clients.clear()

    def list_sessions(self) -> list[str]:
        """List active session names."""
        return list(self._clients.keys())
