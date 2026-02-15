"""
sdk.agent_loop — Iterative agent loop with tool execution.

Drives the model in a loop: send prompt → stream response → detect tool
calls → execute tools → feed results back → repeat until the model
produces a final text response or *max_turns* is reached.

Works with all backends (Copilot, Claude, OpenAI, LocalLLM).

Usage::

    from sdk.agent_loop import AgentLoop
    from sdk._types import AgentEventKind

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
import json
import logging
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

from sdk._tools import ToolRegistry
from sdk._types import (
    AgentEvent,
    AgentEventKind,
    ChunkKind,
    StreamChunk,
    ToolCallInfo,
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
        backend: Any,
        tool_registry: ToolRegistry,
        *,
        max_turns: int = 10,
        on_confirm: ConfirmationCallback | None = None,
    ) -> None:
        self._backend = backend
        self._tools = tool_registry
        self._max_turns = max_turns
        self._on_confirm = on_confirm

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

            try:
                async for chunk in self._backend.stream(current_prompt, **kwargs):
                    event = self._map_chunk(chunk, turn)
                    if event is not None:
                        yield event

                    # Accumulate text
                    if chunk.kind == ChunkKind.TEXT_DELTA:
                        turn_text += chunk.text

                    # Collect tool calls
                    if chunk.kind == ChunkKind.TOOL_USE_START:
                        # Flush previous tool if any
                        if _current_tool_name:
                            tc = self._parse_tool_call(
                                _current_tool_name, _current_tool_input_json, chunk.raw,
                            )
                            tool_calls.append(tc)
                        _current_tool_name = chunk.tool_name
                        _current_tool_input_json = ""

                    if chunk.kind == ChunkKind.TOOL_USE_DELTA:
                        _current_tool_input_json += chunk.tool_input_delta

                # Flush last tool call
                if _current_tool_name:
                    tc = self._parse_tool_call(
                        _current_tool_name, _current_tool_input_json, None,
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
            yield AgentEvent(kind=AgentEventKind.TURN_COMPLETE, turn=turn, text=turn_text)

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
            for tc, result_text, is_err in tool_results:
                yield AgentEvent(
                    kind=AgentEventKind.TOOL_RESULT,
                    tool_name=tc.name,
                    tool_use_id=tc.tool_use_id,
                    tool_result=result_text,
                    is_error=is_err,
                    turn=turn,
                )

            # Build the next prompt with tool results
            current_prompt = self._format_tool_results(tool_results)

            # Clear send-once kwargs after first turn
            kwargs = {}

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
    ) -> list[tuple[ToolCallInfo, str, bool]]:
        """Execute tool calls and return (call, result_text, is_error) tuples."""
        results: list[tuple[ToolCallInfo, str, bool]] = []

        for tc in tool_calls:
            # Confirmation gate
            if self._on_confirm is not None:
                approved = self._on_confirm(tc)
                if asyncio.iscoroutine(approved) or asyncio.isfuture(approved):
                    approved = await approved
                if not approved:
                    results.append((tc, "Tool call denied by user.", True))
                    continue

            spec = self._tools.get(tc.name)
            if spec is None:
                results.append((
                    tc,
                    f"Unknown tool: {tc.name}. Available: {', '.join(self._tools.names())}",
                    True,
                ))
                continue

            try:
                result = await self._call_handler(spec, tc.input)
                result_text = result if isinstance(result, str) else json.dumps(result, default=str)
                results.append((tc, result_text, False))
            except Exception as exc:
                logger.warning("Tool %s failed: %s", tc.name, exc)
                results.append((tc, f"Tool error: {exc}", True))

        return results

    @staticmethod
    async def _call_handler(spec: ToolSpec, inputs: dict[str, Any]) -> Any:
        """Call a tool handler (sync or async)."""
        handler = spec.handler
        if asyncio.iscoroutinefunction(handler):
            return await handler(**inputs)
        return handler(**inputs)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _map_chunk(chunk: StreamChunk, turn: int) -> AgentEvent | None:
        """Map a StreamChunk to an AgentEvent, or None to skip."""
        if chunk.kind == ChunkKind.TEXT_DELTA:
            return AgentEvent(
                kind=AgentEventKind.TEXT_DELTA, text=chunk.text, turn=turn, raw=chunk.raw,
            )
        if chunk.kind == ChunkKind.THINKING_DELTA:
            return AgentEvent(
                kind=AgentEventKind.THINKING_DELTA, text=chunk.text, turn=turn, raw=chunk.raw,
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
                kind=AgentEventKind.ERROR, text=chunk.text, turn=turn, raw=chunk.raw,
            )
        return None

    @staticmethod
    def _parse_tool_call(
        name: str, input_json: str, raw: Any,
    ) -> ToolCallInfo:
        """Parse accumulated tool call data into a ToolCallInfo."""
        parsed_input: dict[str, Any] = {}
        if input_json.strip():
            try:
                parsed_input = json.loads(input_json)
            except json.JSONDecodeError:
                parsed_input = {"_raw_input": input_json}

        return ToolCallInfo(
            tool_use_id=f"tool_{uuid.uuid4().hex[:12]}",
            name=name,
            input=parsed_input,
            raw=raw,
        )

    @staticmethod
    def _format_tool_results(
        results: list[tuple[ToolCallInfo, str, bool]],
    ) -> str:
        """Format tool results as a prompt for the next model turn."""
        parts: list[str] = []
        for tc, result_text, is_error in results:
            status = "ERROR" if is_error else "OK"
            parts.append(
                f"[Tool Result: {tc.name} (id={tc.tool_use_id}) status={status}]\n"
                f"{result_text}\n"
                f"[/Tool Result]"
            )
        return "\n\n".join(parts)
