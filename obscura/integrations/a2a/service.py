"""
obscura.a2a.service — Protocol-agnostic A2A business logic.

The ``A2AService`` is the core: it receives method calls from any
transport (JSON-RPC, REST, SSE, gRPC) and orchestrates task creation,
agent execution, streaming, and state management.

Transports are thin adapters that delegate here.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, AsyncIterator, Callable, Awaitable

from obscura.integrations.a2a.event_mapper import EventMapper
from obscura.integrations.a2a.store import TaskStore
from obscura.integrations.a2a.types import (
    A2AMessage,
    AgentCard,
    Artifact,
    StreamEvent,
    Task,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)
from obscura.core.types import AgentEvent, AgentEventKind, ToolCallInfo

logger = logging.getLogger(__name__)


class A2AService:
    """Core A2A service — protocol-agnostic business logic.

    Parameters
    ----------
    store:
        Task persistence backend (in-memory or Redis).
    agent_card:
        Pre-built agent card for ``/.well-known/agent.json``.
    get_runtime:
        Factory that returns an ``AgentRuntime`` for a given user dict.
        The A2A layer injects ``on_confirm`` callbacks so agent loops
        can be paused for external input.
    agent_model:
        Model backend to use when spawning agents (default: ``"copilot"``).
    agent_system_prompt:
        System prompt for spawned agents.
    """

    def __init__(
        self,
        store: TaskStore,
        agent_card: AgentCard,
        *,
        get_runtime: Callable[..., Any] | None = None,
        agent_model: str = "copilot",
        agent_system_prompt: str = "",
        agent_max_turns: int = 10,
    ) -> None:
        self._store = store
        self._agent_card = agent_card
        self._get_runtime = get_runtime
        self._agent_model = agent_model
        self._agent_system_prompt = agent_system_prompt
        self._agent_max_turns = agent_max_turns

        # Active tasks: task_id → asyncio.Task wrapping agent execution
        self._running: dict[str, asyncio.Task[Any]] = {}

        # Pending confirmations: task_id → (Event, result dict)
        self._pending_confirmations: dict[
            str, tuple[asyncio.Event, dict[str, bool]]
        ] = {}

        # Context → agent mapping for multi-turn conversations
        self._context_agents: dict[str, str] = {}

    @property
    def store(self) -> TaskStore:
        """Read-only access to the task store."""
        return self._store

    @property
    def agent_card(self) -> AgentCard:
        """Read-only agent card."""
        return self._agent_card

    # ------------------------------------------------------------------
    # Agent Card
    # ------------------------------------------------------------------

    def get_agent_card(self) -> AgentCard:
        """Return the agent card for /.well-known/agent.json."""
        return self._agent_card

    # ------------------------------------------------------------------
    # message/send — blocking request
    # ------------------------------------------------------------------

    async def message_send(
        self,
        message: A2AMessage,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        blocking: bool = True,
    ) -> Task:
        """Handle a ``message/send`` request.

        If ``task_id`` is provided and the task is in INPUT_REQUIRED,
        this delivers the user's response and resumes the agent.
        Otherwise, creates a new task and runs the agent.
        """
        ctx_id = context_id or message.contextId or f"ctx-{uuid.uuid4().hex[:12]}"

        # Resume existing task waiting for input
        if task_id and task_id in self._pending_confirmations:
            return await self._resume_task(task_id, message)

        # Create new task
        task = await self._store.create_task(ctx_id, message)
        logger.info("Created task %s in context %s", task.id, ctx_id)

        if blocking:
            await self._run_agent_blocking(task)
            refreshed = await self._store.get_task(task.id)
            return refreshed or task
        else:
            self._run_agent_background(task)
            return task

    # ------------------------------------------------------------------
    # message/stream — streaming request
    # ------------------------------------------------------------------

    async def message_stream(
        self,
        message: A2AMessage,
        *,
        context_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Handle a ``message/stream`` request.

        Creates a task, starts agent execution, and yields events
        as the agent produces them.
        """
        ctx_id = context_id or message.contextId or f"ctx-{uuid.uuid4().hex[:12]}"
        task = await self._store.create_task(ctx_id, message)

        # Set up streaming via pub/sub
        mapper = EventMapper(task.id, ctx_id)

        # Transition to WORKING
        await self._store.transition(task.id, TaskState.WORKING)
        yield mapper.status_event(TaskState.WORKING)

        # Run agent and stream events
        try:
            prompt = self._extract_text(message)
            async for event in self._execute_agent_stream(task, prompt):
                a2a_events = mapper.map(event)
                for a2a_event in a2a_events:
                    # Skip redundant WORKING status from mapper since we already sent it
                    if (
                        isinstance(a2a_event, TaskStatusUpdateEvent)
                        and a2a_event.status.state == TaskState.WORKING
                        and not a2a_event.final
                    ):
                        continue
                    yield a2a_event
        except Exception as e:
            logger.error("Agent execution failed for task %s: %s", task.id, e)
            await self._store.transition(task.id, TaskState.FAILED)
            yield mapper.status_event(TaskState.FAILED, final=True)

    # ------------------------------------------------------------------
    # tasks/get
    # ------------------------------------------------------------------

    async def tasks_get(self, task_id: str) -> Task | None:
        """Return a task by ID."""
        return await self._store.get_task(task_id)

    # ------------------------------------------------------------------
    # tasks/list
    # ------------------------------------------------------------------

    async def tasks_list(
        self,
        context_id: str | None = None,
        state: TaskState | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[Task], str | None]:
        """List tasks with optional filtering and pagination."""
        return await self._store.list_tasks(
            context_id=context_id, state=state, cursor=cursor, limit=limit
        )

    # ------------------------------------------------------------------
    # tasks/cancel
    # ------------------------------------------------------------------

    async def tasks_cancel(self, task_id: str) -> Task:
        """Cancel a running or pending task."""
        # Cancel the running asyncio task if any
        running = self._running.pop(task_id, None)
        if running and not running.done():
            running.cancel()

        # Clear pending confirmations
        self._pending_confirmations.pop(task_id, None)

        return await self._store.cancel_task(task_id)

    # ------------------------------------------------------------------
    # tasks/subscribe
    # ------------------------------------------------------------------

    async def tasks_subscribe(
        self, task_id: str
    ) -> AsyncIterator[StreamEvent]:
        """Subscribe to real-time updates for a task."""
        async for event in self._store.subscribe(task_id):
            yield event

    # ------------------------------------------------------------------
    # Confirmation bridge: on_confirm → INPUT_REQUIRED
    # ------------------------------------------------------------------

    def _make_on_confirm(self, task_id: str) -> Callable[[ToolCallInfo], Awaitable[bool]]:
        """Create an on_confirm callback that bridges to A2A INPUT_REQUIRED.

        When the agent loop wants to confirm a tool call, this:
        1. Transitions the task to INPUT_REQUIRED
        2. Publishes an update for streaming clients
        3. Parks the agent loop via asyncio.Event
        4. Resumes when external client sends a follow-up message
        """
        confirmation_event = asyncio.Event()
        confirmation_result: dict[str, bool] = {"approved": False}
        self._pending_confirmations[task_id] = (confirmation_event, confirmation_result)

        async def on_confirm(tool_call: ToolCallInfo) -> bool:
            # Transition to INPUT_REQUIRED
            confirm_msg = A2AMessage(
                role="agent",
                messageId=f"confirm-{uuid.uuid4().hex[:8]}",
                parts=[TextPart(text=f"Approve tool call: {tool_call.name}({tool_call.input})")],
            )
            await self._store.transition(task_id, TaskState.INPUT_REQUIRED, message=confirm_msg)

            # Publish update for streaming subscribers
            task = await self._store.get_task(task_id)
            if task:
                await self._store.publish_update(
                    task_id,
                    TaskStatusUpdateEvent(
                        taskId=task_id,
                        contextId=task.contextId,
                        status=task.status,
                    ),
                )

            # Park — wait for external client to respond
            await confirmation_event.wait()
            confirmation_event.clear()

            return confirmation_result.get("approved", False)

        return on_confirm

    async def _resume_task(self, task_id: str, message: A2AMessage) -> Task:
        """Resume a task that's waiting for input (INPUT_REQUIRED)."""
        pending = self._pending_confirmations.get(task_id)
        if not pending:
            # No pending confirmation — just append message
            return await self._store.append_message(task_id, message)

        confirmation_event, confirmation_result = pending

        # Determine approval from message content
        text = self._extract_text(message).lower().strip()
        approved = text in ("yes", "true", "approve", "confirm", "ok", "y", "1")
        confirmation_result["approved"] = approved

        # Append the user's response to history
        await self._store.append_message(task_id, message)

        # Transition back to WORKING
        await self._store.transition(task_id, TaskState.WORKING)

        # Wake the agent loop
        confirmation_event.set()

        # Return current task state
        task = await self._store.get_task(task_id)
        assert task is not None, f"Task {task_id} not found after transition"
        return task

    # ------------------------------------------------------------------
    # Agent execution internals
    # ------------------------------------------------------------------

    async def _run_agent_blocking(self, task: Task) -> None:
        """Run agent synchronously (block until completion)."""
        try:
            prompt = self._extract_text_from_history(task)
            await self._store.transition(task.id, TaskState.WORKING)

            result_text = await self._execute_agent(task, prompt)

            # Add result as artifact
            if result_text:
                artifact = Artifact(
                    artifactId=f"art-{uuid.uuid4().hex[:8]}",
                    parts=[TextPart(text=result_text)],
                )
                await self._store.add_artifact(task.id, artifact)

            await self._store.transition(task.id, TaskState.COMPLETED)

        except asyncio.CancelledError:
            try:
                await self._store.transition(task.id, TaskState.CANCELED)
            except Exception:
                pass
        except Exception as e:
            logger.error("Agent execution failed for task %s: %s", task.id, e)
            try:
                await self._store.transition(task.id, TaskState.FAILED)
            except Exception:
                pass

    def _run_agent_background(self, task: Task) -> None:
        """Start agent execution as a background asyncio task."""
        async_task = asyncio.create_task(self._run_agent_blocking(task))
        self._running[task.id] = async_task

        def _cleanup(t: asyncio.Task[Any]) -> None:
            self._running.pop(task.id, None)

        async_task.add_done_callback(_cleanup)

    async def _execute_agent(self, task: Task, prompt: str) -> str:
        """Execute the agent and return the final text result.

        If ``get_runtime`` is not configured (e.g., in tests), returns
        a placeholder indicating no runtime is available.
        """
        if not self._get_runtime:
            logger.warning("No agent runtime configured — returning placeholder")
            return f"[No agent runtime] Received: {prompt}"

        runtime = self._get_runtime()
        agent = runtime.spawn(
            name=f"a2a-{task.id}",
            model=self._agent_model,
            system_prompt=self._agent_system_prompt,
        )
        await agent.start()

        on_confirm = self._make_on_confirm(task.id)
        try:
            result = await agent.run_loop(
                prompt,
                max_turns=self._agent_max_turns,
                on_confirm=on_confirm,
            )
            return str(result) if result else ""
        finally:
            await agent.stop()

    async def _execute_agent_stream(
        self, task: Task, prompt: str,
    ) -> AsyncIterator[AgentEvent]:
        """Execute the agent and yield AgentEvent objects."""
        if not self._get_runtime:
            logger.warning("No agent runtime configured — yielding placeholder")
            yield AgentEvent(kind=AgentEventKind.TURN_START)
            yield AgentEvent(
                kind=AgentEventKind.TEXT_DELTA,
                text=f"[No agent runtime] Received: {prompt}",
            )
            yield AgentEvent(kind=AgentEventKind.TURN_COMPLETE)
            yield AgentEvent(kind=AgentEventKind.AGENT_DONE)
            return

        runtime = self._get_runtime()
        agent = runtime.spawn(
            name=f"a2a-{task.id}",
            model=self._agent_model,
            system_prompt=self._agent_system_prompt,
        )
        await agent.start()

        on_confirm = self._make_on_confirm(task.id)
        try:
            async for event in agent.stream_loop(
                prompt,
                max_turns=self._agent_max_turns,
                on_confirm=on_confirm,
            ):
                yield event
        finally:
            await agent.stop()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(message: A2AMessage) -> str:
        """Extract plain text from a message's parts."""
        texts: list[str] = []
        for part in message.parts:
            if isinstance(part, TextPart):
                texts.append(part.text)
        return " ".join(texts).strip() or "[empty message]"

    @staticmethod
    def _extract_text_from_history(task: Task) -> str:
        """Extract the most recent user message text from task history."""
        for msg in reversed(task.history):
            if msg.role == "user":
                texts: list[str] = []
                for part in msg.parts:
                    if isinstance(part, TextPart):
                        texts.append(part.text)
                text = " ".join(texts).strip()
                if text:
                    return text
        return "[empty]"
