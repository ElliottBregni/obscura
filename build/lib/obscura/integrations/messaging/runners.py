"""obscura.integrations.messaging.runners — Default agent-runner abstractions.

Both ``messaging.router`` and ``messaging.kairos_runner`` need
``ObscuraAgentRunner`` (router instantiates it as the default; kairos_runner
delegates to it for the immediate path). Defining it here — below both —
breaks the peer cycle that previously forced router to lazy-import
``KairosAgentRunner`` inside ``apply_config``.

This module has no obscura.integrations.messaging.* deps; it pulls only
from ``obscura.core``.
"""

from __future__ import annotations

from typing import Any, Protocol

from obscura.core.agent_loop_factory import make_agent_loop
from obscura.core.hooks import HookRegistry
from obscura.core.enums.agent import AgentEventKind, Role
from obscura.core.types import ContentBlock, Message


class AgentRunnerProtocol(Protocol):
    """Anything that can run an agent given a prompt + history and return a string."""

    async def run_turn(
        self,
        prompt: str,
        *,
        session_id: str,
        history: list[dict[str, str]],
        system_prompt: str,
        max_turns: int,
    ) -> str:
        """Run one agent turn and return the full response text."""
        ...


class ObscuraAgentRunner:
    """Runs a single conversation turn using Obscura's AgentLoop directly.

    This is the default runner — no HTTP, no OpenClaw, no external bridge.
    It creates an ephemeral AgentLoop per turn using the provided backend.
    """

    def __init__(
        self,
        backend: Any,  # BackendProtocol
        tool_registry: Any,  # ToolRegistry
        *,
        event_store: Any | None = None,
    ) -> None:
        self._backend = backend
        self._tool_registry = tool_registry
        self._event_store = event_store

    async def run_turn(
        self,
        prompt: str,
        *,
        session_id: str,
        history: list[dict[str, str]],
        system_prompt: str,
        max_turns: int,
    ) -> str:
        """Run one agent turn and collect the full response text."""
        # Rebuild history as Message objects
        messages: list[Message] = []
        for entry in history:
            role_str = entry.get("role", "user")
            text = entry.get("text", "")
            role = Role.USER if role_str == "user" else Role.ASSISTANT
            messages.append(
                Message(role=role, content=[ContentBlock(kind="text", text=text)])
            )

        hooks = HookRegistry()
        loop = make_agent_loop(
            self._backend,
            self._tool_registry,
            hooks=hooks,
            event_store=self._event_store,
        )

        full_response_parts: list[str] = []

        async for event in loop.run(
            prompt,
            session_id=session_id,
            initial_messages=messages,
            max_turns=max_turns,
            system_prompt=system_prompt,
        ):
            if event.kind == AgentEventKind.TEXT_DELTA and event.text:
                full_response_parts.append(event.text)

        return "".join(full_response_parts).strip() or "(no response)"
