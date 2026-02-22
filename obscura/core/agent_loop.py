"""
obscura.agent_loop — Iterative agent loop with tool execution.

Drives the model in a loop: send prompt → stream response → detect tool
calls → execute tools → feed results back → repeat until the model
produces a final text response or *max_turns* is reached.

Works with all backends (Copilot, Claude, OpenAI, LocalLLM).

Usage::

    from obscura.core.agent_loop import AgentLoop
    from obscura.core.types import AgentEventKind

    loop = AgentLoop(backend, tool_registry, max_turns=10)

    async for event in loop.run("Fix the auth bug"):
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

logger = logging.getLogger(__name__)

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
    """

    def __init__(
        self,
        backend: BackendProtocol | None,
        tool_registry: ToolRegistry,
        *,
        max_turns: int = 10,
        on_confirm: ConfirmationCallback | None = None,
        capability_token: Any | None = None,
    ) -> None:
        self._backend = backend
        self._tools = tool_registry
        self._max_turns = max_turns
        self._on_confirm = on_confirm
        self._capability_token = capability_token

    @property
    def max_turns(self) -> int:
        """Read-only max_turns (testing/observability)."""
        return self._max_turns

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, prompt: str, **kwargs: Any) -> AsyncIterator[AgentEvent]:
        """Run the agent loop, yielding events as they occur.

        Yields
        ------
        AgentEvent
            TURN_START, TEXT_DELTA, THINKING_DELTA, TOOL_CALL, TOOL_RESULT,
            TURN_COMPLETE, and finally AGENT_DONE (or ERROR).
        """
        turn = 0
        current_prompt = prompt
        accumulated_text = ""

        while turn < self._max_turns:
            turn += 1
            yield AgentEvent(kind=AgentEventKind.TURN_START, turn=turn)

            tool_calls: list[ToolCallInfo] = []
            turn_text = ""
            _current_tool_name = ""
            _current_tool_input_json = ""
            _current_tool_raw: Any = None

            try:
                if self._backend is None:
                    raise RuntimeError("No backend configured")
                async for chunk in self._backend.stream(current_prompt, **kwargs):
                    event = self._map_chunk(chunk, turn)
                    if event is not None:
                        yield event

                    # Accumulate text
                    if chunk.kind == ChunkKind.TEXT_DELTA:
                        turn_text += chunk.text

                    # Collect tool calls
                    if chunk.kind == ChunkKind.TOOL_USE_START:
                        # Flush previous tool if any (fallback for backends
                        # that don't emit TOOL_USE_END)
                        if _current_tool_name:
                            tc = self._parse_tool_call(
                                _current_tool_name,
                                _current_tool_input_json,
                                _current_tool_raw,
                            )
                            tool_calls.append(tc)
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

            except Exception as exc:
                yield AgentEvent(
                    kind=AgentEventKind.ERROR,
                    text=str(exc),
                    turn=turn,
                    raw=exc,
                )
                return

            accumulated_text += turn_text
            yield AgentEvent(
                kind=AgentEventKind.TURN_COMPLETE, turn=turn, text=turn_text
            )

            # No tool calls → model is done
            if not tool_calls:
                yield AgentEvent(
                    kind=AgentEventKind.AGENT_DONE,
                    turn=turn,
                    text=accumulated_text,
                )
                return

            # Execute tool calls and build results for next turn
            tool_results = await self._execute_tools(tool_calls, turn)

            # Yield tool events
            for result in tool_results:
                yield AgentEvent(
                    kind=AgentEventKind.TOOL_RESULT,
                    tool_name=result.tool,
                    tool_use_id=result.tool_use_id,
                    tool_result=self._render_tool_result_text(result),
                    is_error=result.status == "error",
                    turn=turn,
                    raw=result,
                )

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
        yield AgentEvent(
            kind=AgentEventKind.AGENT_DONE,
            turn=turn,
            text=accumulated_text,
        )

    # ------------------------------------------------------------------
    # Convenience: run and collect final text
    # ------------------------------------------------------------------

    async def run_to_completion(self, prompt: str, **kwargs: Any) -> str:
        """Run the loop and return the concatenated text output."""
        text_parts: list[str] = []
        async for event in self.run(prompt, **kwargs):
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
        if result.status == "ok":
            if isinstance(result.result, str):
                return result.result
            return json.dumps(result.result, default=str)
        if result.error is None:
            return "Tool error"
        payload = {
            "type": result.error.type.value,
            "message": result.error.message,
            "retry_after_ms": result.error.retry_after_ms,
            "safe_to_retry": result.error.safe_to_retry,
        }
        return json.dumps(payload, default=str)

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
        if chunk.kind == ChunkKind.TOOL_USE_START:
            return AgentEvent(
                kind=AgentEventKind.TOOL_CALL,
                tool_name=chunk.tool_name,
                turn=turn,
                raw=chunk.raw,
            )
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
