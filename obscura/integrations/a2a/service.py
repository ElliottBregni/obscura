"""obscura.a2a.service — Protocol-agnostic A2A business logic.

The ``A2AService`` is the core: it receives method calls from any
transport (JSON-RPC, REST, SSE, gRPC) and orchestrates task creation,
agent execution, streaming, and state management.

Transports are thin adapters that delegate here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import TYPE_CHECKING, Any

from obscura.core.enums.protocol import A2ARole, A2ATaskState
from obscura.core.types import AgentEvent, ToolCallInfo
from obscura.integrations.a2a.event_mapper import EventMapper
from obscura.integrations.a2a.types import (
    A2AMessage,
    AgentCard,
    Artifact,
    StreamEvent,
    Task,
    TaskStatusUpdateEvent,
    TextPart,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from obscura.integrations.a2a.store import TaskStore

logger = logging.getLogger(__name__)


class A2AService:
    """Core A2A service — protocol-agnostic business logic.

    Per-task agent execution goes through ``obscura.composition.a2a.
    build_a2a_session``, which constructs an ``AgentSession`` with all
    plugin tools registered and the backend started. This means A2A
    agents can call tools end-to-end — the previous ``get_runtime``-
    based design left agents toolless when ``get_runtime`` was unset
    (the production default), and they returned placeholder strings.

    Parameters
    ----------
    store:
        Task persistence backend (in-memory or Redis).
    agent_card:
        Pre-built agent card for ``/.well-known/agent.json``.
    agent_model:
        Model backend to use when spawning agents (default: ``"copilot"``).
    agent_system_prompt:
        System prompt for spawned agents.
    agent_max_turns:
        Max model turns per task before the loop bails.
    agent_backend:
        Provider backend identifier (``"copilot"``, ``"claude"``, …).
        Passed straight through to ``SessionConfig.backend``.
    """

    def __init__(
        self,
        store: TaskStore,
        agent_card: AgentCard,
        *,
        agent_backend: str = "copilot",
        agent_model: str = "copilot",
        agent_system_prompt: str = "",
        agent_max_turns: int = 10,
    ) -> None:
        self._store = store
        self._agent_card = agent_card
        self._agent_backend = agent_backend
        self._agent_model = agent_model
        self._agent_system_prompt = agent_system_prompt
        self._agent_max_turns = agent_max_turns

        # Active tasks: task_id → asyncio.Task wrapping agent execution
        self._running: dict[str, asyncio.Task[Any]] = {}

        # Pending input-required parking: task_id → (Event, result_dict).
        # Result dict carries 'kind' (confirm/ask/plan/permission),
        # 'approved' (bool), 'answer' (str). _resume_task fills it in
        # when the user replies, then sets the event to wake the
        # agent-loop callback.
        self._pending_confirmations: dict[
            str,
            tuple[asyncio.Event, dict[str, Any]],
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
        push_notification_url: str | None = None,
    ) -> Task:
        """Handle a ``message/send`` request.

        If ``task_id`` is provided and the task is in INPUT_REQUIRED,
        this delivers the user's response and resumes the agent.
        Otherwise, creates a new task and runs the agent.

        Parameters
        ----------
        push_notification_url:
            When ``blocking=False``, POST the completed task JSON to this URL
            after agent execution finishes (best-effort, fire-and-forget).
        """
        ctx_id = context_id or message.contextId or f"ctx-{uuid.uuid4().hex[:12]}"

        # Resume existing task waiting for input
        if task_id and task_id in self._pending_confirmations:
            return await self._resume_task(task_id, message)

        # Create new task
        task = await self._store.create_task(ctx_id, message)
        logger.info("Created task %s in context %s", task.id, ctx_id)

        # --- Channel inject: if a REPL is running, route through the REPL
        # queue so the user sees and responds to the message directly rather
        # than spawning an autonomous agent.  Works for both blocking and
        # non-blocking callers.
        channel_result = await self._try_channel_inject(
            task,
            message,
            blocking=blocking,
            push_notification_url=push_notification_url,
        )
        if channel_result is not None:
            return channel_result

        # Fallback: no REPL present — run autonomous agent
        if blocking:
            await self._run_agent_blocking(task)
            refreshed = await self._store.get_task(task.id)
            return refreshed or task

        # Non-blocking: run in background, fire push notification when done
        if push_notification_url:
            self._run_agent_background_with_push(task, push_notification_url)
        else:
            self._run_agent_background(task)
        return task

    # Per-sender rate limit: max N injects per sliding window
    _INJECT_RATE_LIMIT: int = 10           # max messages
    _INJECT_RATE_WINDOW: float = 60.0      # per this many seconds
    _INJECT_TIMEOUT_MAX: float = 600.0     # hard cap on inject_timeout
    _INJECT_TIMEOUT_DEFAULT: float = 300.0

    # sender_id → deque of timestamps
    _inject_rate_buckets: dict[str, list[float]] = {}

    def _inject_rate_check(self, sender_id: str) -> bool:
        """Return True if the sender is within rate limit, False if throttled."""
        import time
        now = time.monotonic()
        window = self._INJECT_RATE_WINDOW
        bucket = self._inject_rate_buckets.setdefault(sender_id, [])
        # Prune old entries
        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= self._INJECT_RATE_LIMIT:
            return False
        bucket.append(now)
        return True

    @staticmethod
    def _sanitize_inject_label(value: str) -> str:
        """Strip characters that could escape the '[Platform from X]: ' REPL prefix."""
        return (
            value.replace("[", "")
                 .replace("]", "")
                 .replace("\n", " ")
                 .replace("\r", "")
                 .replace("\x00", "")
            [:128]
        )

    async def _try_channel_inject(
        self,
        task: Task,
        message: A2AMessage,
        *,
        blocking: bool,
        push_notification_url: str | None,
        inject_timeout: float | None = None,
    ) -> Task | None:
        """Attempt to inject the A2A message into the REPL channel queue.

        Returns the completed/submitted Task if injection succeeded,
        or ``None`` if no REPL is listening (caller falls back to autonomous agent).

        Blocking callers wait up to ``inject_timeout`` seconds for the REPL
        to reply.  Non-blocking callers return immediately with state SUBMITTED
        and the reply_fn updates the task + fires the push notification.

        Hardening:
        - Liveness check: skips inject if no REPL coroutine is waiting on the queue.
        - Rate limit: max 10 messages per sender per 60s.
        - Label sanitization: strips bracket/control chars from sender metadata.
        - Timeout cap: inject_timeout is clamped to [1, 600] seconds.
        - CancelledError: transitions task to FAILED on server shutdown.
        - asyncio best practice: uses get_running_loop().create_task().
        """
        try:
            from obscura.integrations.messaging.channel_inject import (
                ChannelMessage,
                get_channel_queue,
                push_channel_message,
            )
        except ImportError:
            return None

        queue = get_channel_queue()

        # Liveness check: only inject if the REPL is actively waiting for messages.
        # asyncio.Queue stores pending getters in _getters (internal, stable since 3.4).
        if not getattr(queue, "_getters", None):
            logger.debug("A2A channel inject: no REPL waiter, skipping inject for task %s", task.id)
            return None

        text = self._extract_text(message)
        if not text:
            logger.debug("A2A channel inject: no text in message for task %s", task.id)
            return None

        # Sanitize sender metadata — untrusted input from the peer
        raw_label = (message.metadata.get("from", "") if message.metadata else "") or ""
        sender_label = self._sanitize_inject_label(raw_label)
        sender_id = sender_label or f"a2a:{task.id[:8]}"
        display_name = sender_label or "A2A peer"

        # Per-sender rate limit
        if not self._inject_rate_check(sender_id):
            logger.warning(
                "A2A channel inject: rate limit hit for sender=%s task=%s — falling back to agent",
                sender_id, task.id,
            )
            return None

        # Resolve and clamp inject_timeout
        _timeout = inject_timeout if inject_timeout is not None else self._INJECT_TIMEOUT_DEFAULT
        _timeout = max(1.0, min(_timeout, self._INJECT_TIMEOUT_MAX))

        if blocking:
            # Blocking path: create a Future the reply_fn resolves.
            loop = asyncio.get_running_loop()
            _reply_future: asyncio.Future[str] = loop.create_future()

            async def _reply_blocking(response_text: str) -> bool:
                if not _reply_future.done():
                    _reply_future.set_result(response_text)
                return True

            channel_msg = ChannelMessage(
                platform="a2a",
                sender_id=sender_id,
                text=text,
                reply_fn=_reply_blocking,
                display_name=display_name,
            )
            pushed = push_channel_message(channel_msg)
            if not pushed:
                logger.warning(
                    "A2A channel inject: queue full, falling back to agent for task %s sender=%s",
                    task.id, sender_id,
                )
                return None

            logger.info(
                "A2A task %s injected into REPL channel (blocking, timeout=%ss) sender=%s",
                task.id, _timeout, sender_id,
            )
            await self._store.transition(task.id, A2ATaskState.WORKING)

            try:
                response_text = await asyncio.wait_for(_reply_future, timeout=_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "A2A channel inject: REPL did not reply within %ss for task %s — "
                    "falling back to autonomous agent",
                    _timeout, task.id,
                )
                if not _reply_future.done():
                    _reply_future.cancel()
                await self._run_agent_blocking(task)
                return await self._store.get_task(task.id) or task
            except asyncio.CancelledError:
                logger.warning(
                    "A2A channel inject: server cancelled while waiting for reply on task %s",
                    task.id,
                )
                with contextlib.suppress(Exception):
                    await self._store.transition(task.id, A2ATaskState.FAILED)
                raise

            # Store response as artifact
            await self._store.add_artifact(
                task.id,
                Artifact(parts=[TextPart(text=response_text)]),
            )
            await self._store.transition(task.id, A2ATaskState.COMPLETED)
            return await self._store.get_task(task.id) or task

        else:
            # Non-blocking path: reply_fn updates task + fires push notification.
            _push_url = push_notification_url  # capture for closure

            async def _reply_nonblocking(response_text: str) -> bool:
                try:
                    await self._store.add_artifact(
                        task.id,
                        Artifact(parts=[TextPart(text=response_text)]),
                    )
                    await self._store.transition(task.id, A2ATaskState.COMPLETED)
                    if _push_url:
                        completed = await self._store.get_task(task.id)
                        if completed:
                            asyncio.get_running_loop().create_task(
                                self._fire_push_notification(completed, _push_url)
                            )
                except Exception:
                    logger.debug("A2A channel inject: reply_fn error", exc_info=True)
                return True

            channel_msg = ChannelMessage(
                platform="a2a",
                sender_id=sender_id,
                text=text,
                reply_fn=_reply_nonblocking,
                display_name=display_name,
            )
            pushed = push_channel_message(channel_msg)
            if not pushed:
                logger.warning(
                    "A2A channel inject: queue full, falling back to agent for task %s sender=%s",
                    task.id, sender_id,
                )
                return None

            logger.info(
                "A2A task %s injected into REPL channel (non-blocking) sender=%s",
                task.id, sender_id,
            )
            await self._store.transition(task.id, A2ATaskState.SUBMITTED)
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
        await self._store.transition(task.id, A2ATaskState.WORKING)
        yield mapper.status_event(A2ATaskState.WORKING)

        # Run agent and stream events
        try:
            prompt = self._extract_text(message)
            async for event in self._execute_agent_stream(task, prompt):
                a2a_events = mapper.map(event)
                for a2a_event in a2a_events:
                    # Skip redundant WORKING status from mapper since we already sent it
                    if (
                        isinstance(a2a_event, TaskStatusUpdateEvent)
                        and a2a_event.status.state == A2ATaskState.WORKING
                        and not a2a_event.final
                    ):
                        continue
                    yield a2a_event
        except Exception as e:
            logger.exception("Agent execution failed for task %s: %s", task.id, e)
            await self._store.transition(task.id, A2ATaskState.FAILED)
            yield mapper.status_event(A2ATaskState.FAILED, final=True)

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
        state: A2ATaskState | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[Task], str | None]:
        """List tasks with optional filtering and pagination."""
        return await self._store.list_tasks(
            context_id=context_id,
            state=state,
            cursor=cursor,
            limit=limit,
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

    async def tasks_subscribe(self, task_id: str) -> AsyncIterator[StreamEvent]:
        """Subscribe to real-time updates for a task."""
        async for event in self._store.subscribe(task_id):
            yield event

    # ------------------------------------------------------------------
    # Input-required bridge: tool confirm + ask_user + permission +
    # plan_approval all flow through the same INPUT_REQUIRED machinery.
    #
    # _pending_confirmations[task_id] stores (Event, result_dict). The
    # result dict holds:
    #   - "kind": "confirm" | "ask" | "permission" | "plan"
    #   - "approved": bool (for binary kinds)
    #   - "answer":   str  (for free-text "ask" kind)
    # _resume_task reads `kind` to decide how to interpret the user's
    # follow-up message (y/n parser vs raw text capture).
    # ------------------------------------------------------------------

    async def _park_for_input(
        self,
        task_id: str,
        kind: str,
        prompt_text: str,
        message_id_prefix: str = "input",
    ) -> dict[str, Any]:
        """Park the task in INPUT_REQUIRED with a synthetic agent message
        and wait for the user's reply. Returns the result dict populated
        by ``_resume_task``.
        """
        confirmation_event = asyncio.Event()
        confirmation_result: dict[str, Any] = {
            "kind": kind,
            "approved": False,
            "answer": "",
        }
        self._pending_confirmations[task_id] = (
            confirmation_event,
            confirmation_result,
        )

        msg = A2AMessage(
            role=A2ARole.AGENT,
            messageId=f"{message_id_prefix}-{uuid.uuid4().hex[:8]}",
            parts=[TextPart(text=prompt_text)],
        )
        await self._store.transition(
            task_id,
            A2ATaskState.INPUT_REQUIRED,
            message=msg,
        )

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

        await confirmation_event.wait()
        confirmation_event.clear()
        return confirmation_result

    def _make_on_confirm(
        self,
        task_id: str,
    ) -> Callable[[ToolCallInfo], Awaitable[bool]]:
        """on_confirm callback for tool-call gating (binary y/n)."""

        async def on_confirm(tool_call: ToolCallInfo) -> bool:
            result = await self._park_for_input(
                task_id,
                kind="confirm",
                prompt_text=f"Approve tool call: {tool_call.name}({tool_call.input})",
                message_id_prefix="confirm",
            )
            return bool(result.get("approved", False))

        return on_confirm

    def _make_ask_user(
        self,
        task_id: str,
    ) -> Callable[..., Awaitable[str]]:
        """ask_user callback for free-text questions (or choice menus)."""

        async def ask_user(
            question: str,
            choices: list[str] | None = None,
            allow_custom: bool = False,  # noqa: ARG001
        ) -> str:
            choices_str = ""
            if choices:
                choices_str = " Choices: " + ", ".join(choices)
            result = await self._park_for_input(
                task_id,
                kind="ask",
                prompt_text=f"Question: {question}{choices_str}",
                message_id_prefix="ask",
            )
            return str(result.get("answer", ""))

        return ask_user

    def _make_plan_approval(
        self,
        task_id: str,
    ) -> Callable[[str], Awaitable[bool]]:
        """plan_approval callback for plan-mode exit (binary y/n)."""

        async def plan_approval(plan_summary: str) -> bool:
            result = await self._park_for_input(
                task_id,
                kind="plan",
                prompt_text=(
                    "Approve plan and begin implementation:\n\n"
                    f"{plan_summary or '(no summary)'}"
                ),
                message_id_prefix="plan",
            )
            return bool(result.get("approved", False))

        return plan_approval

    async def _resume_task(self, task_id: str, message: A2AMessage) -> Task:
        """Resume a task that's waiting for input (INPUT_REQUIRED)."""
        pending = self._pending_confirmations.get(task_id)
        if not pending:
            # No pending confirmation — just append message
            return await self._store.append_message(task_id, message)

        confirmation_event, confirmation_result = pending

        kind = str(confirmation_result.get("kind", "confirm"))
        text = self._extract_text(message).strip()

        if kind == "ask":
            # Free-text answer — capture verbatim
            confirmation_result["answer"] = text
            confirmation_result["approved"] = bool(text)
        else:
            # Binary kinds (confirm / plan / permission): parse y/n
            lower = text.lower()
            approved = lower in (
                "yes",
                "true",
                "approve",
                "confirm",
                "ok",
                "y",
                "1",
            )
            confirmation_result["approved"] = approved
            confirmation_result["answer"] = text

        # Append the user's response to history
        await self._store.append_message(task_id, message)

        # Transition back to WORKING
        await self._store.transition(task_id, A2ATaskState.WORKING)

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
            await self._store.transition(task.id, A2ATaskState.WORKING)

            result_text = await self._execute_agent(task, prompt)

            # Add result as artifact
            if result_text:
                artifact = Artifact(
                    artifactId=f"art-{uuid.uuid4().hex[:8]}",
                    parts=[TextPart(text=result_text)],
                )
                await self._store.add_artifact(task.id, artifact)

            await self._store.transition(task.id, A2ATaskState.COMPLETED)

        except asyncio.CancelledError:
            logger.debug("suppressed exception in _run_agent_blocking", exc_info=True)
            with contextlib.suppress(Exception):
                await self._store.transition(task.id, A2ATaskState.CANCELED)
        except Exception as e:
            logger.exception("Agent execution failed for task %s: %s", task.id, e)
            with contextlib.suppress(Exception):
                await self._store.transition(task.id, A2ATaskState.FAILED)

    def _run_agent_background(self, task: Task) -> None:
        """Start agent execution as a background asyncio task."""
        async_task = asyncio.create_task(self._run_agent_blocking(task))
        self._running[task.id] = async_task

        def _cleanup(t: asyncio.Task[Any]) -> None:
            self._running.pop(task.id, None)

        async_task.add_done_callback(_cleanup)

    def _run_agent_background_with_push(self, task: Task, push_url: str) -> None:
        """Start agent execution in background and fire a push notification when done."""

        async def _run_and_push() -> None:
            await self._run_agent_blocking(task)
            refreshed = await self._store.get_task(task.id)
            final_task = refreshed or task
            await self._fire_push_notification(final_task, push_url)

        async_task = asyncio.create_task(_run_and_push())
        self._running[task.id] = async_task

        def _cleanup(t: asyncio.Task[Any]) -> None:
            self._running.pop(task.id, None)

        async_task.add_done_callback(_cleanup)

    async def _fire_push_notification(self, task: Task, url: str) -> None:
        """POST completed task to push notification URL (best-effort).

        Signs the request with ``X-Webhook-Signature: sha256=<hex>`` when
        ``OBSCURA_WEBHOOK_SECRET`` env var or
        ``~/.obscura/network-gateway-webhook.secret`` is configured.
        """
        import hmac as _hmac
        import json
        import os
        from pathlib import Path

        payload = task.model_dump(mode="json")
        try:
            data = json.dumps(payload).encode()

            # Resolve webhook secret (env var wins, then token file)
            secret = os.environ.get("OBSCURA_WEBHOOK_SECRET", "").strip()
            if not secret:
                _secret_file = Path.home() / ".obscura" / "network-gateway-webhook.secret"
                try:
                    for _line in _secret_file.read_text(encoding="utf-8").splitlines():
                        _line = _line.split("#", 1)[0].strip()
                        if _line:
                            secret = _line
                            break
                except OSError:
                    pass

            headers: dict[str, str] = {"Content-Type": "application/json"}
            if secret:
                sig = "sha256=" + _hmac.new(secret.encode(), data, "sha256").hexdigest()
                headers["X-Webhook-Signature"] = sig

            try:
                import httpx

                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(url, content=data, headers=headers)
            except ImportError:
                import urllib.request

                req = urllib.request.Request(url, data=data, method="POST")
                for k, v in headers.items():
                    req.add_header(k, v)
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: urllib.request.urlopen(req, timeout=10)
                )
            logger.info("Push notification sent to %s for task %s", url, task.id)
        except Exception:
            logger.warning(
                "Push notification failed to %s for task %s",
                url,
                task.id,
                exc_info=True,
            )

    async def _execute_agent(self, task: Task, prompt: str) -> str:
        """Execute the agent and return the final text result.

        Builds a per-task ``AgentSession`` via ``build_a2a_session`` so
        the agent has the full plugin tool set available, plus all
        three INPUT_REQUIRED-backed callbacks (on_confirm + ask_user +
        plan_approval). The session is torn down on return.
        """
        from obscura.composition.a2a import build_a2a_session
        from obscura.composition.session import SessionConfig

        config = SessionConfig(
            backend=self._agent_backend,
            model=self._agent_model,
            system_prompt=self._agent_system_prompt,
            max_turns=self._agent_max_turns,
        )
        on_confirm = self._make_on_confirm(task.id)
        ask_user = self._make_ask_user(task.id)
        plan_approval = self._make_plan_approval(task.id)

        async with await build_a2a_session(
            config,
            task_id=task.id,
            on_confirm=on_confirm,
            ask_user=ask_user,
            plan_approval=plan_approval,
        ) as session:
            result = await session.run_loop_to_text(
                prompt,
                max_turns=self._agent_max_turns,
                on_confirm=on_confirm,
            )
            return result or ""

    async def _execute_agent_stream(
        self,
        task: Task,
        prompt: str,
    ) -> AsyncIterator[AgentEvent]:
        """Execute the agent and yield AgentEvent objects."""
        from obscura.composition.a2a import build_a2a_session
        from obscura.composition.session import SessionConfig

        config = SessionConfig(
            backend=self._agent_backend,
            model=self._agent_model,
            system_prompt=self._agent_system_prompt,
            max_turns=self._agent_max_turns,
        )
        on_confirm = self._make_on_confirm(task.id)
        ask_user = self._make_ask_user(task.id)
        plan_approval = self._make_plan_approval(task.id)

        async with await build_a2a_session(
            config,
            task_id=task.id,
            on_confirm=on_confirm,
            ask_user=ask_user,
            plan_approval=plan_approval,
        ) as session:
            async for event in session.stream_loop(
                prompt,
                max_turns=self._agent_max_turns,
                on_confirm=on_confirm,
            ):
                yield event

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
