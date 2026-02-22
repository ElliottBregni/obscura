"""InteractionBus — unified pub/sub for agent-to-user communication.

Agents publish attention requests when they need user input.  UI surfaces
(TUI, Web UI, macOS popups) subscribe and render them, then route the
user's response back to the requesting agent.

Usage::

    bus = InteractionBus()

    # Agent side
    response = await bus.request_attention(
        agent_id="abc",
        agent_name="researcher",
        message="Found conflicting data — which source should I trust?",
        priority=AttentionPriority.HIGH,
        actions=["source_a", "source_b", "skip"],
    )

    # UI side
    bus.on_attention(my_handler)
    await bus.respond(request_id, action="source_a")
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable
from uuid import uuid4

from obscura.core.types import AgentEventKind

__all__ = [
    "AttentionPriority",
    "AttentionRequest",
    "UserResponse",
    "AgentOutput",
    "AgentInput",
    "InteractionBus",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Priority levels
# ---------------------------------------------------------------------------


class AttentionPriority(enum.Enum):
    """How urgently the agent needs user attention."""

    LOW = "low"  # Log only, no popup
    NORMAL = "normal"  # Toast notification
    HIGH = "high"  # Banner notification, stays visible
    CRITICAL = "critical"  # Modal dialog, blocks until dismissed


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttentionRequest:
    """Published by an agent when it needs user input."""

    request_id: str
    agent_id: str
    agent_name: str
    message: str
    priority: AttentionPriority = AttentionPriority.NORMAL
    actions: tuple[str, ...] = ("ok",)
    context: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class UserResponse:
    """Sent by the UI in response to an :class:`AttentionRequest`."""

    request_id: str
    action: str  # one of AttentionRequest.actions, or free text
    text: str = ""  # optional free-form elaboration
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class AgentOutput:
    """Streamed output from an agent to subscribed UI surfaces."""

    agent_id: str
    agent_name: str
    text: str = ""
    event_kind: AgentEventKind | None = None
    is_final: bool = False


@dataclass(frozen=True)
class AgentInput:
    """Input enqueued for a long-running agent to consume."""

    content: str
    source: str = "user"  # "user", agent_id, "system", "trigger"
    metadata: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Callback types
# ---------------------------------------------------------------------------

AttentionHandler = Callable[[AttentionRequest], Awaitable[None]]
OutputHandler = Callable[[AgentOutput], Awaitable[None]]


# ---------------------------------------------------------------------------
# InteractionBus
# ---------------------------------------------------------------------------


class InteractionBus:
    """Pub/sub hub connecting agents to UI surfaces.

    Agents call :meth:`request_attention` to ask the user something and
    block until the user responds.  UI surfaces subscribe via
    :meth:`on_attention` and call :meth:`respond` when the user acts.

    Thread-safety: all methods are coroutine-based; callers must share
    a single event loop (the default in an ``asyncio`` application).
    """

    def __init__(self) -> None:
        self._attention_subscribers: list[AttentionHandler] = []
        self._output_subscribers: list[OutputHandler] = []
        self._response_waiters: dict[str, asyncio.Future[UserResponse]] = {}

    # -- Agent-facing API ---------------------------------------------------

    async def request_attention(
        self,
        agent_id: str,
        agent_name: str,
        message: str,
        *,
        priority: AttentionPriority = AttentionPriority.NORMAL,
        actions: tuple[str, ...] | list[str] | None = None,
        context: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> UserResponse:
        """Publish an attention request and block until the user responds.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait.  ``None`` waits forever.
        """
        resolved_actions = tuple(actions) if actions else ("ok",)
        request = AttentionRequest(
            request_id=uuid4().hex,
            agent_id=agent_id,
            agent_name=agent_name,
            message=message,
            priority=priority,
            actions=resolved_actions,
            context=context or {},
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[UserResponse] = loop.create_future()
        self._response_waiters[request.request_id] = future

        # Fan out to all subscribers
        for handler in self._attention_subscribers:
            try:
                await handler(request)
            except Exception:
                logger.exception("Attention handler failed for %s", request.request_id)

        try:
            if timeout is not None:
                return await asyncio.wait_for(future, timeout=timeout)
            return await future
        except asyncio.TimeoutError:
            self._response_waiters.pop(request.request_id, None)
            return UserResponse(
                request_id=request.request_id,
                action="timeout",
                text="User did not respond in time.",
            )

    async def emit_output(self, output: AgentOutput) -> None:
        """Broadcast agent output to all subscribed UI surfaces."""
        for handler in self._output_subscribers:
            try:
                await handler(output)
            except Exception:
                logger.exception("Output handler failed for agent %s", output.agent_id)

    # -- UI-facing API ------------------------------------------------------

    def on_attention(self, callback: AttentionHandler) -> None:
        """Register a handler called when any agent requests attention."""
        self._attention_subscribers.append(callback)

    def remove_attention_handler(self, callback: AttentionHandler) -> None:
        """Unsubscribe an attention handler."""
        try:
            self._attention_subscribers.remove(callback)
        except ValueError:
            pass

    def on_output(self, callback: OutputHandler) -> None:
        """Register a handler called when an agent emits output."""
        self._output_subscribers.append(callback)

    def remove_output_handler(self, callback: OutputHandler) -> None:
        """Unsubscribe an output handler."""
        try:
            self._output_subscribers.remove(callback)
        except ValueError:
            pass

    async def respond(self, request_id: str, action: str, text: str = "") -> None:
        """Route a user response back to the requesting agent."""
        response = UserResponse(request_id=request_id, action=action, text=text)
        future = self._response_waiters.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(response)
        else:
            logger.warning(
                "No pending waiter for attention request %s (action=%s)",
                request_id,
                action,
            )

    # -- Introspection ------------------------------------------------------

    @property
    def pending_requests(self) -> list[str]:
        """Request IDs still waiting for a user response."""
        return [rid for rid, f in self._response_waiters.items() if not f.done()]

    def has_pending(self) -> bool:
        """Return True if any attention requests are awaiting response."""
        return any(not f.done() for f in self._response_waiters.values())
