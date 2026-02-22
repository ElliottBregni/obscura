"""Tests for obscura.testing — reusable test utilities.

Validates MockBackend, MockBackendBuilder, chunk helpers, tool helpers,
fake classes, and StubAgent work correctly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from obscura.core.agent_loop import AgentLoop
from obscura.core.types import (
    AgentEventKind,
    ChunkKind,
    StreamChunk,
)
from obscura.testing import (
    MockBackend,
    MockBackendBuilder,
    StubAgent,
    make_stub_agent,
    # Chunk helpers
    text_chunk,
    text_chunks,
    thinking_chunk,
    thinking_chunks,
    tool_call_chunks,
    done_chunk,
    error_chunk,
    tool_start_chunk,
    tool_delta_chunk,
    tool_end_chunk,
    # Tool helpers
    echo_handler,
    async_echo_handler,
    failing_handler,
    noop_handler,
    make_tool,
    make_registry,
    # Fakes
    FakeTextBlock,
    FakeThinkingBlock,
    FakeToolUseBlock,
    FakeAssistantMessage,
    FakeResultMessage,
    FakeSystemMessage,
    async_iter,
)


# ---------------------------------------------------------------------------
# Chunk factories
# ---------------------------------------------------------------------------


class TestChunkFactories:
    def test_text_chunk(self) -> None:
        c = text_chunk("hello")
        assert c.kind == ChunkKind.TEXT_DELTA
        assert c.text == "hello"

    def test_thinking_chunk(self) -> None:
        c = thinking_chunk("hmm")
        assert c.kind == ChunkKind.THINKING_DELTA
        assert c.text == "hmm"

    def test_done_chunk(self) -> None:
        c = done_chunk()
        assert c.kind == ChunkKind.DONE

    def test_error_chunk(self) -> None:
        c = error_chunk("fail")
        assert c.kind == ChunkKind.ERROR
        assert c.text == "fail"

    def test_tool_start_chunk(self) -> None:
        c = tool_start_chunk("search")
        assert c.kind == ChunkKind.TOOL_USE_START
        assert c.tool_name == "search"

    def test_tool_delta_chunk(self) -> None:
        c = tool_delta_chunk('{"q":"test"}')
        assert c.kind == ChunkKind.TOOL_USE_DELTA
        assert c.tool_input_delta == '{"q":"test"}'

    def test_tool_end_chunk(self) -> None:
        c = tool_end_chunk()
        assert c.kind == ChunkKind.TOOL_USE_END

    def test_text_chunks(self) -> None:
        chunks = text_chunks("Hello world")
        assert len(chunks) == 3  # "Hello ", "world ", DONE
        assert chunks[0].kind == ChunkKind.TEXT_DELTA
        assert chunks[-1].kind == ChunkKind.DONE

    def test_thinking_chunks(self) -> None:
        chunks = thinking_chunks("thinking...")
        assert len(chunks) == 2
        assert chunks[0].kind == ChunkKind.THINKING_DELTA
        assert chunks[1].kind == ChunkKind.DONE

    def test_tool_call_chunks(self) -> None:
        chunks = tool_call_chunks("read", {"path": "a.py"})
        assert chunks[0].kind == ChunkKind.TOOL_USE_START
        assert chunks[0].tool_name == "read"
        assert chunks[1].kind == ChunkKind.TOOL_USE_DELTA
        assert json.loads(chunks[1].tool_input_delta) == {"path": "a.py"}
        assert chunks[2].kind == ChunkKind.DONE

    def test_tool_call_chunks_with_preceding_text(self) -> None:
        chunks = tool_call_chunks("read", {}, preceding_text="Let me read:")
        assert chunks[0].kind == ChunkKind.TEXT_DELTA
        assert chunks[0].text == "Let me read:"
        assert chunks[1].kind == ChunkKind.TOOL_USE_START


# ---------------------------------------------------------------------------
# Tool helpers
# ---------------------------------------------------------------------------


class TestToolHelpers:
    def test_echo_handler(self) -> None:
        result = echo_handler(x=1, y="two")
        assert result == {"echo": {"x": 1, "y": "two"}}

    @pytest.mark.asyncio
    async def test_async_echo_handler(self) -> None:
        result = await async_echo_handler(key="val")
        assert result == {"echo": {"key": "val"}}

    def test_failing_handler(self) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            failing_handler()

    def test_noop_handler(self) -> None:
        assert noop_handler() == ""

    def test_make_tool_defaults(self) -> None:
        spec = make_tool("my_tool")
        assert spec.name == "my_tool"
        assert spec.description == "Test tool: my_tool"
        assert spec.parameters == {"type": "object"}
        # Handler defaults to echo_handler
        assert spec.handler is echo_handler

    def test_make_tool_with_params(self) -> None:
        spec = make_tool("search", params={"q": {"type": "string"}})
        assert spec.parameters == {
            "type": "object",
            "properties": {"q": {"type": "string"}},
        }

    def test_make_tool_with_custom_handler(self) -> None:
        spec = make_tool("fail", handler=failing_handler)
        assert spec.handler is failing_handler

    def test_make_registry(self) -> None:
        s1 = make_tool("a")
        s2 = make_tool("b")
        reg = make_registry(s1, s2)
        assert "a" in reg
        assert "b" in reg


# ---------------------------------------------------------------------------
# MockBackend
# ---------------------------------------------------------------------------


class TestMockBackend:
    @pytest.mark.asyncio
    async def test_stream_returns_turns(self) -> None:
        backend = MockBackend([
            text_chunks("hello"),
            text_chunks("world"),
        ])
        chunks: list[StreamChunk] = []
        async for c in backend.stream("prompt1"):
            chunks.append(c)
        assert any(c.kind == ChunkKind.TEXT_DELTA for c in chunks)
        assert backend.call_count == 1
        assert backend.prompts == ["prompt1"]

    @pytest.mark.asyncio
    async def test_stream_exhausted_returns_done(self) -> None:
        backend = MockBackend([text_chunks("only")])
        # consume first turn
        async for _ in backend.stream("turn1"):
            pass
        # second turn should return just DONE
        chunks: list[StreamChunk] = []
        async for c in backend.stream("turn2"):
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].kind == ChunkKind.DONE

    def test_register_tool(self) -> None:
        backend = MockBackend()
        spec = make_tool("test")
        backend.register_tool(spec)
        assert "test" in backend.get_tool_registry()


# ---------------------------------------------------------------------------
# MockBackendBuilder
# ---------------------------------------------------------------------------


class TestMockBackendBuilder:
    def test_build_empty(self) -> None:
        backend = MockBackendBuilder().build()
        assert isinstance(backend, MockBackend)

    def test_with_turn(self) -> None:
        backend = (
            MockBackendBuilder()
            .with_turn(text_chunks("hello"))
            .build()
        )
        assert backend._turns  # noqa: SLF001

    def test_with_text(self) -> None:
        backend = (
            MockBackendBuilder()
            .with_text("hi there")
            .build()
        )
        assert len(backend._turns) == 1  # noqa: SLF001
        assert backend._turns[0][-1].kind == ChunkKind.DONE  # noqa: SLF001

    def test_with_tool_call(self) -> None:
        backend = (
            MockBackendBuilder()
            .with_tool_call("read", {"path": "a.py"})
            .build()
        )
        assert backend._turns[0][0].kind == ChunkKind.TOOL_USE_START  # noqa: SLF001

    def test_with_tool(self) -> None:
        spec = make_tool("my_tool")
        backend = (
            MockBackendBuilder()
            .with_tool(spec)
            .build()
        )
        assert "my_tool" in backend.get_tool_registry()

    def test_with_turns(self) -> None:
        backend = (
            MockBackendBuilder()
            .with_turns(text_chunks("a"), text_chunks("b"))
            .build()
        )
        assert len(backend._turns) == 2  # noqa: SLF001

    def test_with_tools(self) -> None:
        backend = (
            MockBackendBuilder()
            .with_tools(make_tool("a"), make_tool("b"))
            .build()
        )
        assert "a" in backend.get_tool_registry()
        assert "b" in backend.get_tool_registry()

    @pytest.mark.asyncio
    async def test_builder_with_agent_loop(self) -> None:
        """End-to-end: builder → MockBackend → AgentLoop."""
        def read_handler(path: str = "") -> str:
            return f"contents of {path}"

        backend = (
            MockBackendBuilder()
            .with_turn(tool_call_chunks("read", {"path": "main.py"}))
            .with_turn(text_chunks("The file has a main function."))
            .with_tool(make_tool("read", handler=read_handler))
            .build()
        )

        loop = AgentLoop(backend, backend.get_tool_registry())
        events = [e async for e in loop.run("read main.py")]

        tool_results = [e for e in events if e.kind == AgentEventKind.TOOL_RESULT]
        assert len(tool_results) == 1
        assert "contents of main.py" in tool_results[0].tool_result

        done = [e for e in events if e.kind == AgentEventKind.AGENT_DONE]
        assert len(done) == 1


# ---------------------------------------------------------------------------
# Fake classes
# ---------------------------------------------------------------------------


class TestFakes:
    def test_fake_text_block(self) -> None:
        b = FakeTextBlock("hello")
        assert b.text == "hello"
        assert type(b).__name__ == "TextBlock"

    def test_fake_thinking_block(self) -> None:
        b = FakeThinkingBlock("think")
        assert b.thinking == "think"
        assert type(b).__name__ == "ThinkingBlock"

    def test_fake_tool_use_block(self) -> None:
        b = FakeToolUseBlock("read")
        assert b.name == "read"
        assert b.id == "tool-123"
        assert type(b).__name__ == "ToolUseBlock"

    def test_fake_assistant_message(self) -> None:
        msg = FakeAssistantMessage([FakeTextBlock("hi")])
        assert len(msg.content) == 1
        assert type(msg).__name__ == "AssistantMessage"

    def test_fake_result_message(self) -> None:
        assert FakeResultMessage.session_id == "sess-abc"
        assert type(FakeResultMessage()).__name__ == "ResultMessage"

    def test_fake_system_message(self) -> None:
        assert FakeSystemMessage.subtype == "info"

    @pytest.mark.asyncio
    async def test_async_iter(self) -> None:
        items = [1, 2, 3]
        collected = [x async for x in async_iter(items)]
        assert collected == [1, 2, 3]


# ---------------------------------------------------------------------------
# StubAgent
# ---------------------------------------------------------------------------


class TestStubAgent:
    @pytest.mark.asyncio
    async def test_aper_phases(self) -> None:
        agent = make_stub_agent(name="test")
        result = await agent.run()
        assert agent.call_order == ["analyze", "plan", "execute", "respond"]
        assert result == [2, 4, 6]

    @pytest.mark.asyncio
    async def test_with_input_data(self) -> None:
        agent = StubAgent()
        result = await agent.run(input_data={"items": [10, 20]})
        assert result == [20, 40]

    @pytest.mark.asyncio
    async def test_name(self) -> None:
        agent = make_stub_agent(name="my-agent")
        assert agent.name == "my-agent"
