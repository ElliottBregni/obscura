"""
sdk.handlers — Lightweight request handler protocol.

For single-purpose handlers that don't need the full APER loop,
:class:`RequestHandler` defines a minimal interface and
:class:`SimpleHandler` wraps a single ``ObscuraClient.send()`` call.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable, TYPE_CHECKING

from sdk.internal.types import Message

if TYPE_CHECKING:
    from sdk.client import ObscuraClient


@runtime_checkable
class RequestHandler(Protocol):
    """Protocol for single-purpose request handlers."""

    async def handle(self, request: Any) -> Any: ...


class SimpleHandler:
    """Wraps a single ``ObscuraClient.send()`` call.

    Usage::

        handler = SimpleHandler(client, system_prompt="You are a helpful assistant.")
        response = await handler.handle("What is 2+2?")
        print(response.text)
    """

    def __init__(self, client: ObscuraClient, system_prompt: str = "") -> None:
        self._client = client
        self._system_prompt = system_prompt

    async def handle(self, request: Any) -> Message:
        """Send *request* as a prompt and return the response."""
        prompt = str(request)
        if self._system_prompt:
            prompt = f"{self._system_prompt}\n\n{prompt}"
        return await self._client.send(prompt)
