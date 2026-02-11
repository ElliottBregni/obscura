"""Tests for sdk.agent_loop — Iterative agent loop with tool execution."""

from __future__ import annotations

import asyncio
import json
import pytest
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock

from sdk._tools import ToolRegistry
from sdk._types import (
    AgentEvent,
    AgentEventKind,
    ChunkKind,
    StreamChunk,
    ToolCallInfo,
    ToolSpec,
)
from sdk.agent_loop import AgentLoop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_text_chunks(text: str) -> list[StreamChunk]:
    """Split text into word-level TEXT_DELTA chunks + DONE."""
    chunks = []
    for word in text.split(" "):
        chunks.append(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=word + " "))
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


def _make_tool_call_chunks(
    tool_name: str,
    tool_input: dict[str, Any],
    preceding_text: str = "",
) -> list[StreamChunk]:
    """Create chunks that simulate the model calling a tool."""
    chunks: list[StreamChunk] = []
    if preceding_text:
        chunks.append(StreamChunk(kind=ChunkKind.TEXT_DELTA, text=preceding_text))
    chunks.append(StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name=tool_name))
    input_json = json.dumps(tool_input)
    chunks.append(StreamChunk(kind=ChunkKind.TOOL_USE_DELTA, tool_input_delta=input_json))
    chunks.append(StreamChunk(kind=ChunkKind.DONE))
    return chunks


class MockBackend:
    """A mock backend that returns pre-configured stream responses per turn."""

    def __init__(self, turn_responses: list[list[StreamChunk]]) -> None:
        self._turns = list(turn_responses)
        self._call_count = 0

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        if self._call_count < len(self._turns):
            chunks = self._turns[self._call_count]
        else:
            # Default: return done
            chunks = [StreamChunk(kind=ChunkKind.DONE)]
        self._call_count += 1
        for chunk in chunks:
            yield chunk


def _make_registry(*specs: ToolSpec) -> ToolRegistry:
    reg = ToolRegistry()
    for s in specs:
        reg.register(s)
    return reg


# ---------------------------------------------------------------------------
# Tests: Basic loop behaviour
# ---------------------------------------------------------------------------

class TestAgentLoopBasic:
    @pytest.mark.asyncio
    async def test_single_turn_text_only(self) -> None:
        """When model produces only text (no tool calls), loop exits after 1 turn."""
        backend = MockBackend([_make_text_chunks("Hello world")])
        loop = AgentLoop(backend, _make_registry())

        events = [e async for e in loop.run("greet me")]

        kinds = [e.kind for e in events]
        assert AgentEventKind.TURN_START in kinds
        assert AgentEventKind.TEXT_DELTA in kinds
        assert AgentEventKind.TURN_COMPLETE in kinds
        assert AgentEventKind.AGENT_DONE in kinds
        # Should be exactly 1 turn
        assert sum(1 for e in events if e.kind == AgentEventKind.TURN_START) == 1

    @pytest.mark.asyncio
    async def test_text_content_collected(self) -> None:
        """TEXT_DELTA events should contain the streamed text."""
        backend = MockBackend([_make_text_chunks("The answer is 42")])
        loop = AgentLoop(backend, _make_registry())

        text_parts = []
        async for event in loop.run("question"):
            if event.kind == AgentEventKind.TEXT_DELTA:
                text_parts.append(event.text)

        full_text = "".join(text_parts)
        assert "answer" in full_text
        assert "42" in full_text

    @pytest.mark.asyncio
    async def test_run_to_completion(self) -> None:
        """run_to_completion() should return concatenated text."""
        backend = MockBackend([_make_text_chunks("done")])
        loop = AgentLoop(backend, _make_registry())

        result = await loop.run_to_completion("go")
        assert "done" in result

    @pytest.mark.asyncio
    async def test_thinking_events_emitted(self) -> None:
        """THINKING_DELTA chunks should become THINKING_DELTA events."""
        chunks = [
            StreamChunk(kind=ChunkKind.THINKING_DELTA, text="Let me think..."),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="Answer"),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        backend = MockBackend([chunks])
        loop = AgentLoop(backend, _make_registry())

        events = [e async for e in loop.run("think")]
        thinking = [e for e in events if e.kind == AgentEventKind.THINKING_DELTA]
        assert len(thinking) == 1
        assert thinking[0].text == "Let me think..."


# ---------------------------------------------------------------------------
# Tests: Tool execution loop
# ---------------------------------------------------------------------------

class TestAgentLoopToolExecution:
    @pytest.mark.asyncio
    async def test_tool_call_and_result(self) -> None:
        """Model calls a tool → loop executes it → feeds result back."""
        read_file_spec = ToolSpec(
            name="read_file",
            description="Read a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            handler=lambda path: f"contents of {path}",
        )

        # Turn 1: model calls read_file
        turn1 = _make_tool_call_chunks("read_file", {"path": "main.py"})
        # Turn 2: model responds with text after seeing tool result
        turn2 = _make_text_chunks("The file contains a main function.")

        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(read_file_spec))

        events = [e async for e in loop.run("read main.py")]

        # Should have tool call and result events
        tool_calls = [e for e in events if e.kind == AgentEventKind.TOOL_CALL]
        tool_results = [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(tool_calls) == 1
        assert tool_calls[0].tool_name == "read_file"
        assert len(tool_results) == 1
        assert "contents of main.py" in tool_results[0].tool_result

        # Should have 2 turns
        turn_starts = [e for e in events if e.kind == AgentEventKind.TURN_START]
        assert len(turn_starts) == 2

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_one_turn(self) -> None:
        """Model calls two tools in the same turn."""
        spec_a = ToolSpec(
            name="tool_a", description="A", parameters={},
            handler=lambda: "result_a",
        )
        spec_b = ToolSpec(
            name="tool_b", description="B", parameters={},
            handler=lambda: "result_b",
        )

        # Turn 1: two tool calls
        turn1 = [
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="tool_a"),
            StreamChunk(kind=ChunkKind.TOOL_USE_DELTA, tool_input_delta="{}"),
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="tool_b"),
            StreamChunk(kind=ChunkKind.TOOL_USE_DELTA, tool_input_delta="{}"),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        # Turn 2: text response
        turn2 = _make_text_chunks("Both tools ran.")

        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec_a, spec_b))

        events = [e async for e in loop.run("run both")]
        tool_results = [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(tool_results) == 2
        result_names = {e.tool_name for e in tool_results}
        assert result_names == {"tool_a", "tool_b"}

    @pytest.mark.asyncio
    async def test_async_tool_handler(self) -> None:
        """Async tool handlers should be awaited properly."""
        async def async_handler(query: str) -> str:
            await asyncio.sleep(0.01)
            return f"searched for {query}"

        spec = ToolSpec(
            name="search", description="Search",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            handler=async_handler,
        )

        turn1 = _make_tool_call_chunks("search", {"query": "agent loops"})
        turn2 = _make_text_chunks("Found results.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("search")]
        tool_results = [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(tool_results) == 1
        assert "searched for agent loops" in tool_results[0].tool_result

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self) -> None:
        """If model calls a tool not in the registry, yield an error result."""
        turn1 = _make_tool_call_chunks("nonexistent_tool", {"x": 1})
        turn2 = _make_text_chunks("OK sorry.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry())

        events = [e async for e in loop.run("call missing")]
        tool_results = [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(tool_results) == 1
        assert tool_results[0].is_error is True
        assert "Unknown tool" in tool_results[0].tool_result

    @pytest.mark.asyncio
    async def test_tool_handler_exception(self) -> None:
        """If a tool handler raises, the result should be an error."""
        def failing_handler() -> str:
            raise RuntimeError("disk full")

        spec = ToolSpec(name="write", description="Write", parameters={}, handler=failing_handler)
        turn1 = _make_tool_call_chunks("write", {})
        turn2 = _make_text_chunks("Write failed.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("write something")]
        tool_results = [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(tool_results) == 1
        assert tool_results[0].is_error is True
        assert "disk full" in tool_results[0].tool_result


# ---------------------------------------------------------------------------
# Tests: Max turns
# ---------------------------------------------------------------------------

class TestAgentLoopMaxTurns:
    @pytest.mark.asyncio
    async def test_max_turns_respected(self) -> None:
        """Loop should stop after max_turns even if model keeps calling tools."""
        spec = ToolSpec(
            name="loop_tool", description="Loops", parameters={},
            handler=lambda: "ok",
        )

        # Every turn calls a tool → infinite loop without max_turns
        turn = _make_tool_call_chunks("loop_tool", {})
        backend = MockBackend([turn] * 20)
        loop = AgentLoop(backend, _make_registry(spec), max_turns=3)

        events = [e async for e in loop.run("loop forever")]
        turn_starts = [e for e in events if e.kind == AgentEventKind.TURN_START]
        assert len(turn_starts) == 3

        done_events = [e for e in events if e.kind == AgentEventKind.AGENT_DONE]
        assert len(done_events) == 1

    @pytest.mark.asyncio
    async def test_default_max_turns(self) -> None:
        """Default max_turns should be 10."""
        loop = AgentLoop(MockBackend([]), _make_registry())
        assert loop._max_turns == 10


# ---------------------------------------------------------------------------
# Tests: Confirmation callback
# ---------------------------------------------------------------------------

class TestAgentLoopConfirmation:
    @pytest.mark.asyncio
    async def test_confirm_approve(self) -> None:
        """When on_confirm returns True, tool is executed normally."""
        spec = ToolSpec(
            name="deploy", description="Deploy",
            parameters={}, handler=lambda: "deployed",
        )

        confirmations: list[ToolCallInfo] = []

        def approve(tc: ToolCallInfo) -> bool:
            confirmations.append(tc)
            return True

        turn1 = _make_tool_call_chunks("deploy", {})
        turn2 = _make_text_chunks("Deployed successfully.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec), on_confirm=approve)

        events = [e async for e in loop.run("deploy")]
        tool_results = [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(tool_results) == 1
        assert tool_results[0].is_error is False
        assert "deployed" in tool_results[0].tool_result
        assert len(confirmations) == 1
        assert confirmations[0].name == "deploy"

    @pytest.mark.asyncio
    async def test_confirm_deny(self) -> None:
        """When on_confirm returns False, tool is denied."""
        spec = ToolSpec(
            name="rm_rf", description="Delete",
            parameters={}, handler=lambda: "deleted everything",
        )

        turn1 = _make_tool_call_chunks("rm_rf", {})
        turn2 = _make_text_chunks("OK, I won't delete.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(
            backend,
            _make_registry(spec),
            on_confirm=lambda tc: False,
        )

        events = [e async for e in loop.run("delete all")]
        tool_results = [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(tool_results) == 1
        assert tool_results[0].is_error is True
        assert "denied" in tool_results[0].tool_result.lower()

    @pytest.mark.asyncio
    async def test_async_confirm_callback(self) -> None:
        """Async confirmation callbacks should be awaited."""
        spec = ToolSpec(
            name="action", description="Action",
            parameters={}, handler=lambda: "done",
        )

        async def async_confirm(tc: ToolCallInfo) -> bool:
            await asyncio.sleep(0.01)
            return True

        turn1 = _make_tool_call_chunks("action", {})
        turn2 = _make_text_chunks("Done.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec), on_confirm=async_confirm)

        events = [e async for e in loop.run("act")]
        tool_results = [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(tool_results) == 1
        assert tool_results[0].is_error is False


# ---------------------------------------------------------------------------
# Tests: Error handling
# ---------------------------------------------------------------------------

class TestAgentLoopErrors:
    @pytest.mark.asyncio
    async def test_backend_stream_error(self) -> None:
        """If the backend stream raises, an ERROR event is yielded."""
        class FailingBackend:
            async def stream(self, prompt: str, **kw: Any) -> AsyncIterator[StreamChunk]:
                raise ConnectionError("backend down")
                yield  # make it a generator  # noqa: E501

        loop = AgentLoop(FailingBackend(), _make_registry())
        events = [e async for e in loop.run("fail")]
        errors = [e for e in events if e.kind == AgentEventKind.ERROR]
        assert len(errors) == 1
        assert "backend down" in errors[0].text


# ---------------------------------------------------------------------------
# Tests: Turn tracking
# ---------------------------------------------------------------------------

class TestAgentLoopTurnTracking:
    @pytest.mark.asyncio
    async def test_turn_numbers_increment(self) -> None:
        """Turn numbers should increment across tool-call iterations."""
        spec = ToolSpec(
            name="step", description="Step", parameters={},
            handler=lambda: "stepped",
        )

        turn1 = _make_tool_call_chunks("step", {})
        turn2 = _make_tool_call_chunks("step", {})
        turn3 = _make_text_chunks("All done.")
        backend = MockBackend([turn1, turn2, turn3])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("go")]
        turn_starts = [e for e in events if e.kind == AgentEventKind.TURN_START]
        assert [e.turn for e in turn_starts] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_agent_done_has_accumulated_text(self) -> None:
        """AGENT_DONE event should contain all text from all turns."""
        spec = ToolSpec(
            name="fetch", description="Fetch", parameters={},
            handler=lambda: "data",
        )

        turn1 = [
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="Fetching... "),
            StreamChunk(kind=ChunkKind.TOOL_USE_START, tool_name="fetch"),
            StreamChunk(kind=ChunkKind.TOOL_USE_DELTA, tool_input_delta="{}"),
            StreamChunk(kind=ChunkKind.DONE),
        ]
        turn2 = _make_text_chunks("Got the data.")
        backend = MockBackend([turn1, turn2])
        loop = AgentLoop(backend, _make_registry(spec))

        events = [e async for e in loop.run("fetch data")]
        done = [e for e in events if e.kind == AgentEventKind.AGENT_DONE]
        assert len(done) == 1
        assert "Fetching" in done[0].text
        assert "data" in done[0].text


# ---------------------------------------------------------------------------
# Tests: ToolCallInfo parsing
# ---------------------------------------------------------------------------

class TestToolCallParsing:
    def test_parse_valid_json(self) -> None:
        tc = AgentLoop._parse_tool_call("my_tool", '{"key": "value"}', None)
        assert tc.name == "my_tool"
        assert tc.input == {"key": "value"}
        assert tc.tool_use_id.startswith("tool_")

    def test_parse_invalid_json(self) -> None:
        tc = AgentLoop._parse_tool_call("bad_tool", "not json", None)
        assert tc.name == "bad_tool"
        assert "_raw_input" in tc.input

    def test_parse_empty_input(self) -> None:
        tc = AgentLoop._parse_tool_call("no_args", "", None)
        assert tc.input == {}

    def test_format_tool_results(self) -> None:
        tc = ToolCallInfo(tool_use_id="tool_abc", name="read", input={})
        formatted = AgentLoop._format_tool_results([(tc, "file content", False)])
        assert "read" in formatted
        assert "tool_abc" in formatted
        assert "file content" in formatted
        assert "OK" in formatted

    def test_format_error_tool_results(self) -> None:
        tc = ToolCallInfo(tool_use_id="tool_err", name="write", input={})
        formatted = AgentLoop._format_tool_results([(tc, "permission denied", True)])
        assert "ERROR" in formatted
        assert "permission denied" in formatted
