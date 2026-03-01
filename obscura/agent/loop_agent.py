"""LoopAgent — freeform long-running conversation agent.

Does **not** follow the APER lifecycle.  Runs an indefinite
prompt → response loop, pausing between iterations to wait for
new user input or inter-agent messages via an async queue.

Usage::

    from obscura.core.client import ObscuraClient
    from obscura.agent.loop_agent import LoopAgent

    async with ObscuraClient("copilot", system_prompt="...") as client:
        agent = LoopAgent(client, name="researcher")

        # Feed input from another coroutine
        await agent.send("What's the latest on X?")

        # Start the agent (blocks until stopped)
        await agent.run_forever()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from obscura.agent.interaction import (
    AgentInput,
    AgentOutput,
    AttentionPriority,
    InteractionBus,
    UserResponse,
)
from obscura.core.types import AgentEventKind

if TYPE_CHECKING:
    from obscura.core.client import ObscuraClient

__all__ = ["LoopAgent"]

logger = logging.getLogger(__name__)


class LoopAgent:
    """Freeform long-running agent.  No APER phases.

    The agent sits in an indefinite loop:

    1. Wait for the next :class:`AgentInput` on the input queue.
    2. Drive the LLM tool-loop via ``ObscuraClient.run_loop``.
    3. Stream output events through the :class:`InteractionBus`.
    4. If the model requests confirmation, escalate via the bus.
    5. Go back to (1).

    The loop runs until :meth:`stop` is called.
    """

    def __init__(
        self,
        client: ObscuraClient,
        *,
        name: str = "loop-agent",
        agent_id: str = "",
        interaction_bus: InteractionBus | None = None,
        max_turns_per_input: int = 25,
    ) -> None:
        self._client = client
        self._name = name
        self._agent_id = agent_id or f"loop-{uuid4().hex[:8]}"
        self._bus = interaction_bus
        self._max_turns = max_turns_per_input
        self._input_queue: asyncio.Queue[AgentInput] = asyncio.Queue()
        self._stopped = False
        self._iteration = 0

    # -- Public properties ---------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def iteration(self) -> int:
        """Number of input→response cycles completed."""
        return self._iteration

    # -- Public API ----------------------------------------------------------

    async def send(self, content: str, *, source: str = "user") -> None:
        """Enqueue a new input for the agent to process.

        This is safe to call from any coroutine while the agent loop
        is running.
        """
        await self._input_queue.put(
            AgentInput(content=content, source=source),
        )

    async def run_forever(self) -> None:
        """Main loop: wait for input → process → emit output → repeat.

        Blocks until :meth:`stop` is called (or the task is cancelled).
        """
        logger.info("[%s] loop agent started (id=%s)", self._name, self._agent_id)
        self._stopped = False

        try:
            while not self._stopped:
                # ------ 1. Wait for the next input --------------------------
                input_msg = await self._get_next_input()
                if input_msg is None:
                    continue  # stop was called while waiting

                logger.debug(
                    "[%s] processing input #%d from %s",
                    self._name,
                    self._iteration,
                    input_msg.source,
                )

                # ------ 2. Drive the tool loop ------------------------------
                text_parts: list[str] = []
                try:
                    async for event in self._client.run_loop(
                        input_msg.content,
                        max_turns=self._max_turns,
                    ):
                        if event.kind == AgentEventKind.TEXT_DELTA:
                            text_parts.append(event.text)
                            await self._emit_output(event.text, is_final=False)

                        elif event.kind == AgentEventKind.TOOL_CALL:
                            logger.debug(
                                "[%s] tool call: %s", self._name, event.tool_name
                            )

                        elif event.kind == AgentEventKind.TOOL_RESULT:
                            logger.debug(
                                "[%s] tool result: %s (error=%s)",
                                self._name,
                                event.tool_name,
                                event.is_error,
                            )

                        elif event.kind == AgentEventKind.CONFIRMATION_REQUEST:
                            response = await self._request_attention(
                                f"Tool '{event.tool_name}' wants to run. Approve?",
                                priority=AttentionPriority.HIGH,
                                actions=("approve", "deny"),
                            )
                            if response is not None and response.action == "deny":
                                logger.info(
                                    "[%s] user denied tool %s",
                                    self._name,
                                    event.tool_name,
                                )

                        elif event.kind == AgentEventKind.AGENT_DONE:
                            break

                except Exception:
                    logger.exception("[%s] error during tool loop", self._name)
                    await self._emit_output(
                        "[error] An error occurred during processing.",
                        is_final=True,
                        event_kind=AgentEventKind.ERROR,
                    )
                    continue

                # ------ 3. Emit final output --------------------------------
                final_text = "".join(text_parts)
                if final_text:
                    await self._emit_output("", is_final=True)

                self._iteration += 1

        except asyncio.CancelledError:
            logger.info("[%s] loop agent cancelled", self._name)
        finally:
            self._stopped = True
            logger.info("[%s] loop agent stopped", self._name)

    async def stop(self) -> None:
        """Signal the agent to stop after the current iteration."""
        self._stopped = True
        # Unblock _get_next_input if it's waiting
        try:
            self._input_queue.put_nowait(
                AgentInput(content="", source="__stop__"),
            )
        except asyncio.QueueFull:
            pass

    # -- Internal helpers ----------------------------------------------------

    async def _get_next_input(self) -> AgentInput | None:
        """Block until an input arrives, or return ``None`` if stopped."""
        while not self._stopped:
            try:
                msg = await asyncio.wait_for(
                    self._input_queue.get(), timeout=1.0,
                )
                if msg.source == "__stop__":
                    return None
                return msg
            except asyncio.TimeoutError:
                continue
        return None

    async def _emit_output(
        self,
        text: str,
        *,
        is_final: bool = False,
        event_kind: AgentEventKind | None = None,
    ) -> None:
        """Push output through the InteractionBus (if wired)."""
        if self._bus is None:
            return
        output = AgentOutput(
            agent_id=self._agent_id,
            agent_name=self._name,
            text=text,
            event_kind=event_kind,
            is_final=is_final,
        )
        try:
            await self._bus.emit_output(output)
        except Exception:
            logger.exception("[%s] failed to emit output", self._name)

    async def _request_attention(
        self,
        message: str,
        *,
        priority: AttentionPriority = AttentionPriority.NORMAL,
        actions: tuple[str, ...] | list[str] | None = None,
        context: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> UserResponse | None:
        """Ask for user attention via the InteractionBus.

        Returns ``None`` if no bus is wired or if the request times out.
        """
        if self._bus is None:
            logger.debug(
                "[%s] no interaction bus — skipping attention request", self._name
            )
            return None

        try:
            return await self._bus.request_attention(
                agent_id=self._agent_id,
                agent_name=self._name,
                message=message,
                priority=priority,
                actions=actions,
                context=context,
                timeout=timeout,
            )
        except Exception:
            logger.exception("[%s] attention request failed", self._name)
            return None
