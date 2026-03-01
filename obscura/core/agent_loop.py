"""
obscura.agent_loop — Iterative agent loop with tool execution.

Drives the model in a loop: send prompt → stream response → detect tool
calls → execute tools → feed results back → repeat until the model
produces a final text response or *max_turns* is reached.

Works with all backends (Copilot, Claude, OpenAI, LocalLLM).

Usage::

    from obscura.core.agent_loop import AgentLoop
    from obscura.core.hooks import HookRegistry
    from obscura.core.event_store import SQLiteEventStore
    from obscura.core.types import AgentEventKind

    hooks = HookRegistry()
    store = SQLiteEventStore("/tmp/events.db")

    loop = AgentLoop(
        backend, tool_registry,
        hooks=hooks, event_store=store,
    )

    async for event in loop.run("Fix the auth bug", session_id="sess-1"):
        match event.kind:
            case AgentEventKind.TEXT_DELTA:
                print(event.text, end="")
            case AgentEventKind.TOOL_CALL:
                print(f"[tool] {event.tool_name}({event.tool_input})")
            case AgentEventKind.TOOL_RESULT:
                print(f"[result] {event.tool_result[:80]}")
            case AgentEventKind.AGENT_DONE:
                print("\\nDone!")
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, cast

from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    AgentEvent,
    AgentEventKind,
    BackendProtocol,
    ChunkKind,
    ContentBlock,
    Message,
    Role,
    StreamChunk,
    ToolCallContext,
    ToolCallEnvelope,
    ToolCallInfo,
    ToolErrorType,
    ToolExecutionError,
    ToolResultEnvelope,
    ToolSpec,
)

from obscura.core.event_store import EventRecord, EventStoreProtocol
from obscura.core.hooks import HookRegistry

logger = logging.getLogger(__name__)

# Parameter name aliases for cross-provider compatibility.
# Maps provider-specific parameter names to canonical tool parameter names.
PARAMETER_ALIASES: dict[str, dict[str, str]] = {
    "write_text_file": {
        "content": "text",  # Copilot/OpenAI uses 'content', we use 'text'
        "file_path": "path",
        "filepath": "path",
    },
    "read_text_file": {
        "file_path": "path",
        "filepath": "path",
    },
    "append_text_file": {
        "content": "text",
        "file_path": "path",
        "filepath": "path",
    },
}


# Type alias for confirmation callbacks.
# Receives a ToolCallInfo, returns True (approve) or False (deny).
ConfirmationCallback = Callable[[ToolCallInfo], Awaitable[bool] | bool]


class AgentLoop:
    """Iterative agent loop that drives tool-calling across multiple turns.

    The loop sends the initial prompt, collects the model response (streamed),
    detects any tool calls in the response, executes the tools locally, feeds
    the results back to the model as a new turn, and repeats.

    Parameters
    ----------
    backend:
        A started backend instance (CopilotBackend or ClaudeBackend).
    tool_registry:
        Registry of available tools. Tool handlers are called during the loop.
    max_turns:
        Maximum number of model turns before the loop stops.
    on_confirm:
        Optional async/sync callback invoked before each tool execution.
        Return ``True`` to approve, ``False`` to deny (tool result will
        be "Tool call denied by user").
    hooks:
        Optional :class:`HookRegistry`.  Before/after hooks fire on every
        event the loop emits.
    event_store:
        Optional :class:`EventStoreProtocol`.  When provided, every emitted
        event is persisted to durable storage.
    agent_name:
        Identifier for the agent (stored in the session record).
    """

    def __init__(
        self,
        backend: BackendProtocol | None,
        tool_registry: ToolRegistry,
        *,
        max_turns: int = 10,
        on_confirm: ConfirmationCallback | None = None,
        capability_token: Any | None = None,
        hooks: HookRegistry | None = None,
        event_store: EventStoreProtocol | None = None,
        agent_name: str = "agent_loop",
        tool_allowlist: list[str] | None = None,
        auto_complete: bool = True,
        backend_name: str = "",
        model_name: str = "",
    ) -> None:
        self._backend = backend
        self._tools = tool_registry
        self._max_turns = max_turns
        self._on_confirm = on_confirm
        self._capability_token = capability_token
        self._hooks = hooks
        self._event_store = event_store
        self._agent_name = agent_name
        self._tool_allowlist = tool_allowlist
        self._auto_complete = auto_complete
        self._backend_name = backend_name
        self._model_name = model_name

        # Pause / mid-run input state
        self._should_pause = False
        self._user_input_queue: asyncio.Queue[str] = asyncio.Queue()

    # ------------------------------------------------------------------
    # Pause / mid-run input public API
    # ------------------------------------------------------------------

    def request_pause(self) -> None:
        """Request the loop to pause at the next turn boundary.

        The current turn will complete normally.  After emitting
        TURN_COMPLETE the loop emits SESSION_PAUSED and returns.
        Thread-safe.
        """
        self._should_pause = True

    def inject_user_input(self, text: str) -> None:
        """Queue a user message to inject at the next turn boundary.

        The text becomes the prompt for the next model turn and a
        USER_INPUT event is emitted.  Thread-safe.
        """
        self._user_input_queue.put_nowait(text)

    @property
    def max_turns(self) -> int:
        """Read-only max_turns (testing/observability)."""
        return self._max_turns

    # ------------------------------------------------------------------
    # Event emission (hooks + persistence)
    # ------------------------------------------------------------------

    async def _emit(
        self,
        event: AgentEvent,
        session_id: str | None,
    ) -> AgentEvent | None:
        """Run before-hooks → persist → return event (or None if suppressed).

        After-hooks run separately via :meth:`_post_emit`.
        """
        current = event

        # Before-hooks
        if self._hooks is not None:
            result = await self._hooks.run_before(current)
            if result is None:
                return None
            current = result

        # Persist
        if self._event_store is not None and session_id is not None:
            await self._event_store.append(session_id, current)

        return current

    async def _post_emit(self, event: AgentEvent) -> None:
        """Run after-hooks for an already-yielded event."""
        if self._hooks is not None:
            await self._hooks.run_after(event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        initial_messages: list[Message] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent loop, yielding events as they occur.

        Parameters
        ----------
        prompt:
            The initial user prompt.
        session_id:
            Optional durable session ID.  When provided (and an event store
            is configured), events are persisted and the session can be
            resumed later.  If ``None``, a transient ID is generated and
            nothing is persisted.

        Yields
        ------
        AgentEvent
            TURN_START, TEXT_DELTA, THINKING_DELTA, TOOL_CALL, TOOL_RESULT,
            TURN_COMPLETE, and finally AGENT_DONE (or ERROR).
        """
        # Reset pause state for this run
        self._should_pause = False

        # Create durable session if store is wired
        sid = session_id
        if self._event_store is not None and sid is not None:
            existing = await self._event_store.get_session(sid)
            if existing is None:
                await self._event_store.create_session(
                    sid,
                    self._agent_name,
                    backend=self._backend_name,
                    model=self._model_name,
                )

        async for event in self._run_inner(prompt, sid, 0, "", kwargs, initial_messages):
            yield event

    async def resume(
        self,
        session_id: str,
        *,
        prompt: str = "",
        **kwargs: Any,
    ) -> AsyncIterator[AgentEvent]:
        """Resume a paused session from the event store.

        Parameters
        ----------
        session_id:
            The session to resume.  Must exist and have status PAUSED.
        prompt:
            Optional override prompt.  If empty, the reconstructed prompt
            from the last run is used.

        Raises
        ------
        RuntimeError
            If no event store is configured.
        ValueError
            If the session does not exist or is not paused.
        """
        if self._event_store is None:
            raise RuntimeError("Cannot resume without an event store")

        session = await self._event_store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id!r} not found")

        from obscura.core.event_store import SessionStatus

        if session.status != SessionStatus.PAUSED:
            raise ValueError(
                f"Session {session_id!r} is {session.status.value}, not paused"
            )

        # Reconstruct state from persisted events
        events = await self._event_store.get_events(session_id)
        turn, acc_text, messages, last_prompt = AgentLoop.reconstruct_state(events)

        # Transition back to RUNNING
        await self._event_store.update_status(session_id, SessionStatus.RUNNING)

        # Reset pause flag
        self._should_pause = False

        resume_prompt = prompt if prompt else last_prompt
        resume_kwargs: dict[str, Any] = dict(kwargs)
        if messages:
            resume_kwargs["messages"] = messages

        async for event in self._run_inner(
            resume_prompt, session_id, turn, acc_text, resume_kwargs,
        ):
            yield event

    # ------------------------------------------------------------------
    # Inner loop (shared by run + resume)
    # ------------------------------------------------------------------

    async def _call_stream(
        self,
        prompt: str,
        kwargs: dict[str, Any],
    ) -> AsyncIterator[StreamChunk]:
        """Call backend.stream with kwargs, isolating type-unsafe spread."""
        if self._backend is None:
            raise RuntimeError("No backend configured")
        async for chunk in self._backend.stream(prompt, **kwargs):
            yield chunk

    async def _run_inner(
        self,
        prompt: str,
        session_id: str | None,
        start_turn: int,
        accumulated_text: str,
        stream_kwargs: dict[str, Any],
        initial_messages: list | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Core loop body shared by :meth:`run` and :meth:`resume`."""
        turn: int = start_turn
        current_prompt: str = prompt
        kwargs: dict[str, Any] = stream_kwargs
        _prev_event: AgentEvent | None = None

        while turn < self._max_turns:
            turn += 1

            # Run post-hook for the previous event before emitting next
            if _prev_event is not None:
                await self._post_emit(_prev_event)
                _prev_event = None

            turn_start = AgentEvent(kind=AgentEventKind.TURN_START, turn=turn)
            emitted = await self._emit(turn_start, session_id)
            if emitted is not None:
                yield emitted
                _prev_event = emitted

            tool_calls: list[ToolCallInfo] = []
            turn_text: str = ""
            _current_tool_name: str = ""
            _current_tool_input_json: str = ""
            _current_tool_raw: Any = None
            _seen_tool_use: bool = False

            try:
                async for chunk in self._call_stream(current_prompt, kwargs):
                    # Suppress text generated after a tool_use block in the
                    # same turn — models often hallucinate tool outcomes
                    # (e.g. "permission denied") before seeing the real result.
                    if chunk.kind == ChunkKind.TEXT_DELTA and _seen_tool_use:
                        continue

                    event = self._map_chunk(chunk, turn)
                    if event is not None:
                        # Run post-hook for previous, then emit new
                        if _prev_event is not None:
                            await self._post_emit(_prev_event)
                            _prev_event = None
                        emitted = await self._emit(event, session_id)
                        if emitted is not None:
                            yield emitted
                            _prev_event = emitted

                    # Accumulate text
                    if chunk.kind == ChunkKind.TEXT_DELTA:
                        turn_text += chunk.text

                    # Collect tool calls
                    if chunk.kind == ChunkKind.TOOL_USE_START:
                        _seen_tool_use = True
                        # Flush previous tool if any (fallback for backends
                        # that don't emit TOOL_USE_END)
                        if _current_tool_name:
                            tc = self._parse_tool_call(
                                _current_tool_name,
                                _current_tool_input_json,
                                _current_tool_raw,
                            )
                            tool_calls.append(tc)
                            # Emit TOOL_CALL with full input
                            tc_ev = AgentEvent(
                                kind=AgentEventKind.TOOL_CALL,
                                tool_name=tc.name,
                                tool_input=tc.input,
                                turn=turn,
                                raw=tc.raw,
                            )
                            if _prev_event is not None:
                                await self._post_emit(_prev_event)
                                _prev_event = None
                            emitted = await self._emit(tc_ev, session_id)
                            if emitted is not None:
                                yield emitted
                                _prev_event = emitted
                        _current_tool_name = chunk.tool_name
                        _current_tool_input_json = ""
                        _current_tool_raw = chunk.raw

                    if chunk.kind == ChunkKind.TOOL_USE_DELTA:
                        _current_tool_input_json += chunk.tool_input_delta

                    # TOOL_USE_END — flush accumulated tool immediately
                    if chunk.kind == ChunkKind.TOOL_USE_END:
                        if _current_tool_name:
                            tc = self._parse_tool_call(
                                _current_tool_name,
                                _current_tool_input_json,
                                _current_tool_raw,
                            )
                            tool_calls.append(tc)
                            # Emit TOOL_CALL with full input
                            tc_ev = AgentEvent(
                                kind=AgentEventKind.TOOL_CALL,
                                tool_name=tc.name,
                                tool_input=tc.input,
                                turn=turn,
                                raw=tc.raw,
                            )
                            if _prev_event is not None:
                                await self._post_emit(_prev_event)
                                _prev_event = None
                            emitted = await self._emit(tc_ev, session_id)
                            if emitted is not None:
                                yield emitted
                                _prev_event = emitted
                            _current_tool_name = ""
                            _current_tool_input_json = ""
                            _current_tool_raw = None

                # Flush last tool call (fallback if no TOOL_USE_END received)
                if _current_tool_name:
                    tc = self._parse_tool_call(
                        _current_tool_name,
                        _current_tool_input_json,
                        _current_tool_raw,
                    )
                    tool_calls.append(tc)
                    # Emit TOOL_CALL with full input
                    tc_ev = AgentEvent(
                        kind=AgentEventKind.TOOL_CALL,
                        tool_name=tc.name,
                        tool_input=tc.input,
                        turn=turn,
                        raw=tc.raw,
                    )
                    if _prev_event is not None:
                        await self._post_emit(_prev_event)
                        _prev_event = None
                    emitted = await self._emit(tc_ev, session_id)
                    if emitted is not None:
                        yield emitted
                        _prev_event = emitted

            except Exception as exc:
                err_event = AgentEvent(
                    kind=AgentEventKind.ERROR,
                    text=str(exc),
                    turn=turn,
                    raw=exc,
                )
                if _prev_event is not None:
                    await self._post_emit(_prev_event)
                emitted = await self._emit(err_event, session_id)
                if emitted is not None:
                    yield emitted
                    await self._post_emit(emitted)
                # Mark session failed
                if self._auto_complete and self._event_store is not None and session_id is not None:
                    from obscura.core.event_store import SessionStatus

                    try:
                        await self._event_store.update_status(
                            session_id, SessionStatus.FAILED
                        )
                    except Exception:
                        pass
                return

            accumulated_text += turn_text

            # Emit TURN_COMPLETE
            if _prev_event is not None:
                await self._post_emit(_prev_event)
                _prev_event = None
            tc_event = AgentEvent(
                kind=AgentEventKind.TURN_COMPLETE, turn=turn, text=turn_text
            )
            emitted = await self._emit(tc_event, session_id)
            if emitted is not None:
                yield emitted
                _prev_event = emitted

            # ----------------------------------------------------------
            # Turn boundary: check pause / user input before next turn
            # ----------------------------------------------------------

            # Pause check — current turn completed, pause before next
            if self._should_pause:
                if _prev_event is not None:
                    await self._post_emit(_prev_event)
                    _prev_event = None
                pause_event = AgentEvent(
                    kind=AgentEventKind.SESSION_PAUSED,
                    turn=turn,
                    text="Session paused at turn boundary",
                )
                emitted = await self._emit(pause_event, session_id)
                if emitted is not None:
                    yield emitted
                    await self._post_emit(emitted)
                # Mark session paused
                if self._event_store is not None and session_id is not None:
                    from obscura.core.event_store import SessionStatus

                    try:
                        await self._event_store.update_status(
                            session_id, SessionStatus.PAUSED
                        )
                    except Exception:
                        pass
                return

            # Mid-run user input — drain queue, use as next prompt
            if not self._user_input_queue.empty():
                if _prev_event is not None:
                    await self._post_emit(_prev_event)
                    _prev_event = None
                user_text = self._user_input_queue.get_nowait()
                ui_event = AgentEvent(
                    kind=AgentEventKind.USER_INPUT,
                    turn=turn,
                    text=user_text,
                )
                emitted = await self._emit(ui_event, session_id)
                if emitted is not None:
                    yield emitted
                    _prev_event = emitted
                # Override the next prompt with the user's input
                current_prompt = user_text
                # Skip tool execution for this turn — go straight to
                # next model call with the injected prompt.
                # Remove "messages" from kwargs so the backend doesn't
                # replay stale tool results.
                kwargs.pop("messages", None)
                continue

            # No tool calls → model is done
            if not tool_calls:
                if _prev_event is not None:
                    await self._post_emit(_prev_event)
                    _prev_event = None
                done_event = AgentEvent(
                    kind=AgentEventKind.AGENT_DONE,
                    turn=turn,
                    text=accumulated_text,
                )
                emitted = await self._emit(done_event, session_id)
                if emitted is not None:
                    yield emitted
                    await self._post_emit(emitted)
                # Mark session completed
                if self._auto_complete and self._event_store is not None and session_id is not None:
                    from obscura.core.event_store import SessionStatus

                    try:
                        await self._event_store.update_status(
                            session_id, SessionStatus.COMPLETED
                        )
                    except Exception:
                        pass
                return

            # Execute tool calls and build results for next turn
            tool_results = await self._execute_tools(tool_calls, turn)

            # Yield tool result events
            for result in tool_results:
                if _prev_event is not None:
                    await self._post_emit(_prev_event)
                    _prev_event = None
                tr_event = AgentEvent(
                    kind=AgentEventKind.TOOL_RESULT,
                    tool_name=result.tool,
                    tool_use_id=result.tool_use_id,
                    tool_result=self._render_tool_result_text(result),
                    is_error=result.status == "error",
                    turn=turn,
                    raw=result,
                )
                emitted = await self._emit(tr_event, session_id)
                if emitted is not None:
                    yield emitted
                    _prev_event = emitted

            # Build structured messages for backends that support it,
            # with plain-text fallback as the prompt.
            structured = self._build_structured_tool_messages(
                tool_calls,
                tool_results,
                turn_text,
            )
            current_prompt = self._format_tool_results_envelopes(tool_results)

            # Pass structured messages via kwargs so backends can
            # persist full tool call/result history.  Merge rather
            # than replace so callers' kwargs (e.g. tool_choice) survive.
            kwargs = {**kwargs, "messages": structured}

        # Hit max turns
        if _prev_event is not None:
            await self._post_emit(_prev_event)
        done_event = AgentEvent(
            kind=AgentEventKind.AGENT_DONE,
            turn=turn,
            text=accumulated_text,
        )
        emitted = await self._emit(done_event, session_id)
        if emitted is not None:
            yield emitted
            await self._post_emit(emitted)
        # Mark session completed
        if self._auto_complete and self._event_store is not None and session_id is not None:
            from obscura.core.event_store import SessionStatus

            try:
                await self._event_store.update_status(session_id, SessionStatus.COMPLETED)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Convenience: run and collect final text
    # ------------------------------------------------------------------

    async def run_to_completion(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Run the loop and return the concatenated text output."""
        text_parts: list[str] = []
        async for event in self.run(prompt, session_id=session_id, **kwargs):
            if event.kind == AgentEventKind.TEXT_DELTA:
                text_parts.append(event.text)
        return "".join(text_parts)

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tools(
        self,
        tool_calls: list[ToolCallInfo],
        turn: int,
    ) -> list[ToolResultEnvelope]:
        """Execute tool calls and return canonical result envelopes."""
        del turn
        results: list[ToolResultEnvelope] = []

        for tc in tool_calls:
            call = ToolCallEnvelope(
                call_id=tc.tool_use_id,
                agent_id="agent_loop",
                tool=tc.name,
                args=tc.input,
                context=ToolCallContext(trace_id=uuid.uuid4().hex, policy="default"),
            )
            started = time.monotonic()

            # Tool allowlist enforcement
            if self._tool_allowlist is not None and tc.name not in self._tool_allowlist:
                err = ToolExecutionError(
                    type=ToolErrorType.UNAUTHORIZED,
                    message=f"Tool '{tc.name}' not in allowlist.",
                    safe_to_retry=False,
                )
                results.append(
                    ToolResultEnvelope(
                        call_id=call.call_id,
                        tool=call.tool,
                        status="error",
                        error=err,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        tool_use_id=tc.tool_use_id,
                        raw=tc.raw,
                    )
                )
                continue

            # Confirmation gate
            if self._on_confirm is not None:
                approved = self._on_confirm(tc)
                if asyncio.iscoroutine(approved) or asyncio.isfuture(approved):
                    approved = await approved
                if not approved:
                    err = ToolExecutionError(
                        type=ToolErrorType.UNAUTHORIZED,
                        message="Tool call denied by user.",
                        safe_to_retry=False,
                    )
                    results.append(
                        ToolResultEnvelope(
                            call_id=call.call_id,
                            tool=call.tool,
                            status="error",
                            error=err,
                            latency_ms=int((time.monotonic() - started) * 1000),
                            tool_use_id=tc.tool_use_id,
                            raw=tc.raw,
                        )
                    )
                    continue

            spec = self._tools.get(tc.name)
            if spec is None:
                err = ToolExecutionError(
                    type=ToolErrorType.NOT_FOUND,
                    message=f"Unknown tool: {tc.name}. Available: {', '.join(self._tools.names())}",
                    safe_to_retry=False,
                )
                results.append(
                    ToolResultEnvelope(
                        call_id=call.call_id,
                        tool=call.tool,
                        status="error",
                        error=err,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        tool_use_id=tc.tool_use_id,
                        raw=tc.raw,
                    )
                )
                continue

            # Capability token enforcement (defense in depth)
            if self._capability_token is not None:
                try:
                    from obscura.auth.capability import validate_capability_token

                    if not validate_capability_token(self._capability_token):
                        _audit_tool_denied(tc.name, "invalid_or_expired_token")
                        err = ToolExecutionError(
                            type=ToolErrorType.UNAUTHORIZED,
                            message="Capability token invalid or expired.",
                            safe_to_retry=False,
                        )
                        results.append(
                            ToolResultEnvelope(
                                call_id=call.call_id,
                                tool=call.tool,
                                status="error",
                                error=err,
                                latency_ms=int((time.monotonic() - started) * 1000),
                                tool_use_id=tc.tool_use_id,
                                raw=tc.raw,
                            )
                        )
                        continue

                    # TODO: enforce tier restriction once tier differentiation is enabled
                    # token_tier = self._capability_token.tier.value
                    # if (
                    #     spec.required_tier == "privileged"
                    #     and token_tier != "privileged"
                    # ):
                    #     _audit_tool_denied(tc.name, "insufficient_tier")
                    #     results.append(
                    #         (
                    #             tc,
                    #             f"Tool '{tc.name}' requires privileged tier.",
                    #             True,
                    #         )
                    #     )
                    #     continue
                except Exception:
                    pass  # Degrade gracefully if capability module unavailable

            try:
                result = await self._call_handler(spec, tc.input)
                results.append(
                    ToolResultEnvelope(
                        call_id=call.call_id,
                        tool=call.tool,
                        status="ok",
                        result=result,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        tool_use_id=tc.tool_use_id,
                        raw=tc.raw,
                    )
                )
            except Exception as exc:
                logger.warning("Tool %s failed: %s", tc.name, exc)
                err = self._normalize_tool_error(exc)
                results.append(
                    ToolResultEnvelope(
                        call_id=call.call_id,
                        tool=call.tool,
                        status="error",
                        error=err,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        tool_use_id=tc.tool_use_id,
                        raw=tc.raw,
                    )
                )

        return results

    @staticmethod
    async def _call_handler(spec: ToolSpec, inputs: dict[str, Any]) -> Any:
        """Call a tool handler (sync or async).

        Automatically filters out kwargs the handler doesn't declare — e.g. a
        ``prompt`` param that Claude passes to ``web_fetch`` but other backends
        don't.  This gives cross-agent parity without requiring every tool to
        enumerate every LLM-convention parameter.
        """
        handler = spec.handler
        
        # Normalize parameter names based on known aliases
        if spec.name in PARAMETER_ALIASES:
            aliases = PARAMETER_ALIASES[spec.name]
            for alias, canonical in aliases.items():
                if alias in inputs and canonical not in inputs:
                    inputs[canonical] = inputs.pop(alias)
        
        try:
            if inspect.iscoroutinefunction(handler):
                return await handler(**inputs)
            return handler(**inputs)
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            # If the handler uses **kwargs it accepts everything — a TypeError
            # here is a genuine bug, not a cross-agent compat issue.
            sig = inspect.signature(handler)
            if any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            ):
                raise
            accepted = {
                n
                for n, p in sig.parameters.items()
                if p.kind
                not in (
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                )
            }
            filtered = {k: v for k, v in inputs.items() if k in accepted}
            dropped = sorted(set(inputs) - accepted)
            logger.debug("Tool %s: dropping undeclared kwargs %s", spec.name, dropped)
            if inspect.iscoroutinefunction(handler):
                return await handler(**filtered)
            return handler(**filtered)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_tool_error(exc: Exception) -> ToolExecutionError:
        msg = str(exc)
        lower = msg.lower()

        if isinstance(exc, (TypeError, ValueError)):
            if (
                "required positional argument" in lower
                or "missing" in lower
                or "unexpected keyword argument" in lower
            ):
                return ToolExecutionError(
                    type=ToolErrorType.INVALID_ARGS,
                    message=msg,
                    safe_to_retry=False,
                )
        if isinstance(exc, PermissionError):
            return ToolExecutionError(
                type=ToolErrorType.UNAUTHORIZED,
                message=msg,
                safe_to_retry=False,
            )
        if isinstance(exc, FileNotFoundError):
            return ToolExecutionError(
                type=ToolErrorType.NOT_FOUND,
                message=msg,
                safe_to_retry=False,
            )
        if isinstance(exc, FileExistsError):
            return ToolExecutionError(
                type=ToolErrorType.CONFLICT,
                message=msg,
                safe_to_retry=False,
            )
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return ToolExecutionError(
                type=ToolErrorType.TIMEOUT,
                message=msg,
                safe_to_retry=True,
            )
        if isinstance(exc, ConnectionError):
            return ToolExecutionError(
                type=ToolErrorType.DEPENDENCY_ERROR,
                message=msg,
                safe_to_retry=True,
            )
        if "rate limit" in lower or "429" in lower:
            return ToolExecutionError(
                type=ToolErrorType.RATE_LIMITED,
                message=msg,
                safe_to_retry=True,
            )
        return ToolExecutionError(
            type=ToolErrorType.UNKNOWN,
            message=msg,
            safe_to_retry=False,
        )

    @staticmethod
    def _render_tool_result_text(result: ToolResultEnvelope) -> str:
        # Hard cap: truncate any single tool result to prevent context blowout
        _MAX_RESULT_CHARS = 8000

        def _encode(obj: Any) -> str:
            """Encode as TOON (~40% fewer tokens), fall back to JSON."""
            try:
                import toons
                return toons.dumps(obj)
            except Exception:
                return json.dumps(obj, default=str)

        def _cap(text: str) -> str:
            if len(text) <= _MAX_RESULT_CHARS:
                return text
            return (
                text[:_MAX_RESULT_CHARS]
                + f"\n... [truncated, {len(text):,} chars total]"
            )

        if result.status == "ok":
            if isinstance(result.result, str):
                return _cap(result.result)
            return _cap(_encode(result.result))
        if result.error is None:
            return "Tool error"
        payload = {
            "type": result.error.type.value,
            "message": result.error.message,
            "retry_after_ms": result.error.retry_after_ms,
            "safe_to_retry": result.error.safe_to_retry,
        }
        return _cap(_encode(payload))

    @staticmethod
    def _map_chunk(chunk: StreamChunk, turn: int) -> AgentEvent | None:
        """Map a StreamChunk to an AgentEvent, or None to skip."""
        if chunk.kind == ChunkKind.TEXT_DELTA:
            return AgentEvent(
                kind=AgentEventKind.TEXT_DELTA,
                text=chunk.text,
                turn=turn,
                raw=chunk.raw,
            )
        if chunk.kind == ChunkKind.THINKING_DELTA:
            return AgentEvent(
                kind=AgentEventKind.THINKING_DELTA,
                text=chunk.text,
                turn=turn,
                raw=chunk.raw,
            )
        # Note: TOOL_CALL events are emitted *after* the full tool call
        # is parsed (in the main loop), not here at TOOL_USE_START.
        # This ensures the persisted TOOL_CALL includes tool_input.
        if chunk.kind == ChunkKind.ERROR:
            return AgentEvent(
                kind=AgentEventKind.ERROR,
                text=chunk.text,
                turn=turn,
                raw=chunk.raw,
            )
        return None

    @staticmethod
    def _parse_tool_call(
        name: str,
        input_json: str,
        raw: Any,
    ) -> ToolCallInfo:
        """Parse accumulated tool call data into a ToolCallInfo."""
        raw_input = AgentLoop._extract_tool_input_from_raw(raw)
        parsed_input: dict[str, Any] = {}
        delta_valid = False

        if input_json.strip():
            try:
                decoded = json.loads(input_json)
                if isinstance(decoded, dict):
                    parsed_input = cast(dict[str, Any], decoded)
                    delta_valid = True
                else:
                    parsed_input = {"_raw_input": input_json}
            except json.JSONDecodeError:
                parsed_input = {"_raw_input": input_json}

        if delta_valid:
            # Streamed JSON deltas are authoritative when present.
            parsed_input = {**raw_input, **parsed_input}
        elif not parsed_input:
            parsed_input = raw_input
        elif raw_input and parsed_input.keys() == {"_raw_input"}:
            # If delta payload is invalid, prefer provider-native structured args.
            parsed_input = raw_input

        return ToolCallInfo(
            tool_use_id=f"tool_{uuid.uuid4().hex[:12]}",
            name=name,
            input=parsed_input,
            raw=raw,
        )

    @staticmethod
    def _extract_tool_input_from_raw(raw: Any) -> dict[str, Any]:
        """Best-effort extraction of tool args from provider-native raw events."""

        def _field(obj: Any, key: str) -> Any:
            if obj is None:
                return None
            if isinstance(obj, dict):
                return cast(dict[str, Any], obj).get(key)
            return getattr(obj, key, None)

        def _coerce_to_dict(value: Any) -> dict[str, Any]:
            if value is None:
                return {}
            if isinstance(value, dict):
                return cast(dict[str, Any], value)
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return {}
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    return {"_raw_input": value}
                if isinstance(decoded, dict):
                    return cast(dict[str, Any], decoded)
                return {"_raw_input": value}
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                dumped = model_dump()
                if isinstance(dumped, dict):
                    return cast(dict[str, Any], dumped)
                return {}
            to_dict = getattr(value, "to_dict", None)
            if callable(to_dict):
                dumped = to_dict()
                if isinstance(dumped, dict):
                    return cast(dict[str, Any], dumped)
                return {}
            as_dict = getattr(value, "__dict__", None)
            if isinstance(as_dict, dict):
                object_dict = cast(dict[str, Any], as_dict)
                return {k: v for k, v in object_dict.items() if not k.startswith("_")}
            return {}

        data = _field(raw, "data")
        if data is None:
            data = raw

        direct_keys = ("tool_input", "input", "arguments", "parameters")
        for key in direct_keys:
            payload = _field(data, key)
            parsed = _coerce_to_dict(payload)
            if parsed:
                return parsed

        nested_tool = _field(data, "tool_call")
        if nested_tool is not None:
            for key in direct_keys:
                payload = _field(nested_tool, key)
                parsed = _coerce_to_dict(payload)
                if parsed:
                    return parsed

        return {}

    @staticmethod
    def _build_structured_tool_messages(
        tool_calls: list[ToolCallInfo],
        tool_results: list[ToolResultEnvelope],
        turn_text: str,
    ) -> list[Message]:
        """Build structured Message objects for tool call/result turns.

        Returns two messages:
        1. Assistant message with text (if any) + tool_use blocks
        2. Tool result message with tool_result blocks

        These are passed to ``backend.stream(prompt, messages=...)``
        so backends can persist the full structured conversation.
        """
        # 1) Assistant message: any text + tool_use blocks
        assistant_blocks: list[ContentBlock] = []
        if turn_text:
            assistant_blocks.append(ContentBlock(kind="text", text=turn_text))
        for tc in tool_calls:
            assistant_blocks.append(
                ContentBlock(
                    kind="tool_use",
                    tool_name=tc.name,
                    tool_input=tc.input,
                    tool_use_id=tc.tool_use_id,
                )
            )
        if not assistant_blocks:
            assistant_blocks.append(ContentBlock(kind="text", text=""))

        assistant_msg = Message(
            role=Role.ASSISTANT,
            content=assistant_blocks,
        )

        # 2) Tool result message
        result_blocks: list[ContentBlock] = []
        result_by_id = {r.tool_use_id: r for r in tool_results}
        for tc in tool_calls:
            result = result_by_id.get(tc.tool_use_id)
            if result is None:
                continue
            result_blocks.append(
                ContentBlock(
                    kind="tool_result",
                    text=AgentLoop._render_tool_result_text(result),
                    tool_use_id=tc.tool_use_id,
                    is_error=result.status == "error",
                )
            )

        result_msg = Message(
            role=Role.TOOL_RESULT,
            content=result_blocks,
        )

        return [assistant_msg, result_msg]

    @staticmethod
    def _format_tool_results(
        results: list[tuple[ToolCallInfo, str, bool]],
    ) -> str:
        """Format tool results as a prompt for the next model turn."""
        envelopes: list[ToolResultEnvelope] = []
        for tc, result_text, is_error in results:
            envelopes.append(
                ToolResultEnvelope(
                    call_id=tc.tool_use_id,
                    tool=tc.name,
                    status="error" if is_error else "ok",
                    result=None if is_error else result_text,
                    error=(
                        ToolExecutionError(
                            type=ToolErrorType.UNKNOWN,
                            message=result_text,
                            safe_to_retry=False,
                        )
                        if is_error
                        else None
                    ),
                    tool_use_id=tc.tool_use_id,
                    raw=tc.raw,
                )
            )
        return AgentLoop._format_tool_results_envelopes(envelopes)

    @staticmethod
    def _format_tool_results_envelopes(results: list[ToolResultEnvelope]) -> str:
        """Format canonical tool result envelopes as prompt text."""
        parts: list[str] = []
        for result in results:
            status = "ERROR" if result.status == "error" else "OK"
            result_text = AgentLoop._render_tool_result_text(result)
            parts.append(
                f"[Tool Result: {result.tool} (id={result.tool_use_id}) status={status}]\n"
                f"{result_text}\n"
                f"[/Tool Result]"
            )
        return "\n\n".join(parts)

    # Public test/observability wrappers ---------------------------------
    @staticmethod
    def parse_tool_call(name: str, input_json: str, raw: Any) -> ToolCallInfo:
        """Public wrapper to parse a tool call (testing)."""
        return AgentLoop._parse_tool_call(name, input_json, raw)

    @staticmethod
    def format_tool_results(results: list[tuple[ToolCallInfo, str, bool]]) -> str:
        """Public wrapper to format tool results (testing)."""
        return AgentLoop._format_tool_results(results)

    @staticmethod
    def reconstruct_state(
        events: list[EventRecord],
    ) -> tuple[int, str, list[Message], str]:
        """Reconstruct loop state from persisted events for resume.

        Returns
        -------
        (turn, accumulated_text, messages, last_prompt)
            - turn: last completed turn number
            - accumulated_text: all text deltas concatenated
            - messages: structured tool call/result Message pairs
            - last_prompt: the most recent prompt text
        """
        turn = 0
        accumulated_text = ""
        last_prompt = ""
        messages: list[Message] = []

        # Group tool calls and results per turn for structured message rebuild
        current_turn_tool_calls: list[ToolCallInfo] = []
        current_turn_tool_results: list[ToolResultEnvelope] = []
        current_turn_text = ""
        current_turn = 0

        for rec in events:
            kind_str = rec.payload.get("kind", "")
            event_turn = int(rec.payload.get("turn", 0))

            # When we enter a new turn, flush any accumulated tool pairs
            if event_turn > current_turn and current_turn > 0:
                if current_turn_tool_calls and current_turn_tool_results:
                    pair = AgentLoop._build_structured_tool_messages(
                        current_turn_tool_calls,
                        current_turn_tool_results,
                        current_turn_text,
                    )
                    messages.extend(pair)
                    last_prompt = AgentLoop._format_tool_results_envelopes(
                        current_turn_tool_results
                    )
                current_turn_tool_calls = []
                current_turn_tool_results = []
                current_turn_text = ""
                current_turn = event_turn

            if kind_str == AgentEventKind.TURN_COMPLETE.value:
                turn = event_turn

            elif kind_str == AgentEventKind.TEXT_DELTA.value:
                text = str(rec.payload.get("text", ""))
                accumulated_text += text
                current_turn_text += text

            elif kind_str == AgentEventKind.TOOL_CALL.value:
                raw_input = rec.payload.get("tool_input")
                tool_input: dict[str, Any] = (
                    cast(dict[str, Any], raw_input)
                    if isinstance(raw_input, dict)
                    else {}
                )
                tc = ToolCallInfo(
                    tool_use_id=str(rec.payload.get("tool_use_id", "")),
                    name=str(rec.payload.get("tool_name", "")),
                    input=tool_input,
                )
                current_turn_tool_calls.append(tc)

            elif kind_str == AgentEventKind.TOOL_RESULT.value:
                result_text = str(rec.payload.get("tool_result", ""))
                is_error = bool(rec.payload.get("is_error", False))
                tr = ToolResultEnvelope(
                    call_id=str(rec.payload.get("tool_use_id", "")),
                    tool=str(rec.payload.get("tool_name", "")),
                    status="error" if is_error else "ok",
                    result=None if is_error else result_text,
                    error=(
                        ToolExecutionError(
                            type=ToolErrorType.UNKNOWN,
                            message=result_text,
                            safe_to_retry=False,
                        )
                        if is_error
                        else None
                    ),
                    tool_use_id=str(rec.payload.get("tool_use_id", "")),
                )
                current_turn_tool_results.append(tr)

            elif kind_str == AgentEventKind.USER_INPUT.value:
                last_prompt = str(rec.payload.get("text", ""))

            # Track current turn
            if event_turn > 0:
                current_turn = event_turn

        # Flush final turn's tool pairs
        if current_turn_tool_calls and current_turn_tool_results:
            pair = AgentLoop._build_structured_tool_messages(
                current_turn_tool_calls,
                current_turn_tool_results,
                current_turn_text,
            )
            messages.extend(pair)
            last_prompt = AgentLoop._format_tool_results_envelopes(
                current_turn_tool_results
            )

        return turn, accumulated_text, messages, last_prompt


# ---------------------------------------------------------------------------
# Audit helper for capability enforcement
# ---------------------------------------------------------------------------


def _audit_tool_denied(tool_name: str, reason: str) -> None:
    """Emit an audit event when a tool call is denied by capability enforcement."""
    try:
        from obscura.telemetry.audit import AuditEvent, emit_audit_event

        emit_audit_event(
            AuditEvent(
                event_type="tool.denied",
                user_id="agent_loop",
                user_email="",
                resource=f"tool:{tool_name}",
                action="execute",
                outcome="denied",
                details={"reason": reason},
            )
        )
    except Exception:
        pass
