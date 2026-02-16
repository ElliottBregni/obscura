"""Tests for sdk.internal.types — dataclasses, enums, message normalization."""

from __future__ import annotations

from sdk.internal.types import (
    Backend,
    ChunkKind,
    ContentBlock,
    HookContext,
    HookPoint,
    Message,
    Role,
    SessionRef,
    StreamChunk,
    ToolSpec,
)


# ---------------------------------------------------------------------------
# Backend enum
# ---------------------------------------------------------------------------


class TestBackend:
    def test_values(self) -> None:
        assert Backend.COPILOT.value == "copilot"
        assert Backend.CLAUDE.value == "claude"

    def test_from_string(self) -> None:
        assert Backend("copilot") is Backend.COPILOT
        assert Backend("claude") is Backend.CLAUDE


# ---------------------------------------------------------------------------
# ContentBlock
# ---------------------------------------------------------------------------


class TestContentBlock:
    def test_frozen(self) -> None:
        block = ContentBlock(kind="text", text="hello")
        try:
            block.text = "world"  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass

    def test_defaults(self) -> None:
        block = ContentBlock(kind="text")
        assert block.text == ""
        assert block.tool_name == ""
        assert block.tool_input == {}
        assert block.tool_use_id == ""
        assert block.is_error is False


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


class TestMessage:
    def test_text_property_concatenates(self) -> None:
        msg = Message(
            role=Role.ASSISTANT,
            content=[
                ContentBlock(kind="text", text="hello "),
                ContentBlock(kind="thinking", text="(should be excluded)"),
                ContentBlock(kind="text", text="world"),
            ],
        )
        assert msg.text == "hello world"

    def test_text_property_empty(self) -> None:
        msg = Message(role=Role.ASSISTANT, content=[])
        assert msg.text == ""

    def test_frozen(self) -> None:
        msg = Message(role=Role.USER, content=[])
        try:
            msg.role = Role.ASSISTANT  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass

    def test_raw_escape_hatch(self) -> None:
        sentinel = object()
        msg = Message(role=Role.ASSISTANT, content=[], raw=sentinel)
        assert msg.raw is sentinel

    def test_backend_tracking(self) -> None:
        msg = Message(role=Role.ASSISTANT, content=[], backend=Backend.COPILOT)
        assert msg.backend is Backend.COPILOT


# ---------------------------------------------------------------------------
# StreamChunk
# ---------------------------------------------------------------------------


class TestStreamChunk:
    def test_text_delta(self) -> None:
        chunk = StreamChunk(kind=ChunkKind.TEXT_DELTA, text="hello")
        assert chunk.kind is ChunkKind.TEXT_DELTA
        assert chunk.text == "hello"

    def test_frozen(self) -> None:
        chunk = StreamChunk(kind=ChunkKind.DONE)
        try:
            chunk.kind = ChunkKind.ERROR  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------


class TestToolSpec:
    def test_creation(self) -> None:
        def my_handler(x: str) -> str:
            return x

        spec = ToolSpec(
            name="test_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {"x": {"type": "string"}}},
            handler=my_handler,
        )
        assert spec.name == "test_tool"
        assert spec.handler is my_handler
        assert spec._pydantic_model is None  # pyright: ignore[reportPrivateUsage]

    def test_frozen(self) -> None:
        spec = ToolSpec(name="t", description="d", parameters={}, handler=lambda: None)
        try:
            spec.name = "other"  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# SessionRef
# ---------------------------------------------------------------------------


class TestSessionRef:
    def test_creation(self) -> None:
        ref = SessionRef(session_id="abc-123", backend=Backend.CLAUDE)
        assert ref.session_id == "abc-123"
        assert ref.backend is Backend.CLAUDE
        assert ref.raw is None


# ---------------------------------------------------------------------------
# HookContext
# ---------------------------------------------------------------------------


class TestHookContext:
    def test_defaults(self) -> None:
        ctx = HookContext(hook=HookPoint.PRE_TOOL_USE)
        assert ctx.tool_name == ""
        assert ctx.tool_input == {}
        assert ctx.tool_output is None
        assert ctx.message is None

    def test_with_tool_info(self) -> None:
        ctx = HookContext(
            hook=HookPoint.POST_TOOL_USE,
            tool_name="read_file",
            tool_input={"path": "/tmp/x"},
            tool_output="file contents",
        )
        assert ctx.tool_name == "read_file"
        assert ctx.tool_output == "file contents"
