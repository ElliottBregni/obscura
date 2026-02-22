"""Tests for sdk.internal.types — dataclasses, enums, message normalization."""

from __future__ import annotations

from obscura.core.types import (
    Backend,
    BackendCapabilities,
    ChunkKind,
    ContentBlock,
    ExecutionMode,
    HookContext,
    HookPoint,
    Message,
    ProviderNativeRequest,
    Role,
    SessionRef,
    StreamChunk,
    ToolSpec,
    ToolChoice,
    UnifiedRequest,
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
# ExecutionMode
# ---------------------------------------------------------------------------


class TestExecutionMode:
    def test_values(self) -> None:
        assert ExecutionMode.UNIFIED.value == "unified"
        assert ExecutionMode.NATIVE.value == "native"

    def test_from_string(self) -> None:
        assert ExecutionMode("unified") is ExecutionMode.UNIFIED
        assert ExecutionMode("native") is ExecutionMode.NATIVE


# ---------------------------------------------------------------------------
# ProviderNativeRequest / UnifiedRequest
# ---------------------------------------------------------------------------


class TestUnifiedRequest:
    def test_provider_native_request_defaults(self) -> None:
        req = ProviderNativeRequest()
        assert req.openai is None
        assert req.claude is None
        assert req.copilot is None
        assert req.localllm is None

    def test_unified_request_defaults(self) -> None:
        req = UnifiedRequest(prompt="hello")
        assert req.mode is ExecutionMode.UNIFIED
        assert req.prompt == "hello"
        assert req.messages is None
        assert req.tool_choice is None
        assert req.native is None
        assert req.metadata == {}

    def test_unified_request_native_mode(self) -> None:
        req = UnifiedRequest(
            prompt="use provider-native payload",
            mode=ExecutionMode.NATIVE,
            tool_choice=ToolChoice.none(),
            native=ProviderNativeRequest(
                openai={"model": "gpt-4.1", "input": "hello"},
            ),
        )
        assert req.mode is ExecutionMode.NATIVE
        assert req.tool_choice is not None
        assert req.tool_choice.mode == "none"
        assert req.native is not None
        assert req.native.openai is not None


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

    def test_native_event_passthrough(self) -> None:
        raw_event = {"provider_event": "x"}
        chunk = StreamChunk(
            kind=ChunkKind.TEXT_DELTA,
            text="hello",
            native_event=raw_event,
        )
        assert chunk.native_event == raw_event


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


# ---------------------------------------------------------------------------
# BackendCapabilities
# ---------------------------------------------------------------------------


class TestBackendCapabilities:
    def test_defaults(self) -> None:
        caps = BackendCapabilities()
        assert caps.supports_native_mode is True
        assert caps.native_features == ()

    def test_native_features(self) -> None:
        caps = BackendCapabilities(native_features=("event_stream", "sdk_hooks"))
        assert caps.native_features == ("event_stream", "sdk_hooks")
